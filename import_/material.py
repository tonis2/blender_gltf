from __future__ import annotations

from typing import TYPE_CHECKING

from ..gltf.constants import TextureFilter, TextureWrap

if TYPE_CHECKING:
    import bpy
    from ..gltf.types import Gltf, TextureInfo, NormalTextureInfo
    from .texture import TextureImporter
    from ..importer import ImportSettings


EXT_MATERIALS_LAYERS = "CUSTOM_materials_layers"
BSDF_STACK_NODE_IDNAME = "BSDFStackNodeType"

# BSDFStackNode per-layer input layout — keep in sync with
# layer_node/bsdf_node.py CHANNELS and export/material.py.
_N_CH = 10
_CH = {
    "Color": 0, "Mask": 1, "Normal": 2, "Roughness": 3, "Metallic": 4,
    "Alpha": 5, "Emission Color": 6, "Emission Strength": 7,
    "Subsurface Weight": 8, "Subsurface Radius": 9,
}
_VALID_BLEND_MODES = {
    "MIX", "MULTIPLY", "ADD", "SUBTRACT", "SCREEN", "OVERLAY",
    "SOFT_LIGHT", "DIFFERENCE", "DARKEN", "LIGHTEN",
}


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
                self._apply_pbr(tree, principled, pbr)

            if base and (base.get("heightTexture") or base.get("bump")):
                # Rebuild the Bump node (Height <- displacement, Normal <- the
                # normal map) feeding the Principled Normal socket.
                self._apply_principled_bump(
                    tree, principled, gltf_mat.normal_texture, base,
                )
            elif gltf_mat.normal_texture:
                self._apply_normal_texture(tree, principled, gltf_mat.normal_texture)

            if gltf_mat.emissive_factor:
                r, g, b = gltf_mat.emissive_factor
                strength = max(r, g, b)
                if strength > 0:
                    principled.inputs["Emission Color"].default_value = (
                        r / strength, g / strength, b / strength, 1.0,
                    )
                    principled.inputs["Emission Strength"].default_value = strength

            if gltf_mat.emissive_texture:
                self._apply_texture(tree, principled, "Emission Color", gltf_mat.emissive_texture, y_offset=-400)

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

    def _apply_pbr(self, tree, principled, pbr) -> None:
        if pbr.base_color_factor:
            principled.inputs["Base Color"].default_value = tuple(pbr.base_color_factor[:4])
            if len(pbr.base_color_factor) > 3:
                principled.inputs["Alpha"].default_value = pbr.base_color_factor[3]

        if pbr.metallic_factor is not None:
            principled.inputs["Metallic"].default_value = pbr.metallic_factor

        if pbr.roughness_factor is not None:
            principled.inputs["Roughness"].default_value = pbr.roughness_factor

        if pbr.base_color_texture:
            self._apply_texture(tree, principled, "Base Color", pbr.base_color_texture, y_offset=0)

        if pbr.metallic_roughness_texture:
            self._apply_texture(tree, principled, "Metallic", pbr.metallic_roughness_texture, y_offset=-200)

    def _apply_texture(self, tree, principled, socket_name, texture_info, y_offset=0) -> None:
        if self.gltf.textures is None:
            return
        tex_index = texture_info.index
        if tex_index >= len(self.gltf.textures):
            return
        gltf_texture = self.gltf.textures[tex_index]
        if gltf_texture.source is None:
            return

        img = self.texture_importer.get_blender_image(gltf_texture.source)
        if img is None:
            return

        tex_node = tree.nodes.new("ShaderNodeTexImage")
        tex_node.image = img
        tex_node.location = (-400, y_offset)

        if gltf_texture.sampler is not None and self.gltf.samplers:
            sampler = self.gltf.samplers[gltf_texture.sampler]
            if sampler.mag_filter == TextureFilter.NEAREST:
                tex_node.interpolation = "Closest"
            else:
                tex_node.interpolation = "Linear"
            if sampler.wrap_s == TextureWrap.CLAMP_TO_EDGE:
                tex_node.extension = "EXTEND"
            else:
                tex_node.extension = "REPEAT"

        self._apply_texture_transform(tree, tex_node, texture_info)

        tree.links.new(tex_node.outputs["Color"], principled.inputs[socket_name])

    def _apply_normal_texture(self, tree, principled, normal_info) -> None:
        if self.gltf.textures is None:
            return
        tex_index = normal_info.index
        if tex_index >= len(self.gltf.textures):
            return
        gltf_texture = self.gltf.textures[tex_index]
        if gltf_texture.source is None:
            return

        img = self.texture_importer.get_blender_image(gltf_texture.source)
        if img is None:
            return

        tex_node = tree.nodes.new("ShaderNodeTexImage")
        tex_node.image = img
        tex_node.image.colorspace_settings.name = "Non-Color"
        tex_node.location = (-600, -600)

        normal_map = tree.nodes.new("ShaderNodeNormalMap")
        normal_map.location = (-300, -600)
        if normal_info.scale is not None:
            normal_map.inputs["Strength"].default_value = normal_info.scale

        self._apply_texture_transform(tree, tex_node, normal_info)

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

    def _apply_texture_transform(self, tree, tex_node, texture_info) -> None:
        """Create Mapping + Texture Coordinate nodes for KHR_texture_transform."""
        if not hasattr(texture_info, "extensions") or not texture_info.extensions:
            return
        transform = texture_info.extensions.get("KHR_texture_transform")
        if transform is None:
            return

        offset = transform.get("offset", [0.0, 0.0])
        rotation = transform.get("rotation", 0.0)
        scale = transform.get("scale", [1.0, 1.0])

        # Convert glTF UV space back to Blender UV space (V is flipped)
        # offset_y_blender = 1 - scale_y_gltf - offset_y_gltf
        # rotation_blender = -rotation_gltf
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
        for j in range(1, total):
            layer = node.layers.add()
            layer.layer_index = j
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
        idx = i * _N_CH + _CH[channel_name]
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
        self._link_stack_texture(
            tree, node, i, "Metallic", pbr.get("metallicRoughnessTexture"), (-500, -200),
        )
        self._link_stack_normal(
            tree, node, i, layer.get("normalTexture"),
            layer.get("heightTexture"), layer.get("bump"), (-500, -400),
        )

        ef = layer.get("emissiveFactor")
        if ef and len(ef) >= 3:
            strength = max(ef[0], ef[1], ef[2])
            ec = self._stack_socket(node, i, "Emission Color")
            es = self._stack_socket(node, i, "Emission Strength")
            if strength > 0:
                if ec is not None:
                    ec.default_value = (
                        ef[0] / strength, ef[1] / strength, ef[2] / strength, 1.0,
                    )
                if es is not None:
                    es.default_value = strength
            elif ec is not None:
                ec.default_value = (ef[0], ef[1], ef[2], 1.0)
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
        if gltf_tex.source is None:
            return None
        img = self.texture_importer.get_blender_image(gltf_tex.source)
        if img is None:
            return None

        tex_node = tree.nodes.new("ShaderNodeTexImage")
        tex_node.image = img
        if non_color:
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
