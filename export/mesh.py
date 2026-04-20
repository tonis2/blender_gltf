from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from ..gltf.buffer import BufferBuilder
from ..gltf.constants import ComponentType, DataType, BufferViewTarget
from ..gltf.types import Mesh, MeshPrimitive
from .converter import convert_positions, convert_normals, flip_uv_v

if TYPE_CHECKING:
    import bpy
    from ..exporter import ExportSettings


class MeshExporter:
    def __init__(self, buffer: BufferBuilder, settings: "ExportSettings") -> None:
        self.buffer = buffer
        self.settings = settings
        self.meshes: list[Mesh] = []
        self._cache: dict[str, int] = {}

    def gather(
        self,
        blender_object: "bpy.types.Object",
        material_map: dict[int, int] | None = None,
        skin_joint_map: dict[str, int] | None = None,
    ) -> int | None:
        """Export mesh data from a Blender object. Returns mesh index or None."""
        import bpy

        if blender_object.type != "MESH":
            return None

        # Temporarily disable armature modifier for rest-pose vertices
        armature_mod = None
        armature_was_visible = None
        if skin_joint_map is not None:
            for mod in blender_object.modifiers:
                if mod.type == "ARMATURE":
                    armature_mod = mod
                    armature_was_visible = mod.show_viewport
                    mod.show_viewport = False
                    break

        try:
            depsgraph = bpy.context.evaluated_depsgraph_get()
            eval_obj = blender_object.evaluated_get(depsgraph)
            blender_mesh = eval_obj.to_mesh()
        finally:
            if armature_mod is not None:
                armature_mod.show_viewport = armature_was_visible

        if blender_mesh is None or len(blender_mesh.vertices) == 0:
            eval_obj.to_mesh_clear()
            return None

        cache_key = blender_mesh.name
        if cache_key in self._cache:
            eval_obj.to_mesh_clear()
            return self._cache[cache_key]

        # Extract shape key deltas before evaluation (shape keys live on original data)
        shape_key_data = None
        if self.settings.export_morph_targets:
            shape_key_data = self._extract_shape_keys(blender_object)

        # Extract vertex weights for skinning
        joint_data = None
        weight_data = None
        if skin_joint_map is not None:
            joint_data, weight_data = self._extract_vertex_weights(
                blender_mesh, blender_object, skin_joint_map,
            )

        try:
            mesh = self._extract(
                blender_mesh, blender_object, material_map,
                shape_key_data, joint_data, weight_data,
            )
        finally:
            eval_obj.to_mesh_clear()

        if mesh is None:
            return None

        index = len(self.meshes)
        self.meshes.append(mesh)
        self._cache[cache_key] = index
        return index

    def _extract_shape_keys(
        self, blender_object: "bpy.types.Object",
    ) -> list[tuple[str, np.ndarray]] | None:
        """Extract shape key position deltas from the original mesh data.
        Returns list of (name, delta_positions) or None."""
        if not hasattr(blender_object.data, "shape_keys") or blender_object.data.shape_keys is None:
            return None

        key_blocks = blender_object.data.shape_keys.key_blocks
        if len(key_blocks) < 2:
            return None

        # Extract basis positions
        basis = key_blocks[0]
        num_verts = len(basis.data)
        basis_co = np.empty(num_verts * 3, dtype=np.float32)
        basis.data.foreach_get("co", basis_co)
        basis_co = basis_co.reshape(-1, 3)

        results: list[tuple[str, np.ndarray]] = []
        for kb in key_blocks[1:]:
            co = np.empty(num_verts * 3, dtype=np.float32)
            kb.data.foreach_get("co", co)
            co = co.reshape(-1, 3)
            delta = co - basis_co
            convert_positions(delta)
            results.append((kb.name, delta))

        return results

    def _extract_vertex_weights(
        self,
        blender_mesh: "bpy.types.Mesh",
        blender_object: "bpy.types.Object",
        joint_map: dict[str, int],
    ) -> tuple[np.ndarray, np.ndarray]:
        """Extract per-vertex bone weights. Returns (joints, weights) arrays of shape (N, 4)."""
        num_verts = len(blender_mesh.vertices)
        joints = np.zeros((num_verts, 4), dtype=np.uint16)
        weights = np.zeros((num_verts, 4), dtype=np.float32)

        # Build vertex group index -> joint index mapping
        group_to_joint: dict[int, int] = {}
        for vg in blender_object.vertex_groups:
            if vg.name in joint_map:
                group_to_joint[vg.index] = joint_map[vg.name]

        for v_idx, vert in enumerate(blender_mesh.vertices):
            bone_weights: list[tuple[int, float]] = []
            for g in vert.groups:
                if g.group in group_to_joint and g.weight > 0:
                    bone_weights.append((group_to_joint[g.group], g.weight))

            # Sort by weight descending, take top 4
            bone_weights.sort(key=lambda x: x[1], reverse=True)
            bone_weights = bone_weights[:4]

            # Normalize
            total = sum(w for _, w in bone_weights)
            if total > 0:
                for i, (j, w) in enumerate(bone_weights):
                    joints[v_idx, i] = j
                    weights[v_idx, i] = w / total

        return joints, weights

    def _extract(
        self,
        blender_mesh: "bpy.types.Mesh",
        blender_object: "bpy.types.Object",
        material_map: dict[int, int] | None = None,
        shape_key_data: list[tuple[str, np.ndarray]] | None = None,
        joint_data: np.ndarray | None = None,
        weight_data: np.ndarray | None = None,
    ) -> Mesh | None:
        name = blender_object.name
        blender_mesh.calc_loop_triangles()
        if len(blender_mesh.loop_triangles) == 0:
            return None

        # --- Extract raw data from Blender ---

        # Loop triangle indices (each triangle = 3 loop indices)
        loop_indices = np.empty(len(blender_mesh.loop_triangles) * 3, dtype=np.uint32)
        blender_mesh.loop_triangles.foreach_get("loops", loop_indices)

        # Per-triangle material index
        tri_mat_idx = np.empty(len(blender_mesh.loop_triangles), dtype=np.uint32)
        blender_mesh.loop_triangles.foreach_get("material_index", tri_mat_idx)

        # Corner vertex indices: loop_index -> vertex_index
        corner_verts = np.empty(len(blender_mesh.loops), dtype=np.int32)
        blender_mesh.attributes[".corner_vert"].data.foreach_get("value", corner_verts)
        corner_verts = corner_verts.astype(np.uint32)

        # Positions (per vertex)
        positions = np.empty(len(blender_mesh.vertices) * 3, dtype=np.float32)
        blender_mesh.vertices.foreach_get("co", positions)
        positions = positions.reshape(-1, 3)
        convert_positions(positions)

        # Normals (per loop/corner)
        normals = np.empty(len(blender_mesh.loops) * 3, dtype=np.float32)
        blender_mesh.corner_normals.foreach_get("vector", normals)
        normals = normals.reshape(-1, 3)
        convert_normals(normals)

        # UVs (per loop, for each layer)
        uv_arrays: list[np.ndarray] = []
        for uv_layer in blender_mesh.uv_layers:
            uvs = np.empty(len(blender_mesh.loops) * 2, dtype=np.float32)
            uv_layer.uv.foreach_get("vector", uvs)
            uvs = uvs.reshape(-1, 2)
            flip_uv_v(uvs)
            uv_arrays.append(uvs)

        # Vertex colors (per loop/corner)
        color_arrays: list[np.ndarray] = []
        for color_attr in blender_mesh.color_attributes:
            if color_attr.domain == "CORNER":
                colors = np.empty(len(blender_mesh.loops) * 4, dtype=np.float32)
                color_attr.data.foreach_get("color", colors)
                colors = colors.reshape(-1, 4)
                color_arrays.append(colors)
            elif color_attr.domain == "POINT":
                # Per-vertex colors need to be expanded to per-loop later
                colors = np.empty(len(blender_mesh.vertices) * 4, dtype=np.float32)
                color_attr.data.foreach_get("color", colors)
                colors = colors.reshape(-1, 4)
                color_arrays.append(colors)

        # --- Build dots structured array ---
        # Each dot represents one loop corner with all its attributes.
        # Unique dots become unique glTF vertices.

        dot_fields: list[tuple[str, str]] = [("vertex_index", "u4")]
        dot_fields.extend([("nx", "f4"), ("ny", "f4"), ("nz", "f4")])
        for i in range(len(uv_arrays)):
            dot_fields.extend([(f"uv{i}_u", "f4"), (f"uv{i}_v", "f4")])
        for i in range(len(color_arrays)):
            dot_fields.extend([
                (f"c{i}_r", "f4"), (f"c{i}_g", "f4"),
                (f"c{i}_b", "f4"), (f"c{i}_a", "f4"),
            ])

        num_loops = len(blender_mesh.loops)
        dots = np.empty(num_loops, dtype=np.dtype(dot_fields))
        dots["vertex_index"] = corner_verts
        dots["nx"] = normals[:, 0]
        dots["ny"] = normals[:, 1]
        dots["nz"] = normals[:, 2]
        for i, uvs in enumerate(uv_arrays):
            dots[f"uv{i}_u"] = uvs[:, 0]
            dots[f"uv{i}_v"] = uvs[:, 1]
        for i, colors in enumerate(color_arrays):
            if colors.shape[0] == num_loops:
                # Per-corner colors
                dots[f"c{i}_r"] = colors[:, 0]
                dots[f"c{i}_g"] = colors[:, 1]
                dots[f"c{i}_b"] = colors[:, 2]
                dots[f"c{i}_a"] = colors[:, 3]
            else:
                # Per-vertex colors: expand to per-loop using corner_verts
                dots[f"c{i}_r"] = colors[corner_verts, 0]
                dots[f"c{i}_g"] = colors[corner_verts, 1]
                dots[f"c{i}_b"] = colors[corner_verts, 2]
                dots[f"c{i}_a"] = colors[corner_verts, 3]

        # --- Split by material and build primitives ---
        unique_materials = np.unique(tri_mat_idx)
        primitives: list[MeshPrimitive] = []

        for mat_idx in unique_materials:
            tri_mask = tri_mat_idx == mat_idx
            loop_mask = np.repeat(tri_mask, 3)
            prim_loop_indices = loop_indices[loop_mask]

            # Map Blender material slot index to glTF material index
            gltf_mat_idx = None
            if material_map and int(mat_idx) in material_map:
                gltf_mat_idx = material_map[int(mat_idx)]

            prim = self._build_primitive(
                positions, dots, prim_loop_indices,
                len(uv_arrays), len(color_arrays), gltf_mat_idx,
                shape_key_data, joint_data, weight_data,
            )
            if prim is not None:
                primitives.append(prim)

        if not primitives:
            return None

        # Set default morph target weights from original shape keys
        weights = None
        if shape_key_data and blender_object.data.shape_keys:
            weights = [kb.value for kb in blender_object.data.shape_keys.key_blocks[1:]]

        return Mesh(primitives=primitives, name=name, weights=weights)

    def _build_primitive(
        self,
        all_positions: np.ndarray,
        all_dots: np.ndarray,
        loop_indices: np.ndarray,
        num_uv_layers: int,
        num_color_layers: int,
        material_index: int | None,
        shape_key_data: list[tuple[str, np.ndarray]] | None = None,
        joint_data: np.ndarray | None = None,
        weight_data: np.ndarray | None = None,
    ) -> MeshPrimitive | None:
        """Deduplicate vertices and create buffer accessors for one primitive."""
        # Extract dots for this primitive's loops
        prim_dots = all_dots[loop_indices]

        # Deduplicate: find unique vertex combinations
        unique_dots, remap_indices = np.unique(prim_dots, return_inverse=True)

        if len(unique_dots) == 0:
            return None

        # Look up positions for unique vertices
        vert_indices = unique_dots["vertex_index"]
        prim_positions = all_positions[vert_indices]

        # Extract normals
        prim_normals = np.column_stack([
            unique_dots["nx"], unique_dots["ny"], unique_dots["nz"],
        ])

        # Choose index component type based on vertex count
        num_verts = len(unique_dots)
        if num_verts <= 255:
            index_type = ComponentType.UNSIGNED_BYTE
        elif num_verts <= 65535:
            index_type = ComponentType.UNSIGNED_SHORT
        else:
            index_type = ComponentType.UNSIGNED_INT

        indices = remap_indices.astype(index_type.numpy_dtype)

        # Add to buffer
        attributes: dict[str, int] = {}

        attributes["POSITION"] = self.buffer.add_accessor(
            prim_positions, ComponentType.FLOAT, DataType.VEC3,
            target=BufferViewTarget.ARRAY_BUFFER, include_bounds=True,
        )

        attributes["NORMAL"] = self.buffer.add_accessor(
            prim_normals, ComponentType.FLOAT, DataType.VEC3,
            target=BufferViewTarget.ARRAY_BUFFER,
        )

        for i in range(num_uv_layers):
            uv_data = np.column_stack([
                unique_dots[f"uv{i}_u"], unique_dots[f"uv{i}_v"],
            ])
            attributes[f"TEXCOORD_{i}"] = self.buffer.add_accessor(
                uv_data, ComponentType.FLOAT, DataType.VEC2,
                target=BufferViewTarget.ARRAY_BUFFER,
            )

        for i in range(num_color_layers):
            color_data = np.column_stack([
                unique_dots[f"c{i}_r"], unique_dots[f"c{i}_g"],
                unique_dots[f"c{i}_b"], unique_dots[f"c{i}_a"],
            ])
            attributes[f"COLOR_{i}"] = self.buffer.add_accessor(
                color_data, ComponentType.FLOAT, DataType.VEC4,
                target=BufferViewTarget.ARRAY_BUFFER,
            )

        # Skinning attributes (per vertex, indexed like positions)
        if joint_data is not None and weight_data is not None:
            prim_joints = joint_data[vert_indices]
            prim_weights = weight_data[vert_indices]
            attributes["JOINTS_0"] = self.buffer.add_accessor(
                prim_joints, ComponentType.UNSIGNED_SHORT, DataType.VEC4,
                target=BufferViewTarget.ARRAY_BUFFER,
            )
            attributes["WEIGHTS_0"] = self.buffer.add_accessor(
                prim_weights, ComponentType.FLOAT, DataType.VEC4,
                target=BufferViewTarget.ARRAY_BUFFER,
            )

        # Morph targets (shape key deltas)
        targets = None
        if shape_key_data:
            targets = []
            for _name, all_deltas in shape_key_data:
                prim_deltas = all_deltas[vert_indices]
                target_acc = self.buffer.add_accessor(
                    prim_deltas, ComponentType.FLOAT, DataType.VEC3,
                    target=BufferViewTarget.ARRAY_BUFFER,
                )
                targets.append({"POSITION": target_acc})

        indices_accessor = self.buffer.add_accessor(
            indices, index_type, DataType.SCALAR,
            target=BufferViewTarget.ELEMENT_ARRAY_BUFFER,
        )

        return MeshPrimitive(
            attributes=attributes,
            indices=indices_accessor,
            material=material_index,
            targets=targets,
        )
