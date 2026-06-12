from __future__ import annotations

from typing import TYPE_CHECKING

from .converter import (
    convert_location, convert_rotation, convert_scale,
    convert_location_array, convert_rotation_array, convert_scale_array,
    matrix_from_gltf,
)

if TYPE_CHECKING:
    import bpy
    from ..gltf.types import Gltf, Node
    from .buffer_reader import BufferReader
    from .mesh import MeshImporter
    from .skin import SkinImporter
    from .physics import PhysicsImporter
    from .particles import ParticleImporter
    from .interactivity import InteractivityImporter
    from .audio import AudioImporter
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
        particle_importer: "ParticleImporter | None" = None,
        interactivity_importer: "InteractivityImporter | None" = None,
        audio_importer: "AudioImporter | None" = None,
        file_resolver=None,
        target_collection: "bpy.types.Collection | None" = None,
        external_depth: int = 0,
    ) -> None:
        self.gltf = gltf
        self.buffer_reader = buffer_reader
        self.mesh_importer = mesh_importer
        self.settings = settings
        self.skin_importer = skin_importer
        self.physics_importer = physics_importer
        self.particle_importer = particle_importer
        self.interactivity_importer = interactivity_importer
        self.audio_importer = audio_importer
        # glTF 2.1 [DRAFT] external assets: resolver + per-file collection cache.
        self.file_resolver = file_resolver
        self.target_collection = target_collection
        self.external_depth = external_depth
        self._imported_files: dict[int, "bpy.types.Collection"] = {}
        self.node_to_blender: dict[int, "bpy.types.Object"] = {}
        self._skin_armatures: dict[int, "bpy.types.Object"] = {}
        # GPU-instancing reconstruction (Geometry Nodes "Instance on Points").
        # Instances/Sources collections are per parent collection (keyed by
        # as_pointer) so multi-scene files get one pair per scene.
        self._instance_node_group: "bpy.types.NodeTree | None" = None
        self._instances_colls: dict[int, "bpy.types.Collection"] = {}
        self._sources_colls: dict[int, "bpy.types.Collection"] = {}

    def import_scene(self, context: "bpy.types.Context") -> dict[int, "bpy.types.Object"]:
        """Import all glTF scenes, creating Blender scenes as needed."""
        import bpy

        if not self.gltf.scenes:
            return self.node_to_blender

        # Sub-asset import: drop every node into one collection (no new Blender
        # scenes, no active-scene switching) so the host can instance it.
        if self.target_collection is not None:
            coll = self.target_collection
            for gltf_scene in self.gltf.scenes:
                if gltf_scene.nodes:
                    for node_index in gltf_scene.nodes:
                        self._import_node(context, node_index, coll, parent_obj=None)
                if self.audio_importer:
                    self.audio_importer.import_global_emitters(gltf_scene, coll)
            return self.node_to_blender

        active_scene_index = self.gltf.scene if self.gltf.scene is not None else 0
        original_scene = context.window.scene

        # Track the Blender scene created for each glTF scene so the active
        # scene can be selected by index rather than by fragile name matching.
        bl_scenes: list["bpy.types.Scene"] = []

        for scene_idx, gltf_scene in enumerate(self.gltf.scenes):
            if scene_idx == 0:
                # Use the existing active scene for the first glTF scene
                bl_scene = context.scene
            else:
                bl_scene = bpy.data.scenes.new(gltf_scene.name or f"Scene_{scene_idx}")

            if gltf_scene.name:
                bl_scene.name = gltf_scene.name

            bl_scenes.append(bl_scene)
            context.window.scene = bl_scene
            collection = bl_scene.collection

            if gltf_scene.nodes:
                for node_index in gltf_scene.nodes:
                    self._import_node(context, node_index, collection, parent_obj=None)

            # Scene-level (global) audio emitters
            if self.audio_importer:
                self.audio_importer.import_global_emitters(gltf_scene, collection)

        # Switch to the active scene as indicated by the glTF, indexing the
        # scenes we just created rather than matching by name.
        if 0 <= active_scene_index < len(bl_scenes):
            context.window.scene = bl_scenes[active_scene_index]
        else:
            context.window.scene = original_scene

        return self.node_to_blender

    def _resolve_external_collection(
        self, context: "bpy.types.Context", file_index: int,
    ) -> "bpy.types.Collection | None":
        """Import a glTF 2.1 [DRAFT] external/packaged asset once into a fresh,
        unlinked collection that host nodes instance. Cached per file index."""
        import bpy
        from ..importer import import_subasset, MAX_EXTERNAL_DEPTH

        if file_index in self._imported_files:
            return self._imported_files[file_index]
        if self.external_depth >= MAX_EXTERNAL_DEPTH:
            print(
                f"[glTF import] external-asset nesting exceeded depth "
                f"{MAX_EXTERNAL_DEPTH}; skipping file {file_index}"
            )
            return None

        files = self.gltf.files or []
        if file_index >= len(files):
            return None
        name = files[file_index].name or f"ExternalAsset_{file_index}"
        coll = bpy.data.collections.new(name)
        # Cache before recursing so a self-referential file can't loop forever.
        self._imported_files[file_index] = coll

        sub_dict, sub_binary = self.file_resolver.load(file_index)
        import_subasset(
            context, sub_dict, sub_binary, self.file_resolver.base_dir,
            coll, self.settings, external_depth=self.external_depth + 1,
        )
        return coll

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
        elif (self.audio_importer and node.extensions
                and "KHR_audio_emitter" in node.extensions):
            emitter_idx = node.extensions["KHR_audio_emitter"].get("emitter")
            if emitter_idx is not None:
                obj_data = self.audio_importer.create_speaker_data(emitter_idx)

        obj = bpy.data.objects.new(name, obj_data)
        collection.objects.link(obj)
        self._apply_transform(obj, node)

        if parent_obj:
            obj.parent = parent_obj

        # KHR_node_visibility
        if node.extensions and "KHR_node_visibility" in node.extensions:
            vis = node.extensions["KHR_node_visibility"]
            if not vis.get("visible", True):
                obj.hide_render = True
                # hide_set is view-layer based; an object imported into an
                # unlinked sub-asset collection isn't in the view layer.
                try:
                    obj.hide_set(True)
                except RuntimeError:
                    obj.hide_viewport = True

        self.node_to_blender[node_index] = obj

        # glTF 2.1 [DRAFT] external asset: turn this node into a Blender
        # collection instance pointing at the (recursively) imported sub-asset.
        if getattr(node, "file", None) is not None and self.file_resolver is not None:
            sub_coll = self._resolve_external_collection(context, node.file)
            if sub_coll is not None:
                obj.instance_type = "COLLECTION"
                obj.instance_collection = sub_coll

        # Handle skinned mesh: apply weights to pre-created armature or create one
        if (node.skin is not None and self.skin_importer
                and self.settings.import_skinning):
            if node.skin in self._skin_armatures:
                # Armature was already created at its wrapper node
                armature_obj = self._skin_armatures[node.skin]
                self.skin_importer.apply_skin_to_mesh(
                    obj, node.skin, armature_obj, node.mesh,
                )
            else:
                # Fallback: create armature at the mesh node
                context.view_layer.update()
                arm_world = self._get_node_world_matrix(node, parent_obj)
                armature_obj = self.skin_importer.import_skin(
                    context, node.skin, obj, collection, arm_world,
                    mesh_index=node.mesh,
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

        # Custom properties from extras
        if node.extras and isinstance(node.extras, dict):
            for key, value in node.extras.items():
                obj[key] = value

        # glTF 2.1 [DRAFT] unique ID -> custom property (round-trips on re-export)
        if getattr(node, "uid", None) and self.settings.import_uids:
            obj["gltf_uid"] = node.uid

        # Physics (rigid body / collider)
        if self.physics_importer and self.settings.import_physics:
            self.physics_importer.import_node(context, obj, node, node_index)

        # Particle systems
        if self.particle_importer and self.settings.import_particles:
            self.particle_importer.import_node(context, obj, node)

        # KHR_interactivity behavior graph
        if self.interactivity_importer and self.settings.import_interactivity:
            self.interactivity_importer.import_node(context, obj, node)

        # Recurse children
        if node.children:
            for child_index in node.children:
                self._import_node(context, child_index, collection, parent_obj=obj)

        # MSFT_lod: the referenced LOD nodes live outside the scene graph, so
        # this is the only path that materializes them. Hide them like an
        # artist-authored LOD set.
        if node.extensions and "MSFT_lod" in node.extensions:
            for lod_index in node.extensions["MSFT_lod"].get("ids", []):
                self._import_node(context, lod_index, collection, parent_obj=obj.parent)
                lod_obj = self.node_to_blender.get(lod_index)
                if lod_obj is not None:
                    lod_obj.hide_render = True
                    try:
                        lod_obj.hide_set(True)
                    except RuntimeError:
                        lod_obj.hide_viewport = True
            if (isinstance(node.extras, dict)
                    and "MSFT_screencoverage" in node.extras
                    and "screencoverage" not in obj):
                obj["screencoverage"] = node.extras["MSFT_screencoverage"]

    def _create_camera(self, camera_index: int) -> "bpy.types.Camera | None":
        """Create a Blender camera from a glTF camera index."""
        import bpy

        if camera_index >= len(self.gltf.cameras):
            return None
        gltf_cam = self.gltf.cameras[camera_index]
        cam_name = gltf_cam.name or f"Camera_{camera_index}"
        cam = bpy.data.cameras.new(cam_name)

        if gltf_cam.type == "orthographic":
            cam.type = "ORTHO"
            ortho = gltf_cam.orthographic
            if ortho is not None:
                cam.ortho_scale = max(ortho.xmag, ortho.ymag) * 2.0
                cam.clip_start = ortho.znear
                cam.clip_end = ortho.zfar
        else:
            cam.type = "PERSP"
            persp = gltf_cam.perspective
            if persp is not None:
                cam.angle_y = persp.yfov
                cam.lens_unit = "FOV"
                cam.clip_start = persp.znear
                if persp.zfar is not None:
                    cam.clip_end = persp.zfar
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
            light.spot_blend = (
                max(0.0, min(1.0, 1.0 - inner / outer)) if outer > 0 else 0.0
            )

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
        """Reconstruct EXT_mesh_gpu_instancing as a single Geometry-Nodes
        "Instance on Points" object per source mesh.

        Instead of one collection-instance empty per instance (which litters the
        viewport with axis gizmos and parent-relationship lines), we build one
        point cloud whose points carry per-instance rotation/scale attributes,
        and a GN modifier that instances the (hidden) source object on those
        points. This is clean in the viewport and still re-exports as
        EXT_mesh_gpu_instancing, because the modifier emits real depsgraph
        instances referencing the source object.
        """
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

        # The number of instances is the length of whichever of
        # translation/rotation/scale is present (any single one suffices; a
        # file may carry only ROTATION or only SCALE). Default to 1 so a
        # degenerate node with no attributes still yields one instance.
        num_instances = 1
        for arr in (trans, rots, scales):
            if arr is not None:
                num_instances = len(arr)
                break

        # Gather source meshes (the node's own mesh plus any mesh children that
        # share these instance transforms).
        sources: list[tuple[str, int]] = []
        if node.mesh is not None:
            sources.append((node.name or f"Instance_{node_index}", node.mesh))
        if node.children and self.gltf.nodes:
            for child_idx in node.children:
                child_node = self.gltf.nodes[child_idx]
                if child_node.mesh is not None:
                    sources.append((child_node.name or "ChildMesh", child_node.mesh))
        if not sources:
            return

        # Build the shared point cloud (one point per instance) carrying the
        # per-instance rotation/scale as attributes the node group reads.
        points_mesh = bpy.data.meshes.new(f"{node.name or f'Instance_{node_index}'}_points")
        # Default absent translation to zeros at every instance, mirroring the
        # identity-quat / unit-scale tiling done below for rotation/scale.
        if trans is not None:
            verts = [tuple(t) for t in trans]
        else:
            verts = [(0.0, 0.0, 0.0)] * num_instances
        points_mesh.from_pydata(verts, [], [])
        points_mesh.update()

        rot_attr = points_mesh.attributes.new("instance_rotation", "QUATERNION", "POINT")
        if rots is not None:
            rot_flat = np.ascontiguousarray(rots, dtype=np.float32).reshape(-1)
        else:
            rot_flat = np.tile(np.array([1, 0, 0, 0], dtype=np.float32), num_instances)
        rot_attr.data.foreach_set("value", rot_flat)

        scl_attr = points_mesh.attributes.new("instance_scale", "FLOAT_VECTOR", "POINT")
        if scales is not None:
            scl_flat = np.ascontiguousarray(scales, dtype=np.float32).reshape(-1)
        else:
            scl_flat = np.tile(np.array([1, 1, 1], dtype=np.float32), num_instances)
        scl_attr.data.foreach_set("vector", scl_flat)

        instances_coll = self._get_instances_collection(collection)
        sources_coll = self._get_sources_collection(collection)

        for s_idx, (src_name, mesh_idx) in enumerate(sources):
            blender_mesh = self.mesh_importer.blender_meshes.get(mesh_idx)
            if blender_mesh is None:
                continue

            # Hidden source object that the GN modifier instances. It stays in
            # the depsgraph (hide via the eye, not "disable in viewports") so
            # Object Info can read its geometry; hide_render keeps it from
            # rendering as a stray copy at the origin.
            source_obj = bpy.data.objects.new(f"{blender_mesh.name}_source", blender_mesh)
            sources_coll.objects.link(source_obj)
            source_obj.hide_render = True
            source_obj.hide_set(True)

            # The instancer: shares the single point cloud across sources.
            inst_name = src_name if s_idx == 0 else f"{src_name}_instances"
            points_obj = bpy.data.objects.new(inst_name, points_mesh)
            instances_coll.objects.link(points_obj)

            mod = points_obj.modifiers.new("GLTF Instances", "NODES")
            mod.node_group = self._build_instance_node_group(source_obj)

            # Instance transforms are in the node's local space; carry the
            # parent on the instancer object so only one relationship exists.
            if parent_obj:
                points_obj.parent = parent_obj

            if s_idx == 0:
                self.node_to_blender[node_index] = points_obj

    def _build_instance_node_group(
        self, source_obj: "bpy.types.Object",
    ) -> "bpy.types.NodeTree":
        """Build an 'Instance on Points' node group bound to one source object.

        The source is set on the Object Info node's Object input directly (its
        default_value), rather than as a modifier-level input, because Blender
        5.x NodesModifier no longer exposes the socket id-property interface.
        """
        import bpy

        ng = bpy.data.node_groups.new("GLTF Instance on Points", "GeometryNodeTree")
        iface = ng.interface
        iface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
        iface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")

        nodes = ng.nodes
        links = ng.links
        n_in = nodes.new("NodeGroupInput"); n_in.location = (-500, 0)
        n_out = nodes.new("NodeGroupOutput"); n_out.location = (400, 0)
        iop = nodes.new("GeometryNodeInstanceOnPoints"); iop.location = (150, 0)
        oinfo = nodes.new("GeometryNodeObjectInfo"); oinfo.location = (-200, -220)
        oinfo.transform_space = "ORIGINAL"
        if "As Instance" in oinfo.inputs:
            oinfo.inputs["As Instance"].default_value = True
        oinfo.inputs["Object"].default_value = source_obj

        n_rot = nodes.new("GeometryNodeInputNamedAttribute"); n_rot.location = (-200, 240)
        n_rot.data_type = "QUATERNION"
        n_rot.inputs["Name"].default_value = "instance_rotation"
        n_scl = nodes.new("GeometryNodeInputNamedAttribute"); n_scl.location = (-200, 60)
        n_scl.data_type = "FLOAT_VECTOR"
        n_scl.inputs["Name"].default_value = "instance_scale"

        links.new(n_in.outputs["Geometry"], iop.inputs["Points"])
        links.new(oinfo.outputs["Geometry"], iop.inputs["Instance"])
        links.new(n_rot.outputs["Attribute"], iop.inputs["Rotation"])
        links.new(n_scl.outputs["Attribute"], iop.inputs["Scale"])
        links.new(iop.outputs["Instances"], n_out.inputs["Geometry"])

        return ng

    def _get_instances_collection(
        self, parent: "bpy.types.Collection",
    ) -> "bpy.types.Collection":
        """Collection holding the visible GN instancer objects, cached per
        parent collection so multi-scene files get one per scene."""
        import bpy
        key = parent.as_pointer()
        coll = self._instances_colls.get(key)
        if coll is None:
            coll = bpy.data.collections.new("Instances")
            parent.children.link(coll)
            self._instances_colls[key] = coll
        return coll

    def _get_sources_collection(
        self, parent: "bpy.types.Collection",
    ) -> "bpy.types.Collection":
        """Collection holding the hidden source objects that get instanced,
        cached per parent collection (one per scene)."""
        import bpy
        key = parent.as_pointer()
        coll = self._sources_colls.get(key)
        if coll is None:
            coll = bpy.data.collections.new("Instance Sources")
            parent.children.link(coll)
            self._sources_colls[key] = coll
        return coll
