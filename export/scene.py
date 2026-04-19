from __future__ import annotations

from typing import TYPE_CHECKING

from ..gltf.types import Scene, Node
from .converter import convert_location, convert_rotation, convert_scale
from .mesh import MeshExporter
from .material import MaterialExporter

if TYPE_CHECKING:
    import bpy
    from ..exporter import ExportSettings


EXT_NODE_VISIBILITY = "KHR_node_visibility"


class SceneExporter:
    def __init__(
        self,
        mesh_exporter: MeshExporter,
        material_exporter: MaterialExporter,
        settings: "ExportSettings",
    ) -> None:
        self.mesh_exporter = mesh_exporter
        self.material_exporter = material_exporter
        self.settings = settings
        self.nodes: list[Node] = []
        self.extensions_used: set[str] = set()

    def gather(self, context: "bpy.types.Context") -> tuple[list[Scene], int]:
        """Traverse the active scene and return (scenes, active_scene_index)."""
        scene = context.scene
        root_nodes: list[int] = []

        for obj in scene.objects:
            if obj.parent is not None:
                continue  # Only process root objects

            node_index = self._gather_node(obj)
            if node_index is not None:
                root_nodes.append(node_index)

        gltf_scene = Scene(
            name=scene.name,
            nodes=root_nodes if root_nodes else None,
        )
        return [gltf_scene], 0

    def _gather_node(self, obj: "bpy.types.Object") -> int | None:
        """Convert a Blender object to a glTF Node. Returns node index."""
        is_visible = obj.visible_get()

        # Gather mesh (if applicable)
        mesh_index = None
        if obj.type == "MESH":
            # Build material slot -> glTF material index mapping
            material_map = self._gather_materials_for_object(obj)
            mesh_index = self.mesh_exporter.gather(obj, material_map)

        # Gather children recursively (include hidden children too)
        children: list[int] = []
        for child in obj.children:
            child_index = self._gather_node(child)
            if child_index is not None:
                children.append(child_index)

        # Convert transform (Blender Z-up -> glTF Y-up)
        loc, rot, scale = obj.matrix_local.decompose()

        translation = convert_location(loc)
        rotation = convert_rotation(rot)
        gltf_scale = convert_scale(scale)

        # Omit identity transforms
        is_identity_t = all(abs(v) < 1e-6 for v in translation)
        is_identity_r = (abs(rotation[0]) < 1e-6 and abs(rotation[1]) < 1e-6 and
                         abs(rotation[2]) < 1e-6 and abs(rotation[3] - 1.0) < 1e-6)
        is_identity_s = all(abs(v - 1.0) < 1e-6 for v in gltf_scale)

        # KHR_node_visibility: only add extension when hidden (visible=true is default)
        extensions = None
        if not is_visible:
            extensions = {
                EXT_NODE_VISIBILITY: {"visible": False}
            }
            self.extensions_used.add(EXT_NODE_VISIBILITY)

        node = Node(
            name=obj.name,
            mesh=mesh_index,
            children=children if children else None,
            translation=translation if not is_identity_t else None,
            rotation=rotation if not is_identity_r else None,
            scale=gltf_scale if not is_identity_s else None,
            extensions=extensions,
        )

        index = len(self.nodes)
        self.nodes.append(node)
        return index

    def _gather_materials_for_object(self, obj: "bpy.types.Object") -> dict[int, int]:
        """Gather materials and return mapping: Blender slot index -> glTF material index."""
        material_map: dict[int, int] = {}
        for i, slot in enumerate(obj.material_slots):
            if slot.material is not None:
                gltf_idx = self.material_exporter.gather(slot.material)
                if gltf_idx is not None:
                    material_map[i] = gltf_idx
        return material_map
