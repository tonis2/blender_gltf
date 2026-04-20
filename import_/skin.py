from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from .converter import convert_matrix, convert_location, convert_rotation, convert_scale

if TYPE_CHECKING:
    import bpy
    from ..gltf.types import Gltf
    from .buffer_reader import BufferReader
    from .mesh import MeshImporter
    from ..importer import ImportSettings


class SkinImporter:
    def __init__(
        self,
        gltf: "Gltf",
        buffer_reader: "BufferReader",
        mesh_importer: "MeshImporter",
        settings: "ImportSettings",
    ) -> None:
        self.gltf = gltf
        self.buffer_reader = buffer_reader
        self.mesh_importer = mesh_importer
        self.settings = settings
        # joint_node_index -> (armature_obj, bone_name) for animation import
        self.bone_node_to_armature: dict[int, tuple["bpy.types.Object", str]] = {}
        # All joint node indices across all skins (to skip during scene traversal)
        self.joint_node_indices: set[int] = set()

        # Pre-compute joint node indices
        if gltf.skins:
            for skin in gltf.skins:
                for j in skin.joints:
                    self.joint_node_indices.add(j)

    def import_skin(
        self,
        context: "bpy.types.Context",
        skin_index: int,
        mesh_obj: "bpy.types.Object",
        collection: "bpy.types.Collection",
    ) -> "bpy.types.Object":
        """Create an armature from a glTF skin, apply vertex weights, return armature object."""
        import bpy
        import mathutils

        skin = self.gltf.skins[skin_index]
        name = skin.name or f"Armature_{skin_index}"

        # Read inverse bind matrices
        ibms = None
        if skin.inverse_bind_matrices is not None:
            ibm_data = self.buffer_reader.read_accessor(skin.inverse_bind_matrices)
            # Shape: (num_joints, 16) for MAT4
            ibms = ibm_data.reshape(-1, 16)

        # Compute joint world transforms (for bone head/tail/roll)
        joint_bind_matrices: list[mathutils.Matrix] = []
        for i, joint_idx in enumerate(skin.joints):
            if ibms is not None:
                # IBM converts from mesh space to bone space in glTF coords
                # bind_matrix = inverse(IBM) converted to Blender coords
                ibm_gltf = ibms[i].tolist()
                ibm_blender = convert_matrix(ibm_gltf)
                bind_mat = ibm_blender.inverted()
            else:
                # No IBMs: compute from node hierarchy
                bind_mat = self._compute_node_world_transform(joint_idx)
            joint_bind_matrices.append(bind_mat)

        # Create armature
        armature_data = bpy.data.armatures.new(name)
        armature_obj = bpy.data.objects.new(name, armature_data)
        collection.objects.link(armature_obj)

        # Enter edit mode to create bones
        context.view_layer.objects.active = armature_obj
        bpy.ops.object.mode_set(mode="EDIT")

        joint_to_bone_name: dict[int, str] = {}
        edit_bones: dict[int, "bpy.types.EditBone"] = {}

        for i, joint_idx in enumerate(skin.joints):
            node = self.gltf.nodes[joint_idx]
            bone_name = node.name or f"Bone_{joint_idx}"
            bone = armature_data.edit_bones.new(bone_name)
            joint_to_bone_name[joint_idx] = bone.name  # Blender may rename
            edit_bones[joint_idx] = bone

            bind_mat = joint_bind_matrices[i]

            # Set bone head from bind matrix translation
            bone.head = bind_mat.translation

            # Determine bone length and direction
            bone_length = self._compute_bone_length(
                joint_idx, skin.joints, joint_bind_matrices,
            )

            # Bone Y-axis (direction from head to tail) from bind matrix column 1
            bone_y = bind_mat.to_3x3().col[1].normalized()
            bone.tail = bone.head + bone_y * bone_length

            # Set roll from bind matrix Z-axis
            bone_z = bind_mat.to_3x3().col[2]
            bone.align_roll(bone_z)

        # Set parent relationships
        for joint_idx in skin.joints:
            node = self.gltf.nodes[joint_idx]
            if node.children:
                for child_idx in node.children:
                    if child_idx in edit_bones:
                        edit_bones[child_idx].parent = edit_bones[joint_idx]

        bpy.ops.object.mode_set(mode="OBJECT")

        # Register bone node mappings for animation import
        for joint_idx, bone_name in joint_to_bone_name.items():
            self.bone_node_to_armature[joint_idx] = (armature_obj, bone_name)

        # Apply vertex weights to mesh
        if mesh_obj and mesh_obj.data:
            node = self._find_mesh_node(mesh_obj.name)
            if node is not None and node.mesh is not None:
                self._apply_vertex_weights(
                    mesh_obj, node.mesh, skin.joints, joint_to_bone_name,
                )

            # Add Armature modifier
            mod = mesh_obj.modifiers.new(name="Armature", type="ARMATURE")
            mod.object = armature_obj

        return armature_obj

    def _compute_bone_length(
        self,
        joint_idx: int,
        all_joints: list[int],
        bind_matrices: list,
    ) -> float:
        """Compute bone length from distance to first child, or default 0.1."""
        node = self.gltf.nodes[joint_idx]
        if node.children:
            # Find first child that is also a joint
            joint_set = set(all_joints)
            for child_idx in node.children:
                if child_idx in joint_set:
                    child_i = all_joints.index(child_idx)
                    parent_i = all_joints.index(joint_idx)
                    parent_head = bind_matrices[parent_i].translation
                    child_head = bind_matrices[child_i].translation
                    dist = (child_head - parent_head).length
                    if dist > 1e-4:
                        return dist
        return 0.1

    def _compute_node_world_transform(self, node_index: int):
        """Compute a node's world transform by walking up the parent chain."""
        import mathutils

        # Build parent map
        parent_map: dict[int, int] = {}
        if self.gltf.nodes:
            for i, node in enumerate(self.gltf.nodes):
                if node.children:
                    for child in node.children:
                        parent_map[child] = i

        # Accumulate transforms from root to this node
        chain = []
        idx = node_index
        while idx is not None:
            chain.append(idx)
            idx = parent_map.get(idx)
        chain.reverse()

        result = mathutils.Matrix.Identity(4)
        for idx in chain:
            node = self.gltf.nodes[idx]
            if node.matrix:
                m = node.matrix
                mat = mathutils.Matrix([
                    [m[0], m[4], m[8], m[12]],
                    [m[1], m[5], m[9], m[13]],
                    [m[2], m[6], m[10], m[14]],
                    [m[3], m[7], m[11], m[15]],
                ])
                # Convert to Blender coords
                loc, rot, scl = mat.decompose()
                loc = convert_location((loc.x, loc.y, loc.z))
                rot = convert_rotation((rot.x, rot.y, rot.z, rot.w))
                scl = convert_scale((scl.x, scl.y, scl.z))
                mat = mathutils.Matrix.LocRotScale(
                    mathutils.Vector(loc),
                    mathutils.Quaternion(rot),
                    mathutils.Vector(scl),
                )
                result = result @ mat
            else:
                local = mathutils.Matrix.Identity(4)
                if node.translation:
                    t = convert_location(node.translation)
                    local = local @ mathutils.Matrix.Translation(t)
                if node.rotation:
                    r = convert_rotation(node.rotation)
                    local = local @ mathutils.Quaternion(r).to_matrix().to_4x4()
                if node.scale:
                    s = convert_scale(node.scale)
                    local = local @ mathutils.Matrix.Diagonal(
                        mathutils.Vector((*s, 1.0))
                    )
                result = result @ local

        return result

    def _find_mesh_node(self, obj_name: str):
        """Find a glTF node by name."""
        if self.gltf.nodes:
            for node in self.gltf.nodes:
                if node.name == obj_name:
                    return node
        return None

    def _apply_vertex_weights(
        self,
        mesh_obj: "bpy.types.Object",
        mesh_index: int,
        joints: list[int],
        joint_to_bone_name: dict[int, str],
    ) -> None:
        """Create vertex groups and assign weights from JOINTS_0/WEIGHTS_0 data."""
        skin_data = self.mesh_importer.skin_data.get(mesh_index)
        if not skin_data:
            return

        # Create vertex groups for all joints
        joint_idx_to_group: dict[int, str] = {}
        for i, joint_idx in enumerate(joints):
            bone_name = joint_to_bone_name.get(joint_idx, f"Joint_{i}")
            if bone_name not in mesh_obj.vertex_groups:
                mesh_obj.vertex_groups.new(name=bone_name)
            joint_idx_to_group[i] = bone_name

        # Apply weights from each primitive's data
        for joint_array, weight_array, vert_offset in skin_data:
            for v_local in range(len(joint_array)):
                v_global = v_local + vert_offset
                for k in range(4):
                    w = float(weight_array[v_local, k])
                    if w > 0:
                        j = int(joint_array[v_local, k])
                        group_name = joint_idx_to_group.get(j)
                        if group_name:
                            vg = mesh_obj.vertex_groups[group_name]
                            vg.add([v_global], w, "REPLACE")
