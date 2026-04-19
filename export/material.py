from __future__ import annotations

from typing import TYPE_CHECKING

from ..gltf.types import Material, MaterialPBRMetallicRoughness, NormalTextureInfo
from .texture import TextureExporter

if TYPE_CHECKING:
    import bpy
    from ..exporter import ExportSettings


class MaterialExporter:
    def __init__(self, texture_exporter: TextureExporter, settings: "ExportSettings") -> None:
        self.texture_exporter = texture_exporter
        self.settings = settings
        self.materials: list[Material] = []
        self._cache: dict[str, int] = {}

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

        principled = self._find_principled_bsdf(blender_material)

        if principled is not None:
            pbr = self._gather_pbr(principled)
            normal_texture = self._gather_normal(principled)
            emissive_texture, emissive_factor = self._gather_emission(principled)
            alpha_mode, alpha_cutoff = self._gather_alpha(blender_material, principled)

        if blender_material.use_backface_culling is False:
            double_sided = True

        return Material(
            name=blender_material.name,
            pbr_metallic_roughness=pbr,
            normal_texture=normal_texture,
            emissive_texture=emissive_texture,
            emissive_factor=emissive_factor,
            alpha_mode=alpha_mode,
            alpha_cutoff=alpha_cutoff,
            double_sided=double_sided,
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
        """If a socket has a linked Image Texture node, return it."""
        socket = node.inputs.get(socket_name)
        if socket is None or not socket.is_linked:
            return None

        linked_node = socket.links[0].from_node
        if linked_node.type == "TEX_IMAGE":
            return linked_node

        # Handle Normal Map node -> Image Texture
        if linked_node.type == "NORMAL_MAP":
            color_socket = linked_node.inputs.get("Color")
            if color_socket and color_socket.is_linked:
                inner_node = color_socket.links[0].from_node
                if inner_node.type == "TEX_IMAGE":
                    return inner_node

        return None

    def _gather_pbr(
        self, principled: "bpy.types.ShaderNodeBsdfPrincipled"
    ) -> MaterialPBRMetallicRoughness:
        # Base color
        base_color = self._get_socket_default(principled, "Base Color")
        base_color_factor = None
        if base_color is not None:
            base_color_factor = [base_color[0], base_color[1], base_color[2], base_color[3]]

        base_color_texture = None
        image_node = self._get_connected_image_node(principled, "Base Color")
        if image_node:
            base_color_texture = self.texture_exporter.gather_texture_info(image_node)

        # Metallic
        metallic = self._get_socket_default(principled, "Metallic")
        metallic_factor = float(metallic) if metallic is not None else None

        # Roughness
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
