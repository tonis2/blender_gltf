from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from ..gltf.buffer import BufferBuilder
from ..gltf.constants import ComponentType, DataType, BufferViewTarget
from ..gltf.types import Mesh, MeshPrimitive
from .converter import convert_positions, convert_normals, flip_uv_v
from . import quantize

if TYPE_CHECKING:
    import bpy
    from ..exporter import ExportSettings


class MeshExporter:
    def __init__(self, buffer: BufferBuilder, settings: "ExportSettings") -> None:
        self.buffer = buffer
        self.settings = settings
        self.meshes: list[Mesh] = []
        self._cache: dict[str, int] = {}
        self.used_quantization = False
        # mesh index -> (translation, scale) a wrapper node needs to undo
        # POSITION quantization. Absent for unquantized/skinned meshes.
        self.dequant_transforms: dict[int, tuple[list[float], list[float]]] = {}
        self._last_dequant: tuple[list[float], list[float]] | None = None

    def gather(
        self,
        blender_object: "bpy.types.Object",
        material_map: dict[int, int] | None = None,
        skin_joint_map: dict[str, int] | None = None,
    ) -> int | None:
        """Export mesh data from a Blender object. Returns mesh index or None."""
        import bpy

        # Accept any type whose post-modifier evaluation can yield mesh data.
        # MESH/CURVE/SURFACE/FONT/META go through the legacy `to_mesh()` path.
        # CURVES/GREASEPENCIL use the Blender 5.x `evaluated_geometry().mesh`
        # API since `to_mesh()` raises "Object does not have geometry data" on
        # those types — but their GN modifiers can still emit mesh output.
        legacy_to_mesh_types = {"MESH", "CURVE", "SURFACE", "FONT", "META"}
        geometry_set_types = {"CURVES", "GREASEPENCIL"}
        if blender_object.type not in legacy_to_mesh_types | geometry_set_types:
            return None
        use_geometry_set = blender_object.type in geometry_set_types

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
            if use_geometry_set:
                geo = eval_obj.evaluated_geometry()
                blender_mesh = geo.mesh if geo is not None else None
            else:
                blender_mesh = eval_obj.to_mesh()
        finally:
            if armature_mod is not None:
                armature_mod.show_viewport = armature_was_visible

        # to_mesh() owns its mesh and needs to_mesh_clear() to free it;
        # evaluated_geometry().mesh is owned by the GeometrySet — never clear.
        def _release_mesh():
            if not use_geometry_set:
                eval_obj.to_mesh_clear()

        if blender_mesh is None or len(blender_mesh.vertices) == 0:
            _release_mesh()
            return None

        # Cache key must uniquely identify the *source* geometry so that linked
        # duplicates (Alt+D: shared data + identical modifiers) dedupe, while
        # genuinely distinct objects never collide. The evaluated mesh name is
        # unreliable: to_mesh() names its result after the original datablock,
        # and datablocks of different ID types can share a name — e.g. a CURVE
        # named "GPencil" and a MESH named "GPencil" both yield to_mesh().name
        # == "GPencil", which previously made the door arches dedupe into the
        # vine stem mesh (dropping the arch geometry, cloning vines onto houses).
        # Key on the original data's pointer plus a modifier-stack signature.
        src = blender_object.original
        key_parts = [str(src.data.as_pointer()) if src.data else str(src.as_pointer())]
        for mod in src.modifiers:
            key_parts.append(mod.type)
            ng = getattr(mod, "node_group", None)
            if ng is not None:
                key_parts.append(str(ng.as_pointer()))
        cache_key = "|".join(key_parts)
        if cache_key in self._cache:
            _release_mesh()
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
            _release_mesh()

        if mesh is None:
            return None

        index = len(self.meshes)
        self.meshes.append(mesh)
        self._cache[cache_key] = index
        if self._last_dequant is not None:
            self.dequant_transforms[index] = self._last_dequant
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

        # MikkTSpace tangents need normals, a UV map, and no n-gon faces.
        has_tangents = False
        if (
            self.settings.export_tangents
            and self.settings.export_normals
            and self.settings.export_texcoords
            and blender_mesh.uv_layers
        ):
            try:
                # Tangents must come from the layer exported as TEXCOORD_0.
                blender_mesh.calc_tangents(uvmap=blender_mesh.uv_layers[0].name)
                has_tangents = True
            except RuntimeError:
                print(f"glTF export: cannot compute tangents for '{name}' (n-gons present), skipping")

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

        # One AABB for the whole mesh so every primitive shares the same
        # dequantization wrapper node. Skinned meshes keep float positions:
        # node TRS is ignored on skinned nodes, so the wrapper would be lost.
        self._last_dequant = None
        pos_quant = None
        if self.settings.export_quantization and joint_data is None:
            offset, scale = quantize.position_dequant_transform(positions)
            pos_quant = (offset, scale)
            self._last_dequant = (offset.tolist(), scale.tolist())

        # Normals (per loop/corner)
        normals = np.empty(len(blender_mesh.loops) * 3, dtype=np.float32)
        blender_mesh.corner_normals.foreach_get("vector", normals)
        normals = normals.reshape(-1, 3)
        convert_normals(normals)

        # Tangents (per loop/corner), valid only after calc_tangents()
        tangents = None
        bitangent_signs = None
        if has_tangents:
            tangents = np.empty(len(blender_mesh.loops) * 3, dtype=np.float32)
            blender_mesh.loops.foreach_get("tangent", tangents)
            tangents = tangents.reshape(-1, 3)
            convert_normals(tangents)
            bitangent_signs = np.empty(len(blender_mesh.loops), dtype=np.float32)
            blender_mesh.loops.foreach_get("bitangent_sign", bitangent_signs)

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
        if has_tangents:
            dot_fields.extend([("tx", "f4"), ("ty", "f4"), ("tz", "f4"), ("tw", "f4")])
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
        if has_tangents:
            dots["tx"] = tangents[:, 0]
            dots["ty"] = tangents[:, 1]
            dots["tz"] = tangents[:, 2]
            # bitangent_sign is used as-is: MikkTSpace works in V-down image
            # space, which already matches glTF's UV convention, so the
            # exporter's V-flip needs no sign compensation (same as
            # io_scene_gltf2).
            dots["tw"] = bitangent_signs
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
                has_tangents=has_tangents,
                pos_quant=pos_quant,
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
        has_tangents: bool = False,
        pos_quant: tuple[np.ndarray, np.ndarray] | None = None,
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

        # Default to UNSIGNED_INT (uint32) so consumers can concatenate primitives
        # into a global vertex buffer without ushort wrap. Per-primitive ubyte/ushort
        # would save space but break renderers that share index buffers across
        # primitives (the tank_game project does this).
        index_type = ComponentType.UNSIGNED_INT
        indices = remap_indices.astype(index_type.numpy_dtype)

        # Add to buffer
        attributes: dict[str, int] = {}

        quantizing = self.settings.export_quantization

        if pos_quant is not None:
            offset, scale = pos_quant
            attributes["POSITION"] = self.buffer.add_accessor(
                quantize.quantize_positions(prim_positions, offset, scale),
                ComponentType.UNSIGNED_SHORT, DataType.VEC3,
                target=BufferViewTarget.ARRAY_BUFFER, include_bounds=True,
                normalized=True, byte_stride=8,
            )
            self.used_quantization = True
        else:
            attributes["POSITION"] = self.buffer.add_accessor(
                prim_positions, ComponentType.FLOAT, DataType.VEC3,
                target=BufferViewTarget.ARRAY_BUFFER, include_bounds=True,
            )

        if quantizing:
            attributes["NORMAL"] = self.buffer.add_accessor(
                quantize.quantize_normals(prim_normals),
                ComponentType.BYTE, DataType.VEC3,
                target=BufferViewTarget.ARRAY_BUFFER,
                normalized=True, byte_stride=4,
            )
            self.used_quantization = True
        else:
            attributes["NORMAL"] = self.buffer.add_accessor(
                prim_normals, ComponentType.FLOAT, DataType.VEC3,
                target=BufferViewTarget.ARRAY_BUFFER,
            )

        if has_tangents:
            prim_tangents = np.column_stack([
                unique_dots["tx"], unique_dots["ty"],
                unique_dots["tz"], unique_dots["tw"],
            ])
            if quantizing:
                attributes["TANGENT"] = self.buffer.add_accessor(
                    quantize.quantize_tangents(prim_tangents),
                    ComponentType.BYTE, DataType.VEC4,
                    target=BufferViewTarget.ARRAY_BUFFER, normalized=True,
                )
            else:
                attributes["TANGENT"] = self.buffer.add_accessor(
                    prim_tangents, ComponentType.FLOAT, DataType.VEC4,
                    target=BufferViewTarget.ARRAY_BUFFER,
                )

        for i in range(num_uv_layers):
            uv_data = np.column_stack([
                unique_dots[f"uv{i}_u"], unique_dots[f"uv{i}_v"],
            ])
            if quantizing and quantize.can_quantize_uvs(uv_data):
                attributes[f"TEXCOORD_{i}"] = self.buffer.add_accessor(
                    quantize.quantize_uvs(uv_data),
                    ComponentType.UNSIGNED_SHORT, DataType.VEC2,
                    target=BufferViewTarget.ARRAY_BUFFER, normalized=True,
                )
                self.used_quantization = True
            else:
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
                if pos_quant is not None:
                    # Deltas are added to POSITION before the dequant node
                    # scale is applied, so bring them into normalized space.
                    prim_deltas = prim_deltas / pos_quant[1]
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
