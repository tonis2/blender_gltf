from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from .converter import convert_positions, convert_normals, flip_uv_v

if TYPE_CHECKING:
    import bpy
    from ..gltf.types import Gltf, Mesh as GltfMesh
    from .buffer_reader import BufferReader
    from .material import MaterialImporter
    from ..importer import ImportSettings


class MeshImporter:
    def __init__(
        self,
        gltf: "Gltf",
        buffer_reader: "BufferReader",
        material_importer: "MaterialImporter",
        settings: "ImportSettings",
    ) -> None:
        self.gltf = gltf
        self.buffer_reader = buffer_reader
        self.material_importer = material_importer
        self.settings = settings
        self.blender_meshes: dict[int, "bpy.types.Mesh"] = {}
        # Skin data: mesh_index -> list of (joints_array, weights_array, vertex_offset)
        self.skin_data: dict[int, list[tuple[np.ndarray, np.ndarray, int]]] = {}

    def import_all(self) -> None:
        if self.gltf.meshes is None:
            return
        for i, gltf_mesh in enumerate(self.gltf.meshes):
            self.blender_meshes[i] = self._import_mesh(i, gltf_mesh)

    def _import_mesh(self, index: int, gltf_mesh: "GltfMesh") -> "bpy.types.Mesh":
        import bpy

        name = gltf_mesh.name or f"Mesh_{index}"
        mesh = bpy.data.meshes.new(name)

        all_verts: list[np.ndarray] = []
        all_loop_verts: list[np.ndarray] = []
        all_mat_indices: list[int] = []
        all_normals: list[tuple[int, np.ndarray, np.ndarray]] = []
        all_uvs: list[tuple[int, int, np.ndarray, np.ndarray]] = []
        all_colors: list[tuple[int, int, np.ndarray, np.ndarray]] = []
        vertex_offset = 0
        num_uv_layers = 0
        num_color_layers = 0

        for prim_idx, prim in enumerate(gltf_mesh.primitives):
            if "POSITION" not in prim.attributes:
                continue

            positions = self.buffer_reader.read_accessor(prim.attributes["POSITION"])
            positions = convert_positions(positions)
            num_verts = len(positions)

            if prim.indices is not None:
                indices = self.buffer_reader.read_accessor(prim.indices).flatten().astype(np.uint32)
            else:
                indices = np.arange(num_verts, dtype=np.uint32)

            # Triangles: offset by accumulated vertex count
            loop_verts = indices + vertex_offset
            num_tris = len(indices) // 3

            all_verts.append(positions)
            all_loop_verts.append(loop_verts)
            all_mat_indices.extend([prim_idx] * num_tris)

            # Normals
            if "NORMAL" in prim.attributes and self.settings.import_normals:
                normals = self.buffer_reader.read_accessor(prim.attributes["NORMAL"])
                normals = convert_normals(normals)
                all_normals.append((vertex_offset, normals, indices))

            # UVs
            uv_idx = 0
            while f"TEXCOORD_{uv_idx}" in prim.attributes:
                if self.settings.import_texcoords:
                    uvs = self.buffer_reader.read_accessor(prim.attributes[f"TEXCOORD_{uv_idx}"])
                    uvs = flip_uv_v(uvs)
                    all_uvs.append((uv_idx, vertex_offset, uvs, indices))
                uv_idx += 1
            num_uv_layers = max(num_uv_layers, uv_idx)

            # Vertex colors
            color_idx = 0
            while f"COLOR_{color_idx}" in prim.attributes:
                if self.settings.import_colors:
                    colors = self.buffer_reader.read_accessor(prim.attributes[f"COLOR_{color_idx}"])
                    all_colors.append((color_idx, vertex_offset, colors, indices))
                color_idx += 1
            num_color_layers = max(num_color_layers, color_idx)

            # Skinning data (JOINTS_0 / WEIGHTS_0)
            if "JOINTS_0" in prim.attributes and "WEIGHTS_0" in prim.attributes:
                joints_acc = self.buffer_reader.read_accessor(prim.attributes["JOINTS_0"])
                weights_acc = self.buffer_reader.read_accessor(prim.attributes["WEIGHTS_0"])
                if index not in self.skin_data:
                    self.skin_data[index] = []
                self.skin_data[index].append((joints_acc, weights_acc, vertex_offset))

            vertex_offset += num_verts

        if not all_verts:
            return mesh

        # Build Blender mesh
        verts = np.concatenate(all_verts)
        loop_vertex_indices = np.concatenate(all_loop_verts)
        num_loops = len(loop_vertex_indices)
        num_polys = num_loops // 3

        mesh.vertices.add(len(verts))
        mesh.vertices.foreach_set("co", verts.flatten())

        mesh.loops.add(num_loops)
        mesh.loops.foreach_set("vertex_index", loop_vertex_indices.astype(np.int32))

        mesh.polygons.add(num_polys)
        loop_starts = np.arange(0, num_loops, 3, dtype=np.int32)
        loop_totals = np.full(num_polys, 3, dtype=np.int32)
        mesh.polygons.foreach_set("loop_start", loop_starts)
        mesh.polygons.foreach_set("loop_total", loop_totals)

        # Material slots
        for prim_idx, prim in enumerate(gltf_mesh.primitives):
            mat = None
            if prim.material is not None:
                mat = self.material_importer.get_blender_material(prim.material)
            if mat is None:
                import bpy
                mat = bpy.data.materials.new(f"{name}_mat_{prim_idx}")
            mesh.materials.append(mat)

        if all_mat_indices:
            mesh.polygons.foreach_set(
                "material_index", np.array(all_mat_indices, dtype=np.int32),
            )

        mesh.update()
        mesh.validate()

        # Custom normals
        if all_normals:
            self._apply_normals(mesh, all_normals, num_loops)

        # UV layers
        if self.settings.import_texcoords:
            for layer_idx in range(num_uv_layers):
                self._apply_uv_layer(mesh, layer_idx, all_uvs, num_loops)

        # Vertex colors
        if self.settings.import_colors:
            for layer_idx in range(num_color_layers):
                self._apply_color_layer(mesh, layer_idx, all_colors, num_loops)

        return mesh

    def _apply_normals(self, mesh, normal_data_list, num_loops: int) -> None:
        """Set custom split normals."""
        final_normals = np.zeros((num_loops, 3), dtype=np.float32)
        loop_offset = 0
        for _vert_offset, normals, indices in normal_data_list:
            for i, idx in enumerate(indices):
                final_normals[loop_offset + i] = normals[idx]
            loop_offset += len(indices)

        mesh.normals_split_custom_set(final_normals.tolist())

    def _apply_uv_layer(self, mesh, layer_idx: int, all_uvs, num_loops: int) -> None:
        layer_name = "UVMap" if layer_idx == 0 else f"UVMap.{layer_idx:03d}"
        uv_layer = mesh.uv_layers.new(name=layer_name)

        loop_uvs = np.zeros((num_loops, 2), dtype=np.float32)
        loop_offset = 0
        for uv_layer_idx, _vert_offset, uvs, indices in all_uvs:
            if uv_layer_idx != layer_idx:
                loop_offset_skip = len(indices) if uv_layer_idx < layer_idx else 0
                continue
            for i, idx in enumerate(indices):
                loop_uvs[loop_offset + i] = uvs[idx]
            loop_offset += len(indices)

        uv_layer.uv.foreach_set("vector", loop_uvs.flatten())

    def _apply_color_layer(self, mesh, layer_idx: int, all_colors, num_loops: int) -> None:
        color_attr = mesh.color_attributes.new(
            name="Color" if layer_idx == 0 else f"Color.{layer_idx:03d}",
            type="FLOAT_COLOR",
            domain="CORNER",
        )

        loop_colors = np.ones((num_loops, 4), dtype=np.float32)
        loop_offset = 0
        for color_layer_idx, _vert_offset, colors, indices in all_colors:
            if color_layer_idx != layer_idx:
                continue
            # Handle VEC3 colors (no alpha) by padding with 1.0
            num_components = colors.shape[1] if colors.ndim > 1 else 1
            for i, idx in enumerate(indices):
                if num_components >= 4:
                    loop_colors[loop_offset + i] = colors[idx]
                elif num_components == 3:
                    loop_colors[loop_offset + i, :3] = colors[idx]
            loop_offset += len(indices)

        color_attr.data.foreach_set("color", loop_colors.flatten())

    def apply_morph_targets(
        self,
        obj: "bpy.types.Object",
        mesh_index: int,
        gltf_mesh: "GltfMesh",
    ) -> None:
        """Apply morph targets to an object. Called after object creation."""
        first_prim = gltf_mesh.primitives[0]
        if not first_prim.targets:
            return

        num_targets = len(first_prim.targets)
        mesh = obj.data
        num_mesh_verts = len(mesh.vertices)

        # Create basis shape key
        obj.shape_key_add(name="Basis", from_mix=False)

        # Get basis positions
        basis_co = np.empty(num_mesh_verts * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", basis_co)

        for t_idx in range(num_targets):
            key = obj.shape_key_add(name=f"Key_{t_idx}", from_mix=False)

            target_co = basis_co.copy()
            vert_offset = 0

            for prim in gltf_mesh.primitives:
                if not prim.targets or t_idx >= len(prim.targets):
                    if "POSITION" in prim.attributes:
                        acc = self.gltf.accessors[prim.attributes["POSITION"]]
                        vert_offset += acc.count
                    continue

                target = prim.targets[t_idx]
                if "POSITION" in target:
                    deltas = self.buffer_reader.read_accessor(target["POSITION"])
                    deltas = convert_positions(deltas)
                    n = len(deltas)
                    # Add deltas to basis positions
                    target_co_3d = target_co.reshape(-1, 3)
                    target_co_3d[vert_offset : vert_offset + n] += deltas

                if "POSITION" in prim.attributes:
                    acc = self.gltf.accessors[prim.attributes["POSITION"]]
                    vert_offset += acc.count

            key.data.foreach_set("co", target_co)

        # Set default weights
        if gltf_mesh.weights:
            for i, w in enumerate(gltf_mesh.weights):
                if i + 1 < len(mesh.shape_keys.key_blocks):
                    mesh.shape_keys.key_blocks[i + 1].value = w
