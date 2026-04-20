from __future__ import annotations

from typing import TYPE_CHECKING

from .converter import (
    convert_location, convert_rotation, convert_scale,
    convert_location_array, convert_rotation_array, convert_scale_array,
)

if TYPE_CHECKING:
    import bpy
    from ..gltf.types import Gltf, Node
    from .buffer_reader import BufferReader
    from .mesh import MeshImporter
    from .skin import SkinImporter
    from ..importer import ImportSettings


class SceneImporter:
    def __init__(
        self,
        gltf: "Gltf",
        buffer_reader: "BufferReader",
        mesh_importer: "MeshImporter",
        settings: "ImportSettings",
        skin_importer: "SkinImporter | None" = None,
    ) -> None:
        self.gltf = gltf
        self.buffer_reader = buffer_reader
        self.mesh_importer = mesh_importer
        self.settings = settings
        self.skin_importer = skin_importer
        self.node_to_blender: dict[int, "bpy.types.Object"] = {}

    def import_scene(self, context: "bpy.types.Context") -> dict[int, "bpy.types.Object"]:
        scene_index = self.gltf.scene if self.gltf.scene is not None else 0
        if not self.gltf.scenes or scene_index >= len(self.gltf.scenes):
            return self.node_to_blender

        gltf_scene = self.gltf.scenes[scene_index]
        collection = context.scene.collection

        if gltf_scene.nodes:
            for node_index in gltf_scene.nodes:
                self._import_node(context, node_index, collection, parent_obj=None)

        return self.node_to_blender

    def _import_node(
        self,
        context: "bpy.types.Context",
        node_index: int,
        collection: "bpy.types.Collection",
        parent_obj: "bpy.types.Object | None",
    ) -> None:
        import bpy

        if self.gltf.nodes is None or node_index >= len(self.gltf.nodes):
            return
        node = self.gltf.nodes[node_index]
        name = node.name or f"Node_{node_index}"

        # Skip joint nodes (they become bones inside armatures)
        if (self.skin_importer and self.settings.import_skinning
                and node_index in self.skin_importer.joint_node_indices):
            return

        # Check for EXT_mesh_gpu_instancing
        if node.extensions and "EXT_mesh_gpu_instancing" in node.extensions:
            self._import_gpu_instanced_node(context, node, node_index, collection, parent_obj)
            return

        # Create Blender object
        blender_mesh = None
        if node.mesh is not None:
            blender_mesh = self.mesh_importer.blender_meshes.get(node.mesh)

        obj = bpy.data.objects.new(name, blender_mesh)
        collection.objects.link(obj)
        self._apply_transform(obj, node)

        if parent_obj:
            obj.parent = parent_obj

        # KHR_node_visibility
        if node.extensions and "KHR_node_visibility" in node.extensions:
            vis = node.extensions["KHR_node_visibility"]
            if not vis.get("visible", True):
                obj.hide_set(True)
                obj.hide_render = True

        self.node_to_blender[node_index] = obj

        # Handle skinned mesh: create armature and apply weights
        if (node.skin is not None and self.skin_importer
                and self.settings.import_skinning):
            armature_obj = self.skin_importer.import_skin(
                context, node.skin, obj, collection,
            )
            self._apply_transform(armature_obj, node)
            if parent_obj:
                armature_obj.parent = parent_obj
            # Clear transform on mesh since armature carries it
            obj.location = (0, 0, 0)
            obj.rotation_quaternion = (1, 0, 0, 0)
            obj.rotation_mode = "QUATERNION"
            obj.scale = (1, 1, 1)
            obj.parent = armature_obj

        # Apply morph targets (needs object)
        if (node.mesh is not None
                and self.settings.import_morph_targets
                and self.gltf.meshes):
            gltf_mesh = self.gltf.meshes[node.mesh]
            self.mesh_importer.apply_morph_targets(obj, node.mesh, gltf_mesh)

        # Recurse children
        if node.children:
            for child_index in node.children:
                self._import_node(context, child_index, collection, parent_obj=obj)

    def _apply_transform(self, obj: "bpy.types.Object", node: "Node") -> None:
        if node.matrix:
            import mathutils
            # glTF stores column-major 4x4
            m = node.matrix
            mat = mathutils.Matrix([
                [m[0], m[4], m[8], m[12]],
                [m[1], m[5], m[9], m[13]],
                [m[2], m[6], m[10], m[14]],
                [m[3], m[7], m[11], m[15]],
            ])
            loc, rot, scl = mat.decompose()
            obj.location = convert_location((loc.x, loc.y, loc.z))
            obj.rotation_mode = "QUATERNION"
            obj.rotation_quaternion = convert_rotation((rot.x, rot.y, rot.z, rot.w))
            obj.scale = convert_scale((scl.x, scl.y, scl.z))
        else:
            if node.translation:
                obj.location = convert_location(node.translation)
            if node.rotation:
                obj.rotation_mode = "QUATERNION"
                obj.rotation_quaternion = convert_rotation(node.rotation)
            if node.scale:
                obj.scale = convert_scale(node.scale)

    def _import_gpu_instanced_node(
        self,
        context: "bpy.types.Context",
        node: "Node",
        node_index: int,
        collection: "bpy.types.Collection",
        parent_obj: "bpy.types.Object | None",
    ) -> None:
        import bpy
        import numpy as np

        ext = node.extensions["EXT_mesh_gpu_instancing"]
        attrs = ext.get("attributes", {})

        trans = self.buffer_reader.read_accessor(attrs["TRANSLATION"]) if "TRANSLATION" in attrs else None
        rots = self.buffer_reader.read_accessor(attrs["ROTATION"]) if "ROTATION" in attrs else None
        scales = self.buffer_reader.read_accessor(attrs["SCALE"]) if "SCALE" in attrs else None

        if trans is not None:
            trans = convert_location_array(trans)
        if rots is not None:
            rots = convert_rotation_array(rots)
        if scales is not None:
            scales = convert_scale_array(scales)

        num_instances = len(trans) if trans is not None else 1

        # Create collection for source mesh
        inst_coll_name = node.name or f"Instance_{node_index}"
        inst_collection = bpy.data.collections.new(inst_coll_name)
        collection.children.link(inst_collection)

        # Add source mesh object(s)
        if node.mesh is not None:
            blender_mesh = self.mesh_importer.blender_meshes.get(node.mesh)
            source_obj = bpy.data.objects.new(node.name or "InstanceSource", blender_mesh)
            inst_collection.objects.link(source_obj)

        if node.children and self.gltf.nodes:
            for child_idx in node.children:
                child_node = self.gltf.nodes[child_idx]
                if child_node.mesh is not None:
                    child_mesh = self.mesh_importer.blender_meshes.get(child_node.mesh)
                    child_obj = bpy.data.objects.new(
                        child_node.name or "ChildMesh", child_mesh,
                    )
                    inst_collection.objects.link(child_obj)

        # Create instance empties
        for i in range(num_instances):
            empty = bpy.data.objects.new(f"{inst_coll_name}_{i}", None)
            empty.instance_type = "COLLECTION"
            empty.instance_collection = inst_collection
            collection.objects.link(empty)

            if trans is not None:
                empty.location = tuple(trans[i])
            if rots is not None:
                empty.rotation_mode = "QUATERNION"
                empty.rotation_quaternion = tuple(rots[i])
            if scales is not None:
                empty.scale = tuple(scales[i])

            if parent_obj:
                empty.parent = parent_obj

        # Exclude source collection from view layer
        self._exclude_collection(context, inst_collection)

    def _exclude_collection(
        self, context: "bpy.types.Context", target: "bpy.types.Collection"
    ) -> None:
        """Recursively find and exclude a collection in the view layer."""
        def _find_and_exclude(layer_col):
            if layer_col.collection == target:
                layer_col.exclude = True
                return True
            for child in layer_col.children:
                if _find_and_exclude(child):
                    return True
            return False

        _find_and_exclude(context.view_layer.layer_collection)
