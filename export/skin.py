from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from ..gltf.buffer import BufferBuilder
from ..gltf.constants import ComponentType, DataType
from ..gltf.types import Node, Skin
from .converter import convert_matrix, trs_to_node_fields

if TYPE_CHECKING:
    import bpy
    from ..exporter import ExportSettings


class SkinExporter:
    def __init__(self, buffer: BufferBuilder, settings: "ExportSettings") -> None:
        self.buffer = buffer
        self.settings = settings
        self.skins: list[Skin] = []
        # (armature_object_name, bone_name) -> node_index (for animation export).
        # Keyed per armature so equal bone names across armatures can't collide.
        self.bone_to_node_index: dict[tuple[str, str], int] = {}
        # armature_name -> {bone_name: joint_index_in_skin}
        self.armature_joint_maps: dict[str, dict[str, int]] = {}
        # armature_name -> skin_index
        self.armature_skin_index: dict[str, int] = {}

    def gather_armature(
        self,
        armature_obj: "bpy.types.Object",
        armature_node_index: int,
        nodes: list[Node],
    ) -> list[int]:
        """Create bone nodes and Skin for an armature.

        Returns list of root bone node indices to add as children of the armature node.
        """
        armature = armature_obj.data
        bones = list(armature.bones)
        if not bones:
            return []

        # Create nodes for all bones, track indices
        bone_node_indices: dict[str, int] = {}
        joint_indices: list[int] = []  # ordered joint list for Skin

        for bone in bones:
            # Compute local TRS relative to parent
            if bone.parent:
                local_mat = bone.parent.matrix_local.inverted() @ bone.matrix_local
            else:
                local_mat = bone.matrix_local

            translation, rotation, gltf_scale = trs_to_node_fields(local_mat)

            node = Node(
                name=bone.name,
                translation=translation,
                rotation=rotation,
                scale=gltf_scale,
            )

            node_index = len(nodes)
            nodes.append(node)
            bone_node_indices[bone.name] = node_index
            joint_indices.append(node_index)
            self.bone_to_node_index[(armature_obj.name, bone.name)] = node_index

        # Wire children
        for bone in bones:
            if bone.children:
                parent_idx = bone_node_indices[bone.name]
                child_indices = [bone_node_indices[c.name] for c in bone.children]
                nodes[parent_idx].children = child_indices

        # Root bone indices
        root_indices = [bone_node_indices[b.name] for b in bones if b.parent is None]

        # Compute inverse bind matrices
        ibm_data = np.empty((len(bones), 16), dtype=np.float32)
        for i, bone in enumerate(bones):
            world_mat = armature_obj.matrix_world @ bone.matrix_local
            ibm = convert_matrix(world_mat.inverted())
            ibm_data[i] = ibm

        ibm_acc = self.buffer.add_accessor(
            ibm_data, ComponentType.FLOAT, DataType.MAT4,
        )

        # Build joint map: bone_name -> index in joints list
        joint_map = {bone.name: i for i, bone in enumerate(bones)}
        self.armature_joint_maps[armature_obj.name] = joint_map

        # Create Skin
        skin = Skin(
            joints=joint_indices,
            inverse_bind_matrices=ibm_acc,
            skeleton=root_indices[0] if root_indices else None,
            name=armature_obj.name,
        )
        skin_index = len(self.skins)
        self.skins.append(skin)
        self.armature_skin_index[armature_obj.name] = skin_index

        return root_indices
