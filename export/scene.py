from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

import numpy as np

from ..gltf.buffer import BufferBuilder
from ..gltf.constants import ComponentType, DataType
from ..gltf.types import Scene, Node
from .converter import (
    convert_location, convert_rotation, convert_scale,
    convert_location_array, convert_rotation_array, convert_scale_array,
)
from .mesh import MeshExporter
from .material import MaterialExporter
from .skin import SkinExporter
from .physics import PhysicsExporter

if TYPE_CHECKING:
    import bpy
    from ..exporter import ExportSettings


EXT_NODE_VISIBILITY = "KHR_node_visibility"
EXT_GPU_INSTANCING = "EXT_mesh_gpu_instancing"


class SceneExporter:
    def __init__(
        self,
        mesh_exporter: MeshExporter,
        material_exporter: MaterialExporter,
        buffer: BufferBuilder,
        settings: "ExportSettings",
        skin_exporter: SkinExporter | None = None,
        physics_exporter: PhysicsExporter | None = None,
    ) -> None:
        self.mesh_exporter = mesh_exporter
        self.material_exporter = material_exporter
        self.buffer = buffer
        self.settings = settings
        self.skin_exporter = skin_exporter
        self.physics_exporter = physics_exporter
        self.nodes: list[Node] = []
        self.object_to_node_index: dict[str, int] = {}
        self.extensions_used: set[str] = set()

    def gather(self, context: "bpy.types.Context") -> tuple[list[Scene], int]:
        """Traverse the active scene and return (scenes, active_scene_index)."""
        scene = context.scene
        root_nodes: list[int] = []

        # Pre-pass: detect instances via depsgraph (GN, collection instances, particles)
        skip_objects: set[str] = set()
        if self.settings.export_gpu_instancing:
            self._instancer_names = set()
            self._instanced_source_names = set()
            instancing_nodes = self._instancing_pre_pass(scene)
            for idx in instancing_nodes:
                root_nodes.append(idx)
            # Only skip objects that are purely instancers (empties with collection instances)
            # or source objects that aren't real scene objects.
            # Don't skip mesh objects that also serve as GN hosts (like Ground),
            # since GN output replaces their geometry and they should be exported normally.
            for name in self._instancer_names:
                obj = scene.objects.get(name)
                if obj and obj.type != "MESH":
                    skip_objects.add(name)
            # Skip source meshes that only exist as instance sources
            # (e.g., Trunk/Foliage in a hidden collection)
            for name in self._instanced_source_names:
                obj = scene.objects.get(name)
                if obj:
                    skip_objects.add(name)

        # Process armatures first to ensure skin data is available for skinned meshes
        root_objects = [
            obj for obj in scene.objects
            if obj.parent is None and obj.name not in skip_objects
        ]
        root_objects.sort(key=lambda o: (0 if o.type == "ARMATURE" else 1))

        for obj in root_objects:
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
        skin_index = None
        if obj.type == "MESH":
            # Check for armature modifier (skinned mesh)
            joint_map = None
            if self.skin_exporter and self.settings.export_skinning:
                armature_mod = self._find_armature_modifier(obj)
                if armature_mod and armature_mod.object:
                    arm_name = armature_mod.object.name
                    if arm_name in self.skin_exporter.armature_joint_maps:
                        joint_map = self.skin_exporter.armature_joint_maps[arm_name]
                        skin_index = self.skin_exporter.armature_skin_index[arm_name]

            # Build material slot -> glTF material index mapping
            material_map = self._gather_materials_for_object(obj)
            mesh_index = self.mesh_exporter.gather(obj, material_map, joint_map)

        # Gather children recursively (include hidden children too)
        children: list[int] = []

        # For armatures, create bone nodes as children
        if obj.type == "ARMATURE" and self.skin_exporter and self.settings.export_skinning:
            # Create armature node first so we have its index
            loc, rot, scale = obj.matrix_local.decompose()
            translation = convert_location(loc)
            rotation = convert_rotation(rot)
            gltf_scale = convert_scale(scale)

            is_identity_t = all(abs(v) < 1e-6 for v in translation)
            is_identity_r = (abs(rotation[0]) < 1e-6 and abs(rotation[1]) < 1e-6 and
                             abs(rotation[2]) < 1e-6 and abs(rotation[3] - 1.0) < 1e-6)
            is_identity_s = all(abs(v - 1.0) < 1e-6 for v in gltf_scale)

            extensions = None
            if not is_visible:
                extensions = {EXT_NODE_VISIBILITY: {"visible": False}}
                self.extensions_used.add(EXT_NODE_VISIBILITY)

            node = Node(
                name=obj.name,
                translation=translation if not is_identity_t else None,
                rotation=rotation if not is_identity_r else None,
                scale=gltf_scale if not is_identity_s else None,
                extensions=extensions,
            )
            index = len(self.nodes)
            self.nodes.append(node)
            self.object_to_node_index[obj.name] = index

            # Create bone child nodes
            root_bone_indices = self.skin_exporter.gather_armature(obj, index, self.nodes)
            children.extend(root_bone_indices)

            # Recurse regular children (skinned meshes parented to armature)
            for child in obj.children:
                child_index = self._gather_node(child)
                if child_index is not None:
                    children.append(child_index)

            node.children = children if children else None
            return index

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
            skin=skin_index,
            children=children if children else None,
            translation=translation if not is_identity_t else None,
            rotation=rotation if not is_identity_r else None,
            scale=gltf_scale if not is_identity_s else None,
            extensions=extensions,
        )

        # Physics extension (rigid body / collider)
        if self.physics_exporter:
            physics_ext = self.physics_exporter.gather_node(obj, mesh_index)
            if physics_ext:
                if node.extensions is None:
                    node.extensions = {}
                node.extensions.update(physics_ext)

        index = len(self.nodes)
        self.nodes.append(node)
        self.object_to_node_index[obj.name] = index
        return index

    @staticmethod
    def _find_armature_modifier(obj: "bpy.types.Object"):
        """Find the first active Armature modifier on an object."""
        for mod in obj.modifiers:
            if mod.type == "ARMATURE" and mod.object:
                return mod
        return None

    def _gather_materials_for_object(self, obj: "bpy.types.Object") -> dict[int, int]:
        """Gather materials and return mapping: Blender slot index -> glTF material index."""
        material_map: dict[int, int] = {}
        for i, slot in enumerate(obj.material_slots):
            if slot.material is not None:
                gltf_idx = self.material_exporter.gather(slot.material)
                if gltf_idx is not None:
                    material_map[i] = gltf_idx
        return material_map

    # --- EXT_mesh_gpu_instancing (depsgraph-based) ---

    def _instancing_pre_pass(self, scene: "bpy.types.Scene") -> list[int]:
        """Detect instances via depsgraph (handles collection instances, GN, particles).
        Returns list of root node indices for instanced nodes."""
        import bpy

        depsgraph = bpy.context.evaluated_depsgraph_get()

        # Collect all instances grouped by source mesh name.
        # Each entry: list of (translation, rotation_wxyz, scale) tuples.
        # We also track which source objects we've seen so we can export their mesh once.
        instance_groups: dict[str, list[tuple[list[float], list[float], list[float]]]] = defaultdict(list)
        source_objects: dict[str, "bpy.types.Object"] = {}
        instancer_names: set[str] = set()

        for inst in depsgraph.object_instances:
            if not inst.is_instance:
                continue
            obj = inst.object.original
            if obj.type != "MESH":
                continue

            # Track the parent (instancer) so we can skip it in normal traversal
            if inst.parent:
                instancer_names.add(inst.parent.original.name)

            loc, rot, scl = inst.matrix_world.decompose()
            instance_groups[obj.name].append((
                [loc.x, loc.y, loc.z],
                [rot.w, rot.x, rot.y, rot.z],
                [scl.x, scl.y, scl.z],
            ))
            if obj.name not in source_objects:
                source_objects[obj.name] = obj

        # Store instancer names so gather() can skip them
        self._instancer_names = instancer_names
        # Also mark source objects that only appear as instances (not in scene directly)
        self._instanced_source_names: set[str] = set(source_objects.keys())

        result_nodes: list[int] = []

        # Group source meshes that share the same set of instance transforms
        # (e.g., Trunk and Foliage from the same collection share transforms)
        # Detect this by comparing instance counts and parent sets
        transform_groups: dict[str, list[str]] = {}  # key -> list of mesh names
        mesh_to_key: dict[str, str] = {}

        for mesh_name, transforms in instance_groups.items():
            # Create a hashable key from the number of instances
            # Meshes from the same collection/GN setup will have identical count
            count = len(transforms)
            # Find if any existing group has the same count AND same translations
            # (comparing first instance location as a quick check)
            first_loc = tuple(round(v, 4) for v in transforms[0][0])
            key = f"{count}_{first_loc}"

            if key in transform_groups:
                transform_groups[key].append(mesh_name)
            else:
                transform_groups[key] = [mesh_name]
            mesh_to_key[mesh_name] = key

        # Process each transform group
        processed_keys: set[str] = set()
        for mesh_name in instance_groups:
            key = mesh_to_key[mesh_name]
            if key in processed_keys:
                continue
            processed_keys.add(key)

            group_meshes = transform_groups[key]
            transforms = instance_groups[group_meshes[0]]  # all share same transforms

            if len(transforms) < 2:
                # Single instance: export as regular node
                node_idx = self._gather_single_instance(
                    group_meshes, source_objects, transforms[0],
                )
                if node_idx is not None:
                    result_nodes.append(node_idx)
            else:
                # Multiple instances: use EXT_mesh_gpu_instancing
                node_idx = self._gather_gpu_instancing(
                    group_meshes, source_objects, transforms,
                )
                if node_idx is not None:
                    result_nodes.append(node_idx)

        return result_nodes

    def _gather_single_instance(
        self,
        mesh_names: list[str],
        source_objects: dict[str, "bpy.types.Object"],
        transform: tuple[list[float], list[float], list[float]],
    ) -> int | None:
        """Export a single instance as a regular node."""
        loc, rot_wxyz, scl = transform
        translation = convert_location(loc)
        rotation = convert_rotation(rot_wxyz)
        gltf_scale = convert_scale(scl)

        children: list[int] = []
        for mesh_name in mesh_names:
            obj = source_objects[mesh_name]
            material_map = self._gather_materials_for_object(obj)
            mesh_index = self.mesh_exporter.gather(obj, material_map)
            if mesh_index is not None:
                child_node = Node(name=mesh_name, mesh=mesh_index)
                child_idx = len(self.nodes)
                self.nodes.append(child_node)
                children.append(child_idx)

        if not children:
            return None

        if len(children) == 1 and len(mesh_names) == 1:
            # Single mesh: set transform directly on the mesh node
            self.nodes[children[0]].translation = translation
            self.nodes[children[0]].rotation = rotation
            self.nodes[children[0]].scale = gltf_scale
            return children[0]

        node = Node(
            name=f"{mesh_names[0]}_instance",
            children=children,
            translation=translation,
            rotation=rotation,
            scale=gltf_scale,
        )
        idx = len(self.nodes)
        self.nodes.append(node)
        return idx

    def _gather_gpu_instancing(
        self,
        mesh_names: list[str],
        source_objects: dict[str, "bpy.types.Object"],
        transforms: list[tuple[list[float], list[float], list[float]]],
    ) -> int | None:
        """Create instanced node(s) with EXT_mesh_gpu_instancing."""
        num_instances = len(transforms)
        translations = np.empty((num_instances, 3), dtype=np.float32)
        rotations = np.empty((num_instances, 4), dtype=np.float32)
        scales = np.empty((num_instances, 3), dtype=np.float32)

        for i, (loc, rot_wxyz, scl) in enumerate(transforms):
            translations[i] = loc
            rotations[i] = rot_wxyz
            scales[i] = scl

        # Convert to glTF coordinate system
        translations = convert_location_array(translations)
        rotations = convert_rotation_array(rotations)
        scales = convert_scale_array(scales)

        # Write instance transform accessors
        trans_acc = self.buffer.add_accessor(
            translations, ComponentType.FLOAT, DataType.VEC3,
        )
        rot_acc = self.buffer.add_accessor(
            rotations, ComponentType.FLOAT, DataType.VEC4,
        )
        scale_acc = self.buffer.add_accessor(
            scales, ComponentType.FLOAT, DataType.VEC3,
        )

        instancing_ext = {
            EXT_GPU_INSTANCING: {
                "attributes": {
                    "TRANSLATION": trans_acc,
                    "ROTATION": rot_acc,
                    "SCALE": scale_acc,
                }
            }
        }
        self.extensions_used.add(EXT_GPU_INSTANCING)

        # Export mesh(es)
        children: list[int] = []
        for mesh_name in mesh_names:
            obj = source_objects[mesh_name]
            material_map = self._gather_materials_for_object(obj)
            mesh_index = self.mesh_exporter.gather(obj, material_map)
            if mesh_index is not None:
                if len(mesh_names) == 1:
                    # Single mesh: put instancing directly on mesh node
                    node = Node(
                        name=f"{mesh_name}_instances",
                        mesh=mesh_index,
                        extensions=instancing_ext,
                    )
                    idx = len(self.nodes)
                    self.nodes.append(node)
                    return idx
                else:
                    child_node = Node(name=mesh_name, mesh=mesh_index)
                    child_idx = len(self.nodes)
                    self.nodes.append(child_node)
                    children.append(child_idx)

        if not children:
            return None

        # Multiple meshes: parent node with instancing + child nodes
        node = Node(
            name=f"{mesh_names[0]}_instances",
            children=children,
            extensions=instancing_ext,
        )
        idx = len(self.nodes)
        self.nodes.append(node)
        return idx
