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
    from .physics import PhysicsImporter
    from ..importer import ImportSettings


class SceneImporter:
    def __init__(
        self,
        gltf: "Gltf",
        buffer_reader: "BufferReader",
        mesh_importer: "MeshImporter",
        settings: "ImportSettings",
        skin_importer: "SkinImporter | None" = None,
        physics_importer: "PhysicsImporter | None" = None,
    ) -> None:
        self.gltf = gltf
        self.buffer_reader = buffer_reader
        self.mesh_importer = mesh_importer
        self.settings = settings
        self.skin_importer = skin_importer
        self.physics_importer = physics_importer
        self.node_to_blender: dict[int, "bpy.types.Object"] = {}
        self._skin_armatures: dict[int, "bpy.types.Object"] = {}

    def import_scene(self, context: "bpy.types.Context") -> dict[int, "bpy.types.Object"]:
        """Import all glTF scenes, creating Blender scenes as needed."""
        import bpy

        if not self.gltf.scenes:
            return self.node_to_blender

        active_scene_index = self.gltf.scene if self.gltf.scene is not None else 0
        original_scene = context.window.scene

        for scene_idx, gltf_scene in enumerate(self.gltf.scenes):
            if scene_idx == 0:
                # Use the existing active scene for the first glTF scene
                bl_scene = context.scene
            else:
                bl_scene = bpy.data.scenes.new(gltf_scene.name or f"Scene_{scene_idx}")

            if gltf_scene.name:
                bl_scene.name = gltf_scene.name

            context.window.scene = bl_scene
            collection = bl_scene.collection

            if gltf_scene.nodes:
                for node_index in gltf_scene.nodes:
                    self._import_node(context, node_index, collection, parent_obj=None)

        # Switch to the active scene as indicated by the glTF
        if active_scene_index < len(self.gltf.scenes):
            target_name = self.gltf.scenes[active_scene_index].name
            if target_name:
                for sc in bpy.data.scenes:
                    if sc.name == target_name or sc.name.startswith(target_name):
                        context.window.scene = sc
                        break
            else:
                context.window.scene = original_scene
        else:
            context.window.scene = original_scene

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

        # If this node was already imported (shared across scenes), link into this collection
        if node_index in self.node_to_blender:
            existing_obj = self.node_to_blender[node_index]
            if existing_obj.name not in collection.objects:
                collection.objects.link(existing_obj)
            return

        node = self.gltf.nodes[node_index]
        name = node.name or f"Node_{node_index}"

        # Skip joint nodes (they become bones inside armatures)
        if (self.skin_importer and self.settings.import_skinning
                and node_index in self.skin_importer.joint_node_indices):
            return

        # Handle armature wrapper nodes: create the armature here
        # instead of creating an empty + a duplicate armature at the mesh
        if (self.skin_importer and self.settings.import_skinning
                and node_index in self.skin_importer.armature_node_indices):
            skin_index = self.skin_importer.skin_for_node[node_index]

            # Compute armature's world matrix so bones are placed in
            # armature-local space rather than world space
            context.view_layer.update()
            arm_world = self._get_node_world_matrix(node, parent_obj)

            armature_obj = self.skin_importer.create_armature(
                context, skin_index, collection, arm_world,
            )
            self._apply_transform(armature_obj, node)
            if parent_obj:
                armature_obj.parent = parent_obj
            self.node_to_blender[node_index] = armature_obj
            self._skin_armatures[skin_index] = armature_obj

            if node.children:
                for child_index in node.children:
                    self._import_node(
                        context, child_index, collection, parent_obj=armature_obj,
                    )
            return

        # Check for EXT_mesh_gpu_instancing
        if node.extensions and "EXT_mesh_gpu_instancing" in node.extensions:
            self._import_gpu_instanced_node(context, node, node_index, collection, parent_obj)
            return

        # Create Blender object
        obj_data = None
        if node.mesh is not None:
            obj_data = self.mesh_importer.blender_meshes.get(node.mesh)
        elif node.camera is not None and self.gltf.cameras:
            obj_data = self._create_camera(node.camera)
        elif (node.extensions and "KHR_lights_punctual" in node.extensions):
            light_idx = node.extensions["KHR_lights_punctual"].get("light")
            if light_idx is not None:
                obj_data = self._create_light(light_idx)

        obj = bpy.data.objects.new(name, obj_data)
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

        # Handle skinned mesh: apply weights to pre-created armature or create one
        if (node.skin is not None and self.skin_importer
                and self.settings.import_skinning):
            if node.skin in self._skin_armatures:
                # Armature was already created at its wrapper node
                armature_obj = self._skin_armatures[node.skin]
                self.skin_importer.apply_skin_to_mesh(obj, node.skin, armature_obj)
            else:
                # Fallback: create armature at the mesh node
                context.view_layer.update()
                arm_world = self._get_node_world_matrix(node, parent_obj)
                armature_obj = self.skin_importer.import_skin(
                    context, node.skin, obj, collection, arm_world,
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

        # Physics (rigid body / collider)
        if self.physics_importer and self.settings.import_physics:
            self.physics_importer.import_node(context, obj, node, node_index)

        # Recurse children
        if node.children:
            for child_index in node.children:
                self._import_node(context, child_index, collection, parent_obj=obj)

    def _create_camera(self, camera_index: int) -> "bpy.types.Camera | None":
        """Create a Blender camera from a glTF camera index."""
        import bpy

        if camera_index >= len(self.gltf.cameras):
            return None
        gltf_cam = self.gltf.cameras[camera_index]
        cam_name = gltf_cam.name if hasattr(gltf_cam, 'name') and gltf_cam.name else f"Camera_{camera_index}"

        if hasattr(gltf_cam, 'type') and gltf_cam.type == "orthographic":
            cam = bpy.data.cameras.new(cam_name)
            cam.type = "ORTHO"
            ortho = gltf_cam.orthographic if hasattr(gltf_cam, 'orthographic') else None
            if ortho:
                xmag = ortho.xmag if hasattr(ortho, 'xmag') else ortho.get("xmag", 1.0)
                ymag = ortho.ymag if hasattr(ortho, 'ymag') else ortho.get("ymag", 1.0)
                cam.ortho_scale = max(xmag, ymag) * 2.0
                cam.clip_start = ortho.znear if hasattr(ortho, 'znear') else ortho.get("znear", 0.01)
                cam.clip_end = ortho.zfar if hasattr(ortho, 'zfar') else ortho.get("zfar", 1000.0)
        else:
            cam = bpy.data.cameras.new(cam_name)
            cam.type = "PERSP"
            persp = gltf_cam.perspective if hasattr(gltf_cam, 'perspective') else None
            if persp:
                yfov = persp.yfov if hasattr(persp, 'yfov') else persp.get("yfov", 0.5)
                cam.angle_y = yfov
                cam.lens_unit = "FOV"
                cam.clip_start = persp.znear if hasattr(persp, 'znear') else persp.get("znear", 0.01)
                zfar = persp.zfar if hasattr(persp, 'zfar') else persp.get("zfar", None)
                if zfar is not None:
                    cam.clip_end = zfar
        return cam

    _GLTF_LIGHT_TYPE_MAP = {
        "point": "POINT",
        "directional": "SUN",
        "spot": "SPOT",
    }

    def _create_light(self, light_index: int) -> "bpy.types.Light | None":
        """Create a Blender light from a KHR_lights_punctual light index."""
        import bpy

        root_ext = self.gltf.extensions or {}
        lights = root_ext.get("KHR_lights_punctual", {}).get("lights", [])
        if light_index >= len(lights):
            return None
        gltf_light = lights[light_index]

        gltf_type = gltf_light.get("type", "point")
        bl_type = self._GLTF_LIGHT_TYPE_MAP.get(gltf_type, "POINT")
        light_name = gltf_light.get("name", f"Light_{light_index}")

        light = bpy.data.lights.new(light_name, bl_type)
        light.energy = gltf_light.get("intensity", 1.0)

        color = gltf_light.get("color", [1.0, 1.0, 1.0])
        light.color = (color[0], color[1], color[2])

        if "range" in gltf_light:
            light.use_custom_distance = True
            light.cutoff_distance = gltf_light["range"]

        if gltf_type == "spot" and "spot" in gltf_light:
            spot = gltf_light["spot"]
            outer = spot.get("outerConeAngle", 0.7854)
            inner = spot.get("innerConeAngle", 0.0)
            light.spot_size = outer * 2.0
            light.spot_blend = inner / outer if outer > 0 else 0.0

        return light

    def _get_node_world_matrix(self, node: "Node", parent_obj: "bpy.types.Object | None"):
        """Compute the expected world matrix for a node from its TRS and parent."""
        import mathutils

        loc = mathutils.Vector(convert_location(node.translation)) if node.translation else mathutils.Vector()
        quat = mathutils.Quaternion(convert_rotation(node.rotation)) if node.rotation else mathutils.Quaternion()
        scl = mathutils.Vector(convert_scale(node.scale)) if node.scale else mathutils.Vector((1, 1, 1))
        local = mathutils.Matrix.LocRotScale(loc, quat, scl)

        if parent_obj:
            return parent_obj.matrix_world @ local
        return local

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
