from __future__ import annotations

from typing import TYPE_CHECKING

from ..gltf.constants import TextureFilter, TextureWrap
from ..layer_node.constants import N_CH, CH_TO_IDX, BLEND_MODES

if TYPE_CHECKING:
    import bpy
    from ..gltf.types import Gltf, TextureInfo, NormalTextureInfo
    from .texture import TextureImporter
    from ..importer import ImportSettings


EXT_MATERIALS_LAYERS = "CUSTOM_materials_layers"
EXT_PACKED_TEXTURE = "CUSTOM_packed_texture"
BSDF_STACK_NODE_IDNAME = "BSDFStackNodeType"

# Valid CUSTOM_materials_layers blend mode ids, derived from the shared table.
_VALID_BLEND_MODES = {m[0] for m in BLEND_MODES}


class MaterialImporter:
    def __init__(
        self,
        gltf: "Gltf",
        texture_importer: "TextureImporter",
        settings: "ImportSettings",
    ) -> None:
        self.gltf = gltf
        self.texture_importer = texture_importer
        self.settings = settings
        self.blender_materials: dict[int, "bpy.types.Material"] = {}

    def import_all(self) -> None:
        if self.gltf.materials is None:
            return
        for i, gltf_mat in enumerate(self.gltf.materials):
            self.blender_materials[i] = self._import_material(i, gltf_mat)

    def get_blender_material(self, material_index: int) -> "bpy.types.Material | None":
        return self.blender_materials.get(material_index)

    def _import_material(self, index, gltf_mat) -> "bpy.types.Material":
        import bpy

        name = gltf_mat.name or f"Material_{index}"
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
        tree = mat.node_tree
        tree.nodes.clear()

        output = tree.nodes.new("ShaderNodeOutputMaterial")
        output.location = (400, 0)

        # CUSTOM_materials_layers: a material with a non-empty `layers` array is
        # authored with a single BSDFStackNode (its Principled BSDF is internal).
        # A `base`-only extension (height/bump on an otherwise plain material)
        # stays a standard Principled material.
        ext_layers = (
            gltf_mat.extensions.get(EXT_MATERIALS_LAYERS)
            if gltf_mat.extensions else None
        )
        has_layers = bool(ext_layers and ext_layers.get("layers"))
        base = ext_layers.get("base") if ext_layers else None

        principled = None
        if has_layers:
            self._build_bsdf_stack(tree, output, gltf_mat, ext_layers)
        else:
            principled = tree.nodes.new("ShaderNodeBsdfPrincipled")
            principled.location = (0, 0)
            tree.links.new(principled.outputs["BSDF"], output.inputs["Surface"])

            pbr = gltf_mat.pbr_metallic_roughness
            if pbr:
                self._apply_pbr(tree, principled, pbr, gltf_mat, mat)

            if base and (base.get("heightTexture") or base.get("bump")):
                # Rebuild the Bump node (Height <- displacement, Normal <- the
                # normal map) feeding the Principled Normal socket.
                self._apply_principled_bump(
                    tree, principled, gltf_mat.normal_texture, base,
                )
            elif gltf_mat.normal_texture:
                self._apply_normal_texture(tree, principled, gltf_mat.normal_texture)

            split = self._split_emissive(
                gltf_mat.emissive_factor, self._emissive_strength_mult(gltf_mat),
            )
            if split is not None:
                color, strength = split
                principled.inputs["Emission Color"].default_value = color
                principled.inputs["Emission Strength"].default_value = strength

            if gltf_mat.emissive_texture:
                self._apply_texture(tree, principled, "Emission Color", gltf_mat.emissive_texture, y_offset=-400)

            # Material PBR-extension layers on the Principled BSDF.
            if gltf_mat.extensions:
                self._apply_clearcoat(tree, principled, gltf_mat.extensions)
                self._apply_sheen(tree, principled, gltf_mat.extensions)

        if gltf_mat.alpha_mode == "BLEND":
            if hasattr(mat, "surface_render_method"):
                mat.surface_render_method = "BLENDED"
        elif gltf_mat.alpha_mode == "MASK":
            if hasattr(mat, "surface_render_method"):
                mat.surface_render_method = "DITHERED"
            mat.alpha_threshold = gltf_mat.alpha_cutoff if gltf_mat.alpha_cutoff is not None else 0.5

        if gltf_mat.double_sided:
            mat.use_backface_culling = False
        else:
            mat.use_backface_culling = True

        # KHR_materials_unlit
        if gltf_mat.extensions and "KHR_materials_unlit" in gltf_mat.extensions:
            gltf_props = getattr(mat, "gltf_props", None)
            if gltf_props:
                gltf_props.unlit = True
            # Make it look unlit in viewport (only meaningful with a top-level
            # Principled; layered materials drive their own internal BSDF).
            if principled is not None:
                principled.inputs["Metallic"].default_value = 0.0
                principled.inputs["Roughness"].default_value = 1.0
                if "Specular IOR Level" in principled.inputs:
                    principled.inputs["Specular IOR Level"].default_value = 0.0

        return mat

    def _apply_pbr(self, tree, principled, pbr, gltf_mat=None, mat=None) -> None:
        if pbr.base_color_factor:
            principled.inputs["Base Color"].default_value = tuple(pbr.base_color_factor[:4])
            if len(pbr.base_color_factor) > 3:
                principled.inputs["Alpha"].default_value = pbr.base_color_factor[3]

        if pbr.metallic_factor is not None:
            principled.inputs["Metallic"].default_value = pbr.metallic_factor

        if pbr.roughness_factor is not None:
            principled.inputs["Roughness"].default_value = pbr.roughness_factor

        if pbr.base_color_texture:
            tex_node = self._apply_texture(
                tree, principled, "Base Color", pbr.base_color_texture, y_offset=0,
            )
            self._apply_base_color_alpha(tree, principled, tex_node, pbr, gltf_mat)

        if pbr.metallic_roughness_texture:
            self._apply_metallic_roughness_texture(tree, principled, pbr, gltf_mat, mat)

    def _apply_base_color_alpha(self, tree, principled, tex_node, pbr, gltf_mat) -> None:
        """Wire the base color texture's Alpha into Principled.Alpha.

        Without this a MASK or BLEND material got its `surface_render_method`
        set and nothing to drive it: Principled.Alpha stayed at
        `baseColorFactor[3]`, so the mesh drew fully opaque and the cut-out parts
        of the texture showed as **black** -- Blender premultiplies a straight-
        alpha image on load, so the RGB under a zero alpha is multiplied away
        before it ever reaches the Color output. Foliage and netting exported as
        alpha-masked quads arrived as solid black cards.

        Two halves, and both are needed:

        - the link, so the alpha the file carries actually reaches the shader;
        - `alpha_mode = 'CHANNEL_PACKED'` on the image, so Blender stops
          premultiplying it. glTF's base color alpha is a coverage mask sitting
          beside an independent color, which is exactly what CHANNEL_PACKED
          means. It also stops the dark fringe that premultiplied black bleeds
          into the kept texels at every mip level.

        glTF defines the final alpha as `baseColorFactor.a * texture.a`, and
        `_wire_channel_factor` already spells that: a factor of 1 links straight
        through, anything else goes through a MULTIPLY.

        MASK additionally means "fully on or fully off at `alphaCutoff`", which
        `surface_render_method` alone does not say -- Blender 4.2 dropped the
        CLIP method and `alpha_threshold` with it -- so the comparison is
        rebuilt as a GREATER_THAN node.
        """
        if tex_node is None:
            return
        alpha_out = tex_node.outputs.get("Alpha")
        if alpha_out is None or "Alpha" not in principled.inputs:
            return

        mode = getattr(gltf_mat, "alpha_mode", None)
        if mode not in ("BLEND", "MASK"):
            return

        # Straight alpha, not premultiplied -- see the docstring.
        if tex_node.image is not None:
            try:
                tex_node.image.alpha_mode = "CHANNEL_PACKED"
            except (AttributeError, TypeError, ValueError):
                pass

        factor = pbr.base_color_factor[3] if (
            pbr.base_color_factor and len(pbr.base_color_factor) > 3
        ) else 1.0

        target = principled.inputs["Alpha"]
        if mode == "MASK":
            cutoff = gltf_mat.alpha_cutoff
            cutoff = 0.5 if cutoff is None else float(cutoff)
            cut = tree.nodes.new("ShaderNodeMath")
            cut.operation = "GREATER_THAN"
            cut.inputs[1].default_value = cutoff
            cut.location = (principled.location[0] - 200, principled.location[1] - 100)
            tree.links.new(cut.outputs["Value"], target)
            target = cut.inputs[0]

        self._wire_channel_factor(tree, alpha_out, target, factor)

    def _apply_metallic_roughness_texture(self, tree, principled, pbr, gltf_mat=None, mat=None) -> None:
        """glTF packs roughness in the G channel and metallic in the B channel of
        a single combined texture. Split it: Green -> Roughness, Blue -> Metallic,
        honoring the scalar factors (final = factor * sampled_channel).

        (Previously the texture's whole Color output was wired straight into the
        Metallic socket, which dumped a plain roughness map onto Metallic and left
        Roughness unconnected.)
        """
        tex_node = self._make_texture_node(
            tree, self._ti_to_dict(pbr.metallic_roughness_texture),
            principled, (-700, -200), non_color=True,
        )
        if tex_node is None:
            return

        sep = tree.nodes.new("ShaderNodeSeparateColor")
        sep.location = (principled.location[0] - 400, principled.location[1] - 200)
        tree.links.new(tex_node.outputs["Color"], sep.inputs["Color"])

        self._wire_channel_factor(
            tree, sep.outputs["Green"], principled.inputs["Roughness"],
            pbr.roughness_factor,
        )
        self._wire_channel_factor(
            tree, sep.outputs["Blue"], principled.inputs["Metallic"],
            pbr.metallic_factor,
        )

        # Reinterpret the packed R (occlusion) and A (alpha) channels of the
        # same image, and record the layout on gltf_props for a lossless
        # re-export.
        if gltf_mat is not None and mat is not None:
            self._apply_packed_channels(tree, principled, mat, gltf_mat, tex_node, pbr)

    def _apply_packed_channels(self, tree, principled, mat, gltf_mat, tex_node, pbr) -> None:
        """Handle the non-standard channels of a hand-packed ORM(A) texture:
        occlusion (R) is recorded for round-trip (preserve-only, no viewport
        effect, matching the official glTF importer); base alpha packed in the
        Alpha channel (CUSTOM_packed_texture) is wired into Principled.Alpha.
        Roughness (G) / Metallic (B) are already split by the caller.
        """
        gltf_props = getattr(mat, "gltf_props", None)
        mr_index = pbr.metallic_roughness_texture.index

        # Occlusion sharing the MR image -> spec ORM. Preserve-only: the R
        # channel round-trips via gltf_props without altering viewport shading.
        occ = gltf_mat.occlusion_texture
        if occ is not None and getattr(occ, "index", None) == mr_index:
            if gltf_props is not None:
                gltf_props.packed_r_channel = "OCCLUSION"
                if getattr(occ, "strength", None) is not None:
                    gltf_props.occlusion_strength = float(occ.strength)

        # Base alpha packed in the MR texture's Alpha channel.
        ext = gltf_mat.extensions or {}
        packed = ext.get(EXT_PACKED_TEXTURE)
        if packed and packed.get("alphaChannel") == "metallicRoughness":
            alpha_out = tex_node.outputs.get("Alpha")
            if alpha_out is not None:
                tree.links.new(alpha_out, principled.inputs["Alpha"])
            if gltf_props is not None:
                gltf_props.packed_alpha = True

    def _wire_channel_factor(self, tree, from_socket, to_socket, factor) -> None:
        """Link a separated metallic/roughness channel into `to_socket`,
        applying the glTF scalar factor (final = factor * channel). A factor of
        0 zeroes the channel, so the socket is left at 0 and nothing is wired; a
        factor of 1 (or unspecified) links the channel directly; otherwise a
        Math MULTIPLY node scales it.
        """
        f = 1.0 if factor is None else float(factor)
        if f == 0.0:
            try:
                to_socket.default_value = 0.0
            except (TypeError, ValueError):
                pass
            return
        if f == 1.0:
            tree.links.new(from_socket, to_socket)
            return
        mul = tree.nodes.new("ShaderNodeMath")
        mul.operation = "MULTIPLY"
        mul.inputs[1].default_value = f
        tree.links.new(from_socket, mul.inputs[0])
        tree.links.new(mul.outputs["Value"], to_socket)

    def _apply_texture(self, tree, principled, socket_name, texture_info, y_offset=0):
        """Wire a color texture into `socket_name`. Builds the Image Texture
        node (with sampler + KHR_texture_transform) via _make_texture_node so
        the node-creation path is uniform across all texture slots.

        Returns the Image Texture node, so a caller that needs the image's other
        outputs -- the base color slot needs Alpha -- does not build it twice.
        """
        tex_node = self._make_texture_node(
            tree, self._ti_to_dict(texture_info), principled, (-400, y_offset),
        )
        if tex_node is None:
            return None
        tree.links.new(tex_node.outputs["Color"], principled.inputs[socket_name])
        return tex_node

    def _apply_normal_texture(self, tree, principled, normal_info) -> None:
        """Wire a normal texture through a Normal Map node into Principled.Normal.
        Built on _make_texture_node so the sampler + KHR_texture_transform block
        is handled uniformly (it was previously missing here)."""
        tex_node = self._make_texture_node(
            tree, self._ti_to_dict(normal_info), principled, (-600, -600),
            non_color=True,
        )
        if tex_node is None:
            return

        normal_map = tree.nodes.new("ShaderNodeNormalMap")
        normal_map.location = (-300, -600)
        scale = getattr(normal_info, "scale", None)
        if scale is not None:
            normal_map.inputs["Strength"].default_value = scale

        tree.links.new(tex_node.outputs["Color"], normal_map.inputs["Color"])
        tree.links.new(normal_map.outputs["Normal"], principled.inputs["Normal"])

    def _apply_principled_bump(self, tree, principled, normal_info, base) -> None:
        """Rebuild a Bump node feeding Principled.Normal: Height <- the
        displacement map, Normal <- the normal map (via a Normal Map node).
        """
        bump = tree.nodes.new("ShaderNodeBump")
        bump.location = (-300, -550)
        bd = base.get("bump") or {}
        if bd.get("strength") is not None:
            bump.inputs["Strength"].default_value = float(bd["strength"])
        if bd.get("distance") is not None:
            bump.inputs["Distance"].default_value = float(bd["distance"])

        height = base.get("heightTexture")
        if height:
            htex = self._make_texture_node(
                tree, self._ti_to_dict(height), principled, (-750, -550),
                non_color=True,
            )
            if htex is not None:
                tree.links.new(htex.outputs["Color"], bump.inputs["Height"])

        if normal_info is not None:
            ntex = self._make_texture_node(
                tree, self._ti_to_dict(normal_info), principled, (-750, -800),
                non_color=True,
            )
            if ntex is not None:
                normal_map = tree.nodes.new("ShaderNodeNormalMap")
                normal_map.location = (-500, -800)
                scale = getattr(normal_info, "scale", None)
                if scale is not None:
                    normal_map.inputs["Strength"].default_value = float(scale)
                tree.links.new(ntex.outputs["Color"], normal_map.inputs["Color"])
                tree.links.new(normal_map.outputs["Normal"], bump.inputs["Normal"])

        tree.links.new(bump.outputs["Normal"], principled.inputs["Normal"])

    def _apply_clearcoat(self, tree, principled, extensions: dict) -> None:
        """KHR_materials_clearcoat -> Principled Coat. Inverse of the exporter:
        clearcoatFactor -> Coat Weight, clearcoatRoughnessFactor -> Coat
        Roughness, with textures wired through their glTF channels (clearcoat in
        R, clearcoat-roughness in G) honoring the scalar factors. glTF clearcoat
        has no IOR/tint, so those Blender inputs keep their defaults.
        """
        cc = extensions.get("KHR_materials_clearcoat")
        if not cc or "Coat Weight" not in principled.inputs:
            return
        factor = cc.get("clearcoatFactor")
        rough = cc.get("clearcoatRoughnessFactor")

        cc_tex = cc.get("clearcoatTexture")
        if cc_tex:
            node = self._make_texture_node(tree, cc_tex, principled, (-900, 250), non_color=True)
            if node is not None:
                sep = tree.nodes.new("ShaderNodeSeparateColor")
                sep.location = (principled.location[0] - 700, principled.location[1] + 250)
                tree.links.new(node.outputs["Color"], sep.inputs["Color"])
                self._wire_channel_factor(
                    tree, sep.outputs["Red"], principled.inputs["Coat Weight"], factor,
                )
        elif factor is not None:
            principled.inputs["Coat Weight"].default_value = float(factor)

        if "Coat Roughness" in principled.inputs:
            cr_tex = cc.get("clearcoatRoughnessTexture")
            if cr_tex:
                node = self._make_texture_node(tree, cr_tex, principled, (-900, 130), non_color=True)
                if node is not None:
                    sep = tree.nodes.new("ShaderNodeSeparateColor")
                    sep.location = (principled.location[0] - 700, principled.location[1] + 130)
                    tree.links.new(node.outputs["Color"], sep.inputs["Color"])
                    self._wire_channel_factor(
                        tree, sep.outputs["Green"], principled.inputs["Coat Roughness"], rough,
                    )
            elif rough is not None:
                principled.inputs["Coat Roughness"].default_value = float(rough)

        cn_tex = cc.get("clearcoatNormalTexture")
        if cn_tex and "Coat Normal" in principled.inputs:
            node = self._make_texture_node(tree, cn_tex, principled, (-900, -50), non_color=True)
            if node is not None:
                nm = tree.nodes.new("ShaderNodeNormalMap")
                nm.location = (principled.location[0] - 650, principled.location[1] - 50)
                scale = cn_tex.get("scale") if isinstance(cn_tex, dict) else None
                if scale is not None:
                    nm.inputs["Strength"].default_value = float(scale)
                tree.links.new(node.outputs["Color"], nm.inputs["Color"])
                tree.links.new(nm.outputs["Normal"], principled.inputs["Coat Normal"])

    def _apply_sheen(self, tree, principled, extensions: dict) -> None:
        """KHR_materials_sheen -> Principled Sheen. glTF stores a single sheen
        *color*; Blender splits it into Sheen Weight (scalar) x Sheen Tint
        (color), so the brightest channel is recovered as the weight and the
        normalized color as the tint (mirrors the exporter's tint*weight fold).
        sheenRoughnessFactor -> Sheen Roughness; the roughness texture's value
        lives in its Alpha channel per the glTF spec.
        """
        sh = extensions.get("KHR_materials_sheen")
        if not sh or "Sheen Weight" not in principled.inputs:
            return
        color = sh.get("sheenColorFactor")
        rough = sh.get("sheenRoughnessFactor")
        color_tex = sh.get("sheenColorTexture")
        rough_tex = sh.get("sheenRoughnessTexture")

        weight = 1.0
        if color and len(color) >= 3:
            peak = max(float(color[0]), float(color[1]), float(color[2]))
            if peak > 0.0:
                weight = peak
                if "Sheen Tint" in principled.inputs and not color_tex:
                    principled.inputs["Sheen Tint"].default_value = (
                        color[0] / peak, color[1] / peak, color[2] / peak, 1.0,
                    )
            else:
                weight = 0.0
        principled.inputs["Sheen Weight"].default_value = weight

        if rough is not None and "Sheen Roughness" in principled.inputs:
            principled.inputs["Sheen Roughness"].default_value = float(rough)

        if color_tex and "Sheen Tint" in principled.inputs:
            node = self._make_texture_node(tree, color_tex, principled, (-900, -200))
            if node is not None:
                tree.links.new(node.outputs["Color"], principled.inputs["Sheen Tint"])
                if weight == 0.0:
                    principled.inputs["Sheen Weight"].default_value = 1.0

        if rough_tex and "Sheen Roughness" in principled.inputs:
            node = self._make_texture_node(tree, rough_tex, principled, (-900, -320), non_color=True)
            if node is not None:
                # glTF carries sheen roughness in the texture's Alpha channel.
                tree.links.new(node.outputs["Alpha"], principled.inputs["Sheen Roughness"])

    def _apply_texture_transform(self, tree, tex_node, texture_info) -> None:
        """Create Mapping + Texture Coordinate nodes for KHR_texture_transform,
        extracting the transform dict off a TextureInfo dataclass and delegating
        to the dict-based implementation."""
        extensions = getattr(texture_info, "extensions", None)
        if not extensions:
            return
        transform = extensions.get("KHR_texture_transform")
        if transform is None:
            return
        self._apply_texture_transform_dict(tree, tex_node, transform)

    # ------------------------------------------------------------------
    # CUSTOM_materials_layers — rebuild a BSDFStackNode
    # ------------------------------------------------------------------

    def _build_bsdf_stack(self, tree, output, gltf_mat, ext: dict) -> None:
        """Rebuild a BSDFStackNode from the extension. Layer 0 is the base
        material; layers 1..N come from ext['layers']. The node's BSDF output
        drives Material Output.Surface so the blend is visible in the viewport.
        """
        ext_layers = ext.get("layers") or []
        total = 1 + len(ext_layers)

        node = tree.nodes.new(BSDF_STACK_NODE_IDNAME)
        node.location = (0, 0)

        # node.init() already created exactly one layer plus the internal tree.
        # Grow to `total`, then rebuild the whole socket interface once. These
        # rebuilds run synchronously on the main thread — that is safe here (the
        # deferred app-timer path in bsdf_node.py only guards live UI edits).
        for _j in range(1, total):
            node.layers.add()
        node.rebuild_group(old_to_new=None)

        layer_dicts = [self._base_layer_dict(gltf_mat, ext)] + list(ext_layers)
        for i, ld in enumerate(layer_dicts):
            self._populate_stack_layer(tree, node, i, ld, is_base=(i == 0))

        # Final rebuild: wire normal-map mix branches (only created when the
        # outer Normal socket is_linked) and fold opacity/enabled/blend into
        # the per-channel mix factors.
        node.rebuild_internals()

        tree.links.new(node.outputs["BSDF"], output.inputs["Surface"])

    def _base_layer_dict(self, gltf_mat, ext: dict | None = None) -> dict:
        """Normalize the base material's fields into the same dict shape used by
        extension layers, so _populate_stack_layer can treat layer 0 uniformly.
        Base-only extras with no core glTF slot (heightTexture/bump) live in
        extension.base.
        """
        ld: dict = {}
        base_extra = (ext or {}).get("base") or {}
        if base_extra.get("heightTexture") is not None:
            ld["heightTexture"] = base_extra["heightTexture"]
        if base_extra.get("bump") is not None:
            ld["bump"] = base_extra["bump"]
        pbr = gltf_mat.pbr_metallic_roughness
        if pbr is not None:
            pdict: dict = {}
            if pbr.base_color_factor:
                pdict["baseColorFactor"] = list(pbr.base_color_factor)
            bct = self._ti_to_dict(pbr.base_color_texture)
            if bct is not None:
                pdict["baseColorTexture"] = bct
            if pbr.metallic_factor is not None:
                pdict["metallicFactor"] = pbr.metallic_factor
            if pbr.roughness_factor is not None:
                pdict["roughnessFactor"] = pbr.roughness_factor
            mrt = self._ti_to_dict(pbr.metallic_roughness_texture)
            if mrt is not None:
                pdict["metallicRoughnessTexture"] = mrt
            if pdict:
                ld["pbrMetallicRoughness"] = pdict
        nt = self._ti_to_dict(gltf_mat.normal_texture)
        if nt is not None:
            ld["normalTexture"] = nt
        if gltf_mat.emissive_factor:
            ld["emissiveFactor"] = list(gltf_mat.emissive_factor)
        et = self._ti_to_dict(gltf_mat.emissive_texture)
        if et is not None:
            ld["emissiveTexture"] = et
        return ld

    @staticmethod
    def _split_emissive(emissive_factor, strength_mult=1.0):
        """Split a glTF emissiveFactor into a Blender (Emission Color rgba,
        Emission Strength) pair. glTF folds intensity into the factor (which is
        clamped <= 1 on export), so we factor the brightest channel back out as
        the strength. KHR_materials_emissive_strength carries the part of the
        intensity that exceeded 1; `strength_mult` (its emissiveStrength) scales
        the recovered strength so HDR emission is restored.

        Returns (color_rgba, strength) or None when there is no emission.
        """
        if not emissive_factor or len(emissive_factor) < 3:
            return None
        r, g, b = emissive_factor[0], emissive_factor[1], emissive_factor[2]
        peak = max(r, g, b)
        if peak > 0:
            color = (r / peak, g / peak, b / peak, 1.0)
            return color, peak * strength_mult
        # All-zero factor: no light, but preserve the (zero) color.
        return (r, g, b, 1.0), 0.0

    @staticmethod
    def _emissive_strength_mult(gltf_mat) -> float:
        """KHR_materials_emissive_strength.emissiveStrength multiplier (1.0 when
        the extension is absent)."""
        ext = gltf_mat.extensions or {}
        es = ext.get("KHR_materials_emissive_strength")
        if es and es.get("emissiveStrength") is not None:
            return float(es["emissiveStrength"])
        return 1.0

    @staticmethod
    def _ti_to_dict(ti):
        """Coerce a TextureInfo/NormalTextureInfo dataclass (or dict) to a
        plain textureInfo dict that _make_texture_node understands.
        """
        if ti is None:
            return None
        if isinstance(ti, dict):
            return ti
        d = {"index": ti.index}
        if getattr(ti, "tex_coord", None) is not None:
            d["texCoord"] = ti.tex_coord
        if getattr(ti, "scale", None) is not None:
            d["scale"] = ti.scale
        if getattr(ti, "extensions", None):
            d["extensions"] = ti.extensions
        return d

    def _stack_socket(self, node, i, channel_name):
        idx = i * N_CH + CH_TO_IDX[channel_name]
        if 0 <= idx < len(node.inputs):
            return node.inputs[idx]
        return None

    def _populate_stack_layer(self, tree, node, i, layer, is_base) -> None:
        pbr = layer.get("pbrMetallicRoughness") or {}

        bcf = pbr.get("baseColorFactor")
        if bcf:
            color_sock = self._stack_socket(node, i, "Color")
            if color_sock is not None and len(bcf) >= 3:
                color_sock.default_value = (bcf[0], bcf[1], bcf[2], 1.0)
            if len(bcf) >= 4:
                alpha_sock = self._stack_socket(node, i, "Alpha")
                if alpha_sock is not None:
                    alpha_sock.default_value = float(bcf[3])

        mf = pbr.get("metallicFactor")
        if mf is not None:
            s = self._stack_socket(node, i, "Metallic")
            if s is not None:
                s.default_value = float(mf)
        rf = pbr.get("roughnessFactor")
        if rf is not None:
            s = self._stack_socket(node, i, "Roughness")
            if s is not None:
                s.default_value = float(rf)

        self._link_stack_texture(
            tree, node, i, "Color", pbr.get("baseColorTexture"), (-500, 0),
        )
        self._link_stack_metallic_roughness(
            tree, node, i, pbr.get("metallicRoughnessTexture"), mf, rf, (-500, -200),
        )
        self._link_stack_normal(
            tree, node, i, layer.get("normalTexture"),
            layer.get("heightTexture"), layer.get("bump"), (-500, -400),
        )

        # Layer emission: the exporter bakes full intensity into emissiveFactor
        # (no per-layer KHR_materials_emissive_strength), so split with mult=1.
        split = self._split_emissive(layer.get("emissiveFactor"))
        if split is not None:
            color, strength = split
            ec = self._stack_socket(node, i, "Emission Color")
            es = self._stack_socket(node, i, "Emission Strength")
            if ec is not None:
                ec.default_value = color
            if es is not None:
                es.default_value = strength
        self._link_stack_texture(
            tree, node, i, "Emission Color", layer.get("emissiveTexture"), (-500, -600),
        )

        ss = layer.get("subsurface")
        if ss:
            w = self._stack_socket(node, i, "Subsurface Weight")
            if w is not None and ss.get("weight") is not None:
                w.default_value = float(ss["weight"])
            radius = ss.get("radius")
            r = self._stack_socket(node, i, "Subsurface Radius")
            if r is not None and radius and len(radius) >= 3:
                r.default_value = (radius[0], radius[1], radius[2])

        if not is_base:
            self._link_stack_mask(
                tree, node, i, layer.get("mask"), (-500, -800),
            )

        # Per-layer metadata on the StackLayerProperties entry.
        if i < len(node.layers):
            props = node.layers[i]
            name = layer.get("name")
            if name:
                props.layer_name = name
            if not is_base:
                blend = layer.get("blendMode")
                if blend and str(blend).upper() in _VALID_BLEND_MODES:
                    props.blend_mode = str(blend).upper()
                if layer.get("opacity") is not None:
                    props.opacity = float(layer["opacity"])
                if layer.get("enabled") is not None:
                    props.enabled = bool(layer["enabled"])

    def _link_stack_texture(self, tree, node, i, channel_name, tex_dict, offset) -> None:
        if not tex_dict:
            return
        tex_node = self._make_texture_node(tree, tex_dict, node, offset)
        if tex_node is None:
            return
        socket = self._stack_socket(node, i, channel_name)
        if socket is not None:
            tree.links.new(tex_node.outputs["Color"], socket)

    def _link_stack_metallic_roughness(
        self, tree, node, i, tex_dict, mf, rf, offset,
    ) -> None:
        """Split a combined metallic-roughness texture into the layer's separate
        Roughness (green channel) and Metallic (blue channel) sockets, honoring
        the scalar factors. Mirrors _apply_metallic_roughness_texture for the
        BSDFStackNode path.
        """
        if not tex_dict:
            return
        tex_node = self._make_texture_node(tree, tex_dict, node, offset, non_color=True)
        if tex_node is None:
            return

        sep = tree.nodes.new("ShaderNodeSeparateColor")
        sep.location = (node.location[0] + offset[0] + 200, node.location[1] + offset[1])
        tree.links.new(tex_node.outputs["Color"], sep.inputs["Color"])

        rough_sock = self._stack_socket(node, i, "Roughness")
        if rough_sock is not None:
            self._wire_channel_factor(tree, sep.outputs["Green"], rough_sock, rf)
        metal_sock = self._stack_socket(node, i, "Metallic")
        if metal_sock is not None:
            self._wire_channel_factor(tree, sep.outputs["Blue"], metal_sock, mf)

    def _link_stack_normal(
        self, tree, node, i, normal_dict, height_dict, bump, offset,
    ) -> None:
        """Wire layer i's Normal socket. A normal map flows through a Normal Map
        node; when height/bump data exists it is rebuilt as a Bump node (Height
        <- depth map, Normal <- the normal map), mirroring the exported graph.
        """
        socket = self._stack_socket(node, i, "Normal")
        if socket is None:
            return

        normal_out = None
        if normal_dict:
            tex_node = self._make_texture_node(
                tree, normal_dict, node, offset, non_color=True,
            )
            if tex_node is not None:
                normal_map = tree.nodes.new("ShaderNodeNormalMap")
                normal_map.location = (
                    node.location[0] + offset[0] + 200,
                    node.location[1] + offset[1],
                )
                scale = normal_dict.get("scale")
                if scale is not None:
                    normal_map.inputs["Strength"].default_value = float(scale)
                tree.links.new(tex_node.outputs["Color"], normal_map.inputs["Color"])
                normal_out = normal_map.outputs["Normal"]

        if height_dict or bump:
            # Rebuild the Bump node that combined the depth map with the normal.
            bump_node = tree.nodes.new("ShaderNodeBump")
            bump_node.location = (
                node.location[0] + offset[0] + 400,
                node.location[1] + offset[1],
            )
            if bump:
                if bump.get("strength") is not None:
                    bump_node.inputs["Strength"].default_value = float(bump["strength"])
                if bump.get("distance") is not None:
                    bump_node.inputs["Distance"].default_value = float(bump["distance"])
            height_node = self._make_texture_node(
                tree, height_dict, node, (offset[0], offset[1] - 200), non_color=True,
            ) if height_dict else None
            if height_node is not None:
                tree.links.new(height_node.outputs["Color"], bump_node.inputs["Height"])
            if normal_out is not None:
                tree.links.new(normal_out, bump_node.inputs["Normal"])
            tree.links.new(bump_node.outputs["Normal"], socket)
            return

        if normal_out is not None:
            tree.links.new(normal_out, socket)

    def _link_stack_mask(self, tree, node, i, mask, offset) -> None:
        if not mask:
            return
        socket = self._stack_socket(node, i, "Mask")
        if socket is None:
            return

        source = (mask.get("source") or "TEXTURE").upper()
        channel = (mask.get("channel") or "R").upper()
        out_socket_name = "Alpha" if channel == "A" else "Color"
        sep_loc = (node.location[0] + offset[0] + 200, node.location[1] + offset[1])

        if source == "TEXTURE":
            tex = mask.get("texture")
            tex_node = self._make_texture_node(
                tree, tex, node, offset, non_color=True,
            )
            if tex_node is None:
                return
            from_socket = (
                tex_node.outputs.get(out_socket_name)
                or tex_node.outputs["Color"]
            )
            if channel in ("G", "B"):
                sep = tree.nodes.new("ShaderNodeSeparateColor")
                sep.location = sep_loc
                tree.links.new(tex_node.outputs["Color"], sep.inputs["Color"])
                ch_socket = sep.outputs.get(
                    {"R": "Red", "G": "Green", "B": "Blue"}[channel]
                )
                if ch_socket is not None:
                    tree.links.new(ch_socket, socket)
                    return
            tree.links.new(from_socket, socket)
            return

        if source == "VERTEX_COLOR":
            attr_name = mask.get("attribute") or "COLOR_0"
            vc = tree.nodes.new("ShaderNodeVertexColor")
            vc.layer_name = attr_name
            vc.location = (node.location[0] + offset[0], node.location[1] + offset[1])
            from_socket = (
                vc.outputs["Alpha"] if channel == "A" else vc.outputs["Color"]
            )
            if channel in ("G", "B"):
                sep = tree.nodes.new("ShaderNodeSeparateColor")
                sep.location = sep_loc
                tree.links.new(vc.outputs["Color"], sep.inputs["Color"])
                ch_socket = sep.outputs.get(
                    {"R": "Red", "G": "Green", "B": "Blue"}[channel]
                )
                if ch_socket is not None:
                    tree.links.new(ch_socket, socket)
                    return
            tree.links.new(from_socket, socket)

    def _make_texture_node(
        self, tree, tex_dict, anchor_node, offset, non_color: bool = False,
    ):
        """Create an Image Texture node from a textureInfo-shaped dict."""
        if not tex_dict or self.gltf.textures is None:
            return None
        idx = tex_dict.get("index")
        if idx is None or idx >= len(self.gltf.textures):
            return None
        gltf_tex = self.gltf.textures[idx]
        source = gltf_tex.source
        if source is None and gltf_tex.extensions:
            # KHR_texture_basisu keeps the KTX2 image in the extension block.
            source = (gltf_tex.extensions.get("KHR_texture_basisu") or {}).get("source")
        if source is None:
            return None
        img = self.texture_importer.get_blender_image(source)
        if img is None:
            return None

        tex_node = tree.nodes.new("ShaderNodeTexImage")
        tex_node.image = img
        # Only force Non-Color when the image didn't carry an explicit
        # colorspace hint on export; otherwise we'd clobber the round-tripped
        # setting and corrupt images shared between color and non-color slots.
        if non_color and source not in self.texture_importer.hinted_images:
            tex_node.image.colorspace_settings.name = "Non-Color"
        tex_node.location = (
            anchor_node.location[0] + offset[0],
            anchor_node.location[1] + offset[1],
        )

        if gltf_tex.sampler is not None and self.gltf.samplers:
            sampler = self.gltf.samplers[gltf_tex.sampler]
            if sampler.mag_filter == TextureFilter.NEAREST:
                tex_node.interpolation = "Closest"
            else:
                tex_node.interpolation = "Linear"
            if sampler.wrap_s == TextureWrap.CLAMP_TO_EDGE:
                tex_node.extension = "EXTEND"
            else:
                tex_node.extension = "REPEAT"

        # Apply KHR_texture_transform if present
        ext = tex_dict.get("extensions") or {}
        transform = ext.get("KHR_texture_transform")
        if transform:
            self._apply_texture_transform_dict(tree, tex_node, transform)

        return tex_node

    def _apply_texture_transform_dict(self, tree, tex_node, transform: dict) -> None:
        offset = transform.get("offset", [0.0, 0.0])
        rotation = transform.get("rotation", 0.0)
        scale = transform.get("scale", [1.0, 1.0])

        bl_offset_x = offset[0]
        bl_offset_y = 1.0 - scale[1] - offset[1]
        bl_rotation = -rotation
        bl_scale_x = scale[0]
        bl_scale_y = scale[1]

        tex_x = tex_node.location[0]
        tex_y = tex_node.location[1]

        mapping = tree.nodes.new("ShaderNodeMapping")
        mapping.location = (tex_x - 200, tex_y)
        mapping.inputs["Location"].default_value = (bl_offset_x, bl_offset_y, 0.0)
        mapping.inputs["Rotation"].default_value = (0.0, 0.0, bl_rotation)
        mapping.inputs["Scale"].default_value = (bl_scale_x, bl_scale_y, 1.0)

        tex_coord = tree.nodes.new("ShaderNodeTexCoord")
        tex_coord.location = (tex_x - 400, tex_y)

        tree.links.new(tex_coord.outputs["UV"], mapping.inputs["Vector"])
        tree.links.new(mapping.outputs["Vector"], tex_node.inputs["Vector"])
