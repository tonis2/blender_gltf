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
        # Meshes that already received shape keys (they are shared datablocks,
        # so morph targets must only be applied once even when several nodes
        # reference the same mesh).
        self._morphed_meshes: set[int] = set()

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
        # glTF NORMAL/TEXCOORD/COLOR are per-vertex attributes, so we accumulate
        # them into per-vertex arrays addressed by the FINAL (offset) vertex
        # index. After mesh.validate() — which may delete degenerate or
        # duplicate faces and renumber loops — we look each value up by the
        # surviving loop's vertex_index. That keeps every layer aligned no
        # matter how many faces validate drops, instead of relying on fragile
        # pre-validate loop offsets ("Number of custom normals is not number
        # of loops", scrambled UVs on later primitives).
        normal_parts: list[tuple[int, int, np.ndarray]] = []
        uv_parts: list[tuple[int, int, int, np.ndarray]] = []
        color_parts: list[tuple[int, int, int, np.ndarray]] = []
        vertex_offset = 0
        num_uv_layers = 0
        num_color_layers = 0
        normals_covered = 0  # vertices that received an explicit NORMAL

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
                normal_parts.append((vertex_offset, num_verts, normals))
                normals_covered += num_verts

            # UVs
            uv_idx = 0
            while f"TEXCOORD_{uv_idx}" in prim.attributes:
                if self.settings.import_texcoords:
                    uvs = self.buffer_reader.read_accessor(prim.attributes[f"TEXCOORD_{uv_idx}"])
                    uvs = flip_uv_v(uvs)
                    uv_parts.append((uv_idx, vertex_offset, num_verts, uvs))
                uv_idx += 1
            num_uv_layers = max(num_uv_layers, uv_idx)

            # Vertex colors
            color_idx = 0
            while f"COLOR_{color_idx}" in prim.attributes:
                if self.settings.import_colors:
                    colors = self.buffer_reader.read_accessor(prim.attributes[f"COLOR_{color_idx}"])
                    color_parts.append((color_idx, vertex_offset, num_verts, colors))
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
        loop_vertex_indices = np.concatenate(all_loop_verts).astype(np.int32)
        total_verts = len(verts)
        num_loops = len(loop_vertex_indices)
        num_polys = num_loops // 3

        mesh.vertices.add(total_verts)
        mesh.vertices.foreach_set("co", verts.flatten())

        mesh.loops.add(num_loops)
        mesh.loops.foreach_set("vertex_index", loop_vertex_indices)

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
                mat = bpy.data.materials.new(f"{name}_mat_{prim_idx}")
            mesh.materials.append(mat)

        if all_mat_indices:
            mesh.polygons.foreach_set(
                "material_index", np.array(all_mat_indices, dtype=np.int32),
            )

        mesh.update()
        mesh.validate()

        # validate() may have dropped faces and renumbered loops. Vertices are
        # left intact, so read each surviving loop's vertex_index and use it to
        # gather the per-vertex attributes — alignment is guaranteed regardless
        # of how many loops were removed.
        loop_vidx = np.empty(len(mesh.loops), dtype=np.int32)
        mesh.loops.foreach_get("vertex_index", loop_vidx)

        # Custom normals (only when every contributing vertex carried a NORMAL;
        # otherwise leave Blender's computed normals rather than zeroing some).
        if normal_parts and normals_covered == total_verts:
            vert_normals = np.zeros((total_verts, 3), dtype=np.float32)
            for off, nv, normals in normal_parts:
                vert_normals[off : off + nv] = normals
            mesh.normals_split_custom_set(vert_normals[loop_vidx].tolist())

        # UV layers
        if self.settings.import_texcoords:
            for layer_idx in range(num_uv_layers):
                self._apply_uv_layer(mesh, layer_idx, uv_parts, total_verts, loop_vidx)

        # Vertex colors
        if self.settings.import_colors:
            for layer_idx in range(num_color_layers):
                self._apply_color_layer(mesh, layer_idx, color_parts, total_verts, loop_vidx)

        return mesh

    def _apply_uv_layer(self, mesh, layer_idx, uv_parts, total_verts, loop_vidx) -> None:
        layer_name = "UVMap" if layer_idx == 0 else f"UVMap.{layer_idx:03d}"
        uv_layer = mesh.uv_layers.new(name=layer_name)

        vert_uvs = np.zeros((total_verts, 2), dtype=np.float32)
        for uv_layer_idx, off, nv, uvs in uv_parts:
            if uv_layer_idx != layer_idx:
                continue
            vert_uvs[off : off + nv] = uvs

        uv_layer.uv.foreach_set("vector", vert_uvs[loop_vidx].flatten())

    def _apply_color_layer(self, mesh, layer_idx, color_parts, total_verts, loop_vidx) -> None:
        # Named after the glTF attribute it came from, not "Color": a
        # CUSTOM_materials_layers mask addresses its vertex colours by name,
        # the exporter writes that name as a COLOR_n slot, and the material
        # importer wires the Color Attribute node to the same string. Call the
        # attribute anything else and every one of those nodes points at a
        # layer the mesh does not have — which Blender renders as flat red.
        color_attr = mesh.color_attributes.new(
            name=f"COLOR_{layer_idx}",
            type="FLOAT_COLOR",
            domain="CORNER",
        )

        vert_colors = np.ones((total_verts, 4), dtype=np.float32)
        for color_layer_idx, off, nv, colors in color_parts:
            if color_layer_idx != layer_idx:
                continue
            # Handle VEC3 colors (no alpha) by padding alpha with 1.0
            num_components = colors.shape[1] if colors.ndim > 1 else 1
            if num_components >= 4:
                vert_colors[off : off + nv] = colors[:, :4]
            elif num_components == 3:
                vert_colors[off : off + nv, :3] = colors

        color_attr.data.foreach_set("color", vert_colors[loop_vidx].flatten())

    def apply_morph_targets(
        self,
        obj: "bpy.types.Object",
        mesh_index: int,
        gltf_mesh: "GltfMesh",
    ) -> None:
        """Apply morph targets to an object. Called after object creation.

        Shape keys live on the shared mesh datablock, so when several nodes
        reference the same mesh this must only run once.
        """
        if mesh_index in self._morphed_meshes:
            return

        first_prim = gltf_mesh.primitives[0]
        if not first_prim.targets:
            return
        self._morphed_meshes.add(mesh_index)

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
