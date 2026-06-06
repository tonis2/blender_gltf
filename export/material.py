from __future__ import annotations

from typing import TYPE_CHECKING

from ..gltf.types import Material, MaterialPBRMetallicRoughness, NormalTextureInfo
from .texture import TextureExporter

if TYPE_CHECKING:
    import bpy
    from ..exporter import ExportSettings


EXT_MATERIALS_UNLIT = "KHR_materials_unlit"
EXT_MATERIALS_LAYERS = "CUSTOM_materials_layers"
BSDF_STACK_NODE_IDNAME = "BSDFStackNodeType"
# All Blender ShaderNodeMix blend types the BSDFStackNode exposes per layer.
_VALID_BLEND_MODES = {
    "MIX", "MULTIPLY", "ADD", "SUBTRACT", "SCREEN", "OVERLAY",
    "SOFT_LIGHT", "DIFFERENCE", "DARKEN", "LIGHTEN",
}
_VALID_MASK_CHANNELS = {"R", "G", "B", "A"}

# BSDFStackNode per-layer input layout: N_CH sockets per layer, ordered as below.
# Keep in sync with layer_node/bsdf_node.py CHANNELS.
_N_CH = 10
_CH = {
    "Color": 0, "Mask": 1, "Normal": 2, "Roughness": 3, "Metallic": 4,
    "Alpha": 5, "Emission Color": 6, "Emission Strength": 7,
    "Subsurface Weight": 8, "Subsurface Radius": 9,
}


class MaterialExporter:
    def __init__(self, texture_exporter: TextureExporter, settings: "ExportSettings") -> None:
        self.texture_exporter = texture_exporter
        self.settings = settings
        self.materials: list[Material] = []
        self._cache: dict[str, int] = {}
        self.extensions_used: set[str] = set()

    def gather(self, blender_material: "bpy.types.Material") -> int | None:
        """Export a Blender material. Returns material index or None."""
        if blender_material is None:
            return None

        if blender_material.name in self._cache:
            return self._cache[blender_material.name]

        material = self._extract(blender_material)
        index = len(self.materials)
        self.materials.append(material)
        self._cache[blender_material.name] = index
        return index

    def _extract(self, blender_material: "bpy.types.Material") -> Material:
        pbr = None
        normal_texture = None
        emissive_texture = None
        emissive_factor = None
        alpha_mode = None
        alpha_cutoff = None
        double_sided = None
        layers = None
        base_extra = None

        stack_node = self._find_bsdf_stack_node(blender_material)
        if stack_node is not None:
            # BSDFStackNode drives Material Output.Surface. Its Principled BSDF
            # is internal to the node group, so _find_principled_bsdf won't see
            # it. Layer 0 becomes the base material; layers 1..N become the
            # CUSTOM_materials_layers extension.
            (
                pbr, normal_texture, emissive_texture, emissive_factor,
                alpha_mode, alpha_cutoff, layers, base_extra,
            ) = self._extract_from_bsdf_stack(blender_material, stack_node)
        else:
            principled = self._find_principled_bsdf(blender_material)

            if principled is not None:
                pbr = self._gather_pbr(principled)
                normal_texture = self._gather_normal(principled)
                emissive_texture, emissive_factor = self._gather_emission(principled)
                alpha_mode, alpha_cutoff = self._gather_alpha(blender_material, principled)

                # A Bump node feeding Normal may carry a Height (displacement)
                # map that core glTF can't hold -> extension.base.
                h, b = self._bump_info_from_socket(principled.inputs.get("Normal"))
                if h is not None or b is not None:
                    base_extra = {}
                    if h is not None:
                        base_extra["heightTexture"] = h
                    if b is not None:
                        base_extra["bump"] = b
            else:
                # No Principled BSDF: try to recover base color + alpha from a
                # custom shader group plugged directly into Material Output.Surface
                # (e.g. tree-leaf shaders).
                pbr, alpha_mode, alpha_cutoff = self._gather_from_surface_group(blender_material)

        if blender_material.use_backface_culling is False:
            double_sided = True

        # KHR_materials_unlit
        extensions = None
        gltf_props = getattr(blender_material, "gltf_props", None)
        if gltf_props and gltf_props.unlit:
            extensions = {EXT_MATERIALS_UNLIT: {}}
            self.extensions_used.add(EXT_MATERIALS_UNLIT)

        # CUSTOM_materials_layers
        if layers or base_extra:
            if extensions is None:
                extensions = {}
            ext_dict: dict = {}
            if layers:
                ext_dict["layers"] = layers
            if base_extra:
                ext_dict["base"] = base_extra
            extensions[EXT_MATERIALS_LAYERS] = ext_dict
            self.extensions_used.add(EXT_MATERIALS_LAYERS)

        return Material(
            name=blender_material.name,
            pbr_metallic_roughness=pbr,
            normal_texture=normal_texture,
            emissive_texture=emissive_texture,
            emissive_factor=emissive_factor,
            alpha_mode=alpha_mode,
            alpha_cutoff=alpha_cutoff,
            double_sided=double_sided,
            extensions=extensions,
        )

    def _find_principled_bsdf(
        self, blender_material: "bpy.types.Material"
    ) -> "bpy.types.ShaderNodeBsdfPrincipled | None":
        if not blender_material.use_nodes or blender_material.node_tree is None:
            return None

        for node in blender_material.node_tree.nodes:
            if node.type == "BSDF_PRINCIPLED":
                return node
        return None

    def _get_socket_default(self, node: "bpy.types.ShaderNode", name: str):
        """Get the default value of a socket input."""
        socket = node.inputs.get(name)
        if socket is None:
            return None
        return socket.default_value

    def _get_connected_image_node(
        self, node: "bpy.types.ShaderNode", socket_name: str
    ) -> "bpy.types.ShaderNodeTexImage | None":
        """Resolve `node.inputs[socket_name]` to an upstream Image Texture,
        following pass-through nodes (Reroute, Normal Map, Group boundaries).
        """
        socket = node.inputs.get(socket_name)
        return self._walk_to_image(socket)

    def _walk_to_image(
        self, socket, _group_stack=None, _visited=None, _depth=0,
    ) -> "bpy.types.ShaderNodeTexImage | None":
        """Generic socket walker that returns the first Image Texture node
        reachable upstream through Reroute / Normal Map / node-group I/O.
        `_group_stack` tracks GROUP nodes we've descended into so we can hop
        back out via GROUP_INPUT.
        """
        if socket is None or _depth > 16:
            return None
        if not socket.is_linked:
            return None
        if _visited is None:
            _visited = set()

        link = socket.links[0]
        upstream = link.from_node
        from_socket = link.from_socket
        key = (id(upstream), getattr(from_socket, "identifier", from_socket.name))
        if key in _visited:
            return None
        _visited.add(key)

        t = upstream.type
        if t == "TEX_IMAGE":
            return upstream
        if t == "REROUTE":
            return self._walk_to_image(
                upstream.inputs[0] if upstream.inputs else None,
                _group_stack, _visited, _depth + 1,
            )
        if t == "NORMAL_MAP":
            return self._walk_to_image(
                upstream.inputs.get("Color"),
                _group_stack, _visited, _depth + 1,
            )
        if t == "BUMP":
            # A Bump node combines a Height (bump/displacement) map with an
            # optional tangent-space Normal map. The tangent-space normal lives
            # on the `Normal` input; the Height map is captured separately as
            # heightTexture (see _layer_bump_info). For normalTexture purposes,
            # follow the Normal input.
            return self._walk_to_image(
                upstream.inputs.get("Normal"),
                _group_stack, _visited, _depth + 1,
            )
        if t == "GROUP":
            tree = getattr(upstream, "node_tree", None)
            if tree is None:
                return None
            gout = next((n for n in tree.nodes if n.type == "GROUP_OUTPUT"), None)
            if gout is None:
                return None
            inner = gout.inputs.get(from_socket.name)
            if inner is None:
                # Fall back to matching by output index
                for i, o in enumerate(upstream.outputs):
                    if o is from_socket and i < len(gout.inputs):
                        inner = gout.inputs[i]
                        break
            if inner is None:
                return None
            return self._walk_to_image(
                inner, (_group_stack or ()) + (upstream,), _visited, _depth + 1,
            )
        if t == "GROUP_INPUT":
            if not _group_stack:
                return None
            parent = _group_stack[-1]
            parent_in = parent.inputs.get(from_socket.name)
            if parent_in is None:
                return None
            return self._walk_to_image(
                parent_in, _group_stack[:-1], _visited, _depth + 1,
            )
        if t in ("MIX", "MIX_RGB"):
            # Best-effort: return whichever color input traces to an image.
            # Skip the Factor/Mask input. Try inputs in declared order; first
            # hit wins. Used for Ucupaint clamps and unbaked layer chains.
            for inp in upstream.inputs:
                n = inp.name.lower()
                if n in ("fac", "factor"):
                    continue
                # MIX node has typed sockets; only follow color-ish ones
                if hasattr(inp, "type") and inp.type not in ("RGBA", "VECTOR"):
                    continue
                if not inp.is_linked:
                    continue
                hit = self._walk_to_image(
                    inp, _group_stack, _visited, _depth + 1,
                )
                if hit is not None:
                    return hit
        return None

    def _gather_pbr(
        self, principled: "bpy.types.ShaderNodeBsdfPrincipled"
    ) -> MaterialPBRMetallicRoughness:
        # Base color
        base_color_socket = principled.inputs.get("Base Color")
        base_color_factor, base_color_texture = self._read_color_socket(base_color_socket)

        # Metallic / Roughness scalar factors. When a socket is linked Blender
        # ignores its default_value, so the glTF factor must stay at the spec
        # default of 1.0 (write None). Otherwise the stale socket default would
        # be multiplied into the texture on a spec-correct import — e.g. halving
        # a roughness map that sits at a 0.5 default.
        metallic_socket = principled.inputs.get("Metallic")
        if metallic_socket is not None and metallic_socket.is_linked:
            metallic_factor = None
        else:
            metallic = self._get_socket_default(principled, "Metallic")
            metallic_factor = float(metallic) if metallic is not None else None

        roughness_socket = principled.inputs.get("Roughness")
        if roughness_socket is not None and roughness_socket.is_linked:
            roughness_factor = None
        else:
            roughness = self._get_socket_default(principled, "Roughness")
            roughness_factor = float(roughness) if roughness is not None else None

        # Metallic/Roughness texture (if connected)
        mr_texture = None
        mr_node = self._get_connected_image_node(principled, "Metallic")
        if mr_node is None:
            mr_node = self._get_connected_image_node(principled, "Roughness")
        if mr_node:
            mr_texture = self.texture_exporter.gather_texture_info(mr_node)

        return MaterialPBRMetallicRoughness(
            base_color_factor=base_color_factor,
            base_color_texture=base_color_texture,
            metallic_factor=metallic_factor,
            roughness_factor=roughness_factor,
            metallic_roughness_texture=mr_texture,
        )

    def _gather_normal(
        self, principled: "bpy.types.ShaderNodeBsdfPrincipled"
    ) -> NormalTextureInfo | None:
        image_node = self._get_connected_image_node(principled, "Normal")
        if image_node is None:
            return None

        tex_info = self.texture_exporter.gather_texture_info(image_node)
        if tex_info is None:
            return None

        # Get normal strength from Normal Map node
        scale = None
        normal_socket = principled.inputs.get("Normal")
        if normal_socket and normal_socket.is_linked:
            normal_map_node = normal_socket.links[0].from_node
            if normal_map_node.type == "NORMAL_MAP":
                strength = normal_map_node.inputs.get("Strength")
                if strength and strength.default_value != 1.0:
                    scale = float(strength.default_value)

        return NormalTextureInfo(
            index=tex_info.index,
            tex_coord=tex_info.tex_coord,
            scale=scale,
            extensions=tex_info.extensions,
        )

    def _gather_emission(
        self, principled: "bpy.types.ShaderNodeBsdfPrincipled"
    ) -> tuple["TextureInfo | None", list[float] | None]:
        emission_color = self._get_socket_default(principled, "Emission Color")
        emission_strength = self._get_socket_default(principled, "Emission Strength")

        if emission_color is None:
            return None, None

        strength = float(emission_strength) if emission_strength is not None else 1.0

        # Check if emission is effectively zero
        r, g, b = float(emission_color[0]), float(emission_color[1]), float(emission_color[2])
        if (r * strength == 0 and g * strength == 0 and b * strength == 0):
            return None, None

        emissive_factor = [r * strength, g * strength, b * strength]

        # Emission texture
        emissive_texture = None
        image_node = self._get_connected_image_node(principled, "Emission Color")
        if image_node:
            emissive_texture = self.texture_exporter.gather_texture_info(image_node)

        return emissive_texture, emissive_factor

    def _gather_alpha(
        self,
        blender_material: "bpy.types.Material",
        principled: "bpy.types.ShaderNodeBsdfPrincipled",
    ) -> tuple[str | None, float | None]:
        blend_method = blender_material.surface_render_method if hasattr(
            blender_material, "surface_render_method"
        ) else getattr(blender_material, "blend_method", "OPAQUE")

        if blend_method == "OPAQUE":
            return None, None

        alpha = self._get_socket_default(principled, "Alpha")

        if blend_method == "CLIP" or blend_method == "HASHED":
            threshold = getattr(blender_material, "alpha_threshold", 0.5)
            cutoff = float(threshold) if threshold != 0.5 else None
            return "MASK", cutoff

        return "BLEND", None

    # Common input names that custom shader groups use for the diffuse/base
    # color and alpha sockets. Ordered by preference.
    _GROUP_BASE_COLOR_INPUTS = ("Base Color", "BaseColor", "Color", "Diffuse", "Albedo")
    _GROUP_ALPHA_INPUTS = ("Alpha", "Opacity")
    _GROUP_NORMAL_INPUTS = ("Normal", "Normal Map")

    def _gather_from_surface_group(
        self, blender_material: "bpy.types.Material",
    ) -> tuple["MaterialPBRMetallicRoughness | None", str | None, float | None]:
        """Fallback for materials with no Principled BSDF: walk
        Material Output.Surface, and if it's a custom shader group, try to
        recover (base color factor + texture, alpha mode) from common input
        names (Diffuse, Color, Alpha, …).
        Returns (pbr or None, alpha_mode, alpha_cutoff).
        """
        if not blender_material.use_nodes or blender_material.node_tree is None:
            return None, None, None

        out_node = next(
            (n for n in blender_material.node_tree.nodes if n.type == "OUTPUT_MATERIAL"),
            None,
        )
        if out_node is None:
            return None, None, None
        surface = out_node.inputs.get("Surface")
        if surface is None or not surface.is_linked:
            return None, None, None

        group_node = surface.links[0].from_node
        if group_node.type != "GROUP" or getattr(group_node, "node_tree", None) is None:
            return None, None, None

        bc_socket = None
        for name in self._GROUP_BASE_COLOR_INPUTS:
            s = group_node.inputs.get(name)
            if s is not None:
                bc_socket = s
                break
        bc_factor, bc_tex = self._read_color_socket(bc_socket) if bc_socket else (None, None)

        # Alpha: only emit a mode if the group exposes an alpha socket.
        alpha_mode = None
        alpha_cutoff = None
        for name in self._GROUP_ALPHA_INPUTS:
            a = group_node.inputs.get(name)
            if a is None:
                continue
            blend_method = (
                blender_material.surface_render_method
                if hasattr(blender_material, "surface_render_method")
                else getattr(blender_material, "blend_method", "OPAQUE")
            )
            if blend_method in ("CLIP", "HASHED"):
                threshold = getattr(blender_material, "alpha_threshold", 0.5)
                alpha_mode, alpha_cutoff = "MASK", (
                    float(threshold) if threshold != 0.5 else None
                )
            elif blend_method != "OPAQUE":
                alpha_mode = "BLEND"
            break

        if bc_factor is None and bc_tex is None:
            return None, alpha_mode, alpha_cutoff

        pbr = MaterialPBRMetallicRoughness(
            base_color_factor=bc_factor,
            base_color_texture=bc_tex,
        )
        return pbr, alpha_mode, alpha_cutoff

    # ------------------------------------------------------------------
    # CUSTOM_materials_layers — BSDFStackNode authoring path
    # ------------------------------------------------------------------

    def _find_bsdf_stack_node(self, blender_material):
        """Return the BSDFStackNode feeding Material Output.Surface, if any."""
        if not blender_material.use_nodes or blender_material.node_tree is None:
            return None
        out = next(
            (n for n in blender_material.node_tree.nodes
             if n.type == "OUTPUT_MATERIAL"),
            None,
        )
        if out is None:
            return None
        surface = out.inputs.get("Surface")
        if surface is None or not surface.is_linked:
            return None
        node = surface.links[0].from_node
        if getattr(node, "bl_idname", "") == BSDF_STACK_NODE_IDNAME:
            return node
        return None

    def _layer_socket(self, node, layer_index, channel_name):
        """Look up a BSDFStackNode input socket by (layer, channel name)."""
        idx = layer_index * _N_CH + _CH[channel_name]
        if 0 <= idx < len(node.inputs):
            return node.inputs[idx]
        return None

    def _layer_image_tex(self, node, layer_index, channel_name):
        """TextureInfo for the image feeding a layer's channel socket, or None."""
        socket = self._layer_socket(node, layer_index, channel_name)
        img = self._walk_to_image(socket) if socket is not None else None
        if img is None:
            return None
        return self.texture_exporter.gather_texture_info(img)

    def _layer_float(self, node, layer_index, channel_name):
        socket = self._layer_socket(node, layer_index, channel_name)
        if socket is None:
            return None
        try:
            return float(socket.default_value)
        except (TypeError, ValueError):
            return None

    def _layer_scalar_factor(self, node, layer_index, channel_name):
        """Scalar factor for a metallic/roughness-style channel: the socket's
        default_value, or None when the socket is linked. A linked socket's
        default is ignored by Blender, so the glTF factor must stay at its 1.0
        default to avoid double-applying it on top of the texture at import.
        """
        socket = self._layer_socket(node, layer_index, channel_name)
        if socket is None or socket.is_linked:
            return None
        try:
            return float(socket.default_value)
        except (TypeError, ValueError):
            return None

    def _layer_color_rgb(self, node, layer_index, channel_name):
        socket = self._layer_socket(node, layer_index, channel_name)
        if socket is None:
            return None
        v = socket.default_value
        return [float(v[0]), float(v[1]), float(v[2])]

    def _layer_pbr_dict(self, node, i):
        """Build a pbrMetallicRoughness dict for layer i (factors + textures)."""
        pbr: dict = {}
        rgb = self._layer_color_rgb(node, i, "Color")
        alpha = self._layer_float(node, i, "Alpha")
        a = alpha if alpha is not None else 1.0
        if rgb is not None and (rgb != [1.0, 1.0, 1.0] or a != 1.0):
            pbr["baseColorFactor"] = [rgb[0], rgb[1], rgb[2], a]
        bc_tex = self._layer_image_tex(node, i, "Color")
        if bc_tex is not None:
            pbr["baseColorTexture"] = bc_tex
        m = self._layer_scalar_factor(node, i, "Metallic")
        if m is not None and m != 1.0:
            pbr["metallicFactor"] = m
        r = self._layer_scalar_factor(node, i, "Roughness")
        if r is not None and r != 1.0:
            pbr["roughnessFactor"] = r
        mr_tex = (
            self._layer_image_tex(node, i, "Metallic")
            or self._layer_image_tex(node, i, "Roughness")
        )
        if mr_tex is not None:
            pbr["metallicRoughnessTexture"] = mr_tex
        return pbr or None

    def _layer_normal_info(self, node, i):
        """NormalTextureInfo for layer i's Normal channel, or None."""
        socket = self._layer_socket(node, i, "Normal")
        if socket is None or not socket.is_linked:
            return None
        img = self._walk_to_image(socket)
        if img is None:
            return None
        ti = self.texture_exporter.gather_texture_info(img)
        if ti is None:
            return None
        scale = self._normal_map_scale(socket)
        return NormalTextureInfo(
            index=ti.index, tex_coord=ti.tex_coord,
            scale=scale, extensions=ti.extensions,
        )

    def _normal_map_scale(self, socket):
        """Return the Normal Map node's Strength (!= 1.0) feeding `socket`, or
        None. Looks through a Bump node (Normal input) if present.
        """
        if socket is None or not socket.is_linked:
            return None
        src = socket.links[0].from_node
        if src.type == "BUMP":
            inner = src.inputs.get("Normal")
            if inner is not None and inner.is_linked:
                src = inner.links[0].from_node
            else:
                return None
        if src.type == "NORMAL_MAP":
            strength = src.inputs.get("Strength")
            if strength is not None and strength.default_value != 1.0:
                return float(strength.default_value)
        return None

    def _layer_bump_info(self, node, i):
        """If layer i's Normal socket is driven by a Bump node carrying a Height
        (depth/displacement) map, return (height_texture_info, bump_dict);
        otherwise (None, None). `bump_dict` = {strength, distance}.
        """
        return self._bump_info_from_socket(self._layer_socket(node, i, "Normal"))

    def _bump_info_from_socket(self, socket):
        """If `socket` is driven by a Bump node carrying a Height (depth/
        displacement) map, return (height_texture_info, bump_dict); otherwise
        (None, None). `bump_dict` = {strength, distance}.
        """
        if socket is None or not socket.is_linked:
            return None, None
        src = socket.links[0].from_node
        if src.type != "BUMP":
            return None, None
        height_socket = src.inputs.get("Height")
        himg = self._walk_to_image(height_socket) if height_socket is not None else None
        if himg is None:
            # A Bump node with no Height map carries no depth data to preserve.
            return None, None
        height_ti = self.texture_exporter.gather_texture_info(himg)
        if height_ti is None:
            return None, None
        bump = {}
        strength = src.inputs.get("Strength")
        if strength is not None:
            bump["strength"] = float(strength.default_value)
        distance = src.inputs.get("Distance")
        if distance is not None:
            bump["distance"] = float(distance.default_value)
        return height_ti, (bump or None)

    def _layer_emission(self, node, i):
        """Return (emissive_factor list|None, TextureInfo|None) for layer i."""
        rgb = self._layer_color_rgb(node, i, "Emission Color")
        strength = self._layer_float(node, i, "Emission Strength")
        if rgb is None:
            return None, None
        s = strength if strength is not None else 1.0
        factor = [rgb[0] * s, rgb[1] * s, rgb[2] * s]
        tex = self._layer_image_tex(node, i, "Emission Color")
        if factor == [0.0, 0.0, 0.0] and tex is None:
            return None, None
        return factor, tex

    def _layer_subsurface(self, node, i):
        """Return {weight, radius} dict for layer i, or None when inactive."""
        weight = self._layer_float(node, i, "Subsurface Weight")
        if weight is None or weight == 0.0:
            return None
        out = {"weight": weight}
        radius = self._layer_color_rgb(node, i, "Subsurface Radius")
        if radius is not None:
            out["radius"] = radius
        return out

    def _extract_from_bsdf_stack(self, blender_material, node):
        """Map a BSDFStackNode to (pbr, normal, emis_tex, emis_factor,
        alpha_mode, alpha_cutoff, layers, base_extra). Layer 0 -> base material;
        layers 1..N -> the CUSTOM_materials_layers `layers` array. `base_extra`
        carries layer-0 data with no core glTF slot (heightTexture/bump) and
        becomes extension.base.
        """
        n_layers = len(node.layers)

        # ---- Layer 0 -> base material ----
        rgb = self._layer_color_rgb(node, 0, "Color") or [1.0, 1.0, 1.0]
        alpha = self._layer_float(node, 0, "Alpha")
        a = alpha if alpha is not None else 1.0
        base_factor = [rgb[0], rgb[1], rgb[2], a]
        metallic = self._layer_scalar_factor(node, 0, "Metallic")
        roughness = self._layer_scalar_factor(node, 0, "Roughness")
        pbr = MaterialPBRMetallicRoughness(
            base_color_factor=(
                base_factor if base_factor != [1.0, 1.0, 1.0, 1.0] else None
            ),
            base_color_texture=self._layer_image_tex(node, 0, "Color"),
            metallic_factor=metallic,
            roughness_factor=roughness,
            metallic_roughness_texture=(
                self._layer_image_tex(node, 0, "Metallic")
                or self._layer_image_tex(node, 0, "Roughness")
            ),
        )
        normal_texture = self._layer_normal_info(node, 0)
        emissive_factor, emissive_texture = self._layer_emission(node, 0)
        alpha_mode, alpha_cutoff = self._gather_alpha(blender_material, node)

        # Base height/bump has no core glTF slot -> extension.base.
        base_extra: dict = {}
        base_height, base_bump = self._layer_bump_info(node, 0)
        if base_height is not None:
            base_extra["heightTexture"] = base_height
        if base_bump is not None:
            base_extra["bump"] = base_bump

        # ---- Layers 1..N -> extension ----
        layers: list[dict] = []
        for i in range(1, n_layers):
            layer = self._gather_bsdf_layer(node, i)
            if layer is not None:
                layers.append(layer)

        return (
            pbr, normal_texture, emissive_texture, emissive_factor,
            alpha_mode, alpha_cutoff, (layers or None), (base_extra or None),
        )

    def _gather_bsdf_layer(self, node, i):
        """Build one extension layer dict from BSDFStackNode layer i (i >= 1)."""
        layer: dict = {}
        props = node.layers[i] if i < len(node.layers) else None

        if props is not None and props.layer_name:
            layer["name"] = props.layer_name

        pbr = self._layer_pbr_dict(node, i)
        if pbr:
            layer["pbrMetallicRoughness"] = pbr

        normal = self._layer_normal_info(node, i)
        if normal is not None:
            layer["normalTexture"] = normal

        height_ti, bump = self._layer_bump_info(node, i)
        if height_ti is not None:
            layer["heightTexture"] = height_ti
        if bump is not None:
            layer["bump"] = bump

        emissive_factor, emissive_texture = self._layer_emission(node, i)
        if emissive_factor is not None:
            layer["emissiveFactor"] = emissive_factor
        if emissive_texture is not None:
            layer["emissiveTexture"] = emissive_texture

        subsurface = self._layer_subsurface(node, i)
        if subsurface is not None:
            layer["subsurface"] = subsurface

        # Mask (optional): omit for a full-opacity unmasked layer.
        mask = self._gather_layer_mask(self._layer_socket(node, i, "Mask"))
        if mask is not None:
            layer["mask"] = mask

        if props is not None:
            bm = str(props.blend_mode).upper()
            if bm in _VALID_BLEND_MODES and bm != "MIX":
                layer["blendMode"] = bm
            if props.opacity != 1.0:
                layer["opacity"] = float(props.opacity)
            if not props.enabled:
                layer["enabled"] = False

        return layer

    def _image_node_from_socket(self, socket):
        """Like _get_connected_image_node but takes a socket directly."""
        return self._walk_to_image(socket)

    def _read_color_socket(self, socket):
        """Resolve a color socket to (factor, TextureInfo).

        Handles the common upstream cases: Image Texture (texture wins, factor
        is the socket's local default), RGB node (read its output value as the
        factor), or unlinked (read socket default).

        When a socket is linked through pass-through nodes (Reroute, Group)
        but no image texture is reachable, the upstream output's
        `default_value` is often a stale evaluated value (commonly black for
        unevaluated group outputs). The Principled-side socket's
        `default_value` is a more reliable user-visible factor, so prefer it.
        """
        if socket is None:
            return None, None

        image_node = self._image_node_from_socket(socket)
        if image_node is not None:
            v = socket.default_value
            tex_info = self.texture_exporter.gather_texture_info(image_node)
            return [v[0], v[1], v[2], v[3]], tex_info

        if socket.is_linked:
            from_socket = socket.links[0].from_socket
            from_node = socket.links[0].from_node
            # For RGB-style upstreams (RGB, Value->Combine, Color attribute),
            # the output default is meaningful. For Group / Reroute / shader
            # nodes, prefer the Principled-side default.
            if getattr(from_node, "type", None) in {"RGB", "VALUE", "COMBINE_COLOR", "COMBINE_RGB"}:
                v = getattr(from_socket, "default_value", None)
                if v is not None and hasattr(v, "__len__") and len(v) >= 3:
                    a = v[3] if len(v) >= 4 else 1.0
                    return [v[0], v[1], v[2], a], None

        v = socket.default_value
        return [v[0], v[1], v[2], v[3]], None

    def _gather_layer_mask(self, socket) -> dict | None:
        if socket is None or not socket.is_linked:
            return None

        link = socket.links[0]
        src = link.from_node
        from_socket_name = link.from_socket.name

        if src.type == "TEX_IMAGE":
            ti = self.texture_exporter.gather_texture_info(src)
            if ti is None:
                return None
            tex: dict = {"index": ti.index}
            if ti.tex_coord is not None:
                tex["texCoord"] = ti.tex_coord
            if ti.extensions:
                tex["extensions"] = ti.extensions
            mask = {"source": "TEXTURE", "texture": tex}
            channel = "A" if from_socket_name == "Alpha" else "R"
            if channel != "R":
                mask["channel"] = channel
            return mask

        # Separate Color/RGB driven by an image — write the channel
        if src.type in ("SEPARATE_COLOR", "SEPRGB"):
            channel = _socket_to_channel(from_socket_name)
            color_in = src.inputs.get("Color") or src.inputs.get("Image")
            if color_in is not None and color_in.is_linked:
                inner = color_in.links[0].from_node
                if inner.type == "TEX_IMAGE":
                    ti = self.texture_exporter.gather_texture_info(inner)
                    if ti is None:
                        return None
                    tex = {"index": ti.index}
                    if ti.tex_coord is not None:
                        tex["texCoord"] = ti.tex_coord
                    if ti.extensions:
                        tex["extensions"] = ti.extensions
                    mask = {"source": "TEXTURE", "texture": tex}
                    if channel != "R":
                        mask["channel"] = channel
                    return mask
                if inner.type in ("VERTEX_COLOR", "ATTRIBUTE"):
                    return _vertex_color_mask(inner, channel)

        if src.type in ("VERTEX_COLOR", "ATTRIBUTE"):
            channel = "A" if from_socket_name == "Alpha" else "R"
            return _vertex_color_mask(src, channel)

        return None


def _socket_to_channel(name: str) -> str:
    name = name.upper()
    if name in _VALID_MASK_CHANNELS:
        return name
    if name == "RED":
        return "R"
    if name == "GREEN":
        return "G"
    if name == "BLUE":
        return "B"
    if name == "ALPHA":
        return "A"
    return "R"


def _vertex_color_mask(src, channel: str) -> dict:
    attr = ""
    if src.type == "VERTEX_COLOR":
        attr = getattr(src, "layer_name", "") or ""
    else:
        attr = getattr(src, "attribute_name", "") or ""
    mask = {"source": "VERTEX_COLOR"}
    if attr and attr != "COLOR_0":
        mask["attribute"] = attr
    if channel != "R":
        mask["channel"] = channel
    return mask
