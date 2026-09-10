"""Bake compact standing-room grids from exported physics collider sources."""

from __future__ import annotations

import hashlib
import math
import struct
from collections import deque
from typing import TYPE_CHECKING

import numpy as np
import bmesh
from mathutils import Vector

from ..gltf.constants import ComponentType, DataType

if TYPE_CHECKING:
    import bpy
    from ..exporter import ExportSettings
    from ..gltf.buffer import BufferBuilder


EXT_NAVIGATION = "CUSTOM_physics_navigation"
MAX_VOXELS = 16_000_000
NAV_PAD = 2


def _linked(cells, dims, step, cell, voxel, dx, dy, dz):
    """Return whether a grid move has the same legal cardinal route as runtime."""
    nx, ny, nz = dims
    if abs(dy * cell) > step + 1e-5 or (dx == 0 and dz == 0):
        return False
    plane = nx * ny
    x = voxel % nx
    y = (voxel // nx) % ny
    z = voxel // plane
    tx, ty, tz = x + dx, y + dy, z + dz
    if not (0 <= tx < nx and 0 <= ty < ny and 0 <= tz < nz):
        return False
    if voxel + dz * plane + dy * nx + dx not in cells:
        return False
    if dx and dz:
        max_dy = min(ny - 1, max(0, int(math.floor(step / cell + 1e-5))))
        for sy in range(-max_dy, max_dy + 1):
            if not 0 <= y + sy < ny:
                continue
            x_side = voxel + sy * nx + dx
            if (_linked(cells, dims, step, cell, voxel, dx, sy, 0)
                    and _linked(cells, dims, step, cell, x_side, 0, dy - sy, dz)):
                return True
            z_side = voxel + dz * plane + sy * nx
            if (_linked(cells, dims, step, cell, voxel, 0, sy, dz)
                    and _linked(cells, dims, step, cell, z_side, dx, dy - sy, 0)):
                return True
        return False
    return True


def _y_up(v: Vector) -> tuple[float, float, float]:
    return float(v.x), float(v.z), float(-v.y)


def _bounds_from_object(obj) -> tuple[list[float], list[float]]:
    if obj.type == "EMPTY":
        half = float(obj.empty_display_size)
        corners = [(x * half, y * half, z * half)
                   for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)]
    else:
        corners = obj.bound_box
    points = [_y_up(obj.matrix_world @ Vector(corner)) for corner in corners]
    return ([min(p[i] for p in points) for i in range(3)],
            [max(p[i] for p in points) for i in range(3)])


def _triangle_box_overlap(triangle, center, half) -> bool:
    """Exact SAT overlap for one triangle and an axis-aligned cell box."""
    vertices = [Vector(point) - center for point in triangle]
    edges = [vertices[1] - vertices[0], vertices[2] - vertices[1], vertices[0] - vertices[2]]
    box_axes = (Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1)))
    axes = [*box_axes, edges[0].cross(edges[1])]
    axes.extend(edge.cross(axis) for edge in edges for axis in box_axes)
    for axis in axes:
        if axis.length_squared < 1e-20:
            continue
        projected = [vertex.dot(axis) for vertex in vertices]
        radius = sum(half[i] * abs(axis[i]) for i in range(3))
        if min(projected) > radius or max(projected) < -radius:
            return False
    return True


def _revolve(profile, radial=16):
    vertices = []
    for radius, z in profile:
        for i in range(radial + 1):
            angle = 2 * math.pi * i / radial
            vertices.append(Vector((radius * math.sin(angle), radius * math.cos(angle), z)))
    triangles = []
    stride = radial + 1
    for row in range(len(profile) - 1):
        for i in range(radial):
            a, b = row * stride + i, row * stride + i + 1
            c, d = (row + 1) * stride + i, (row + 1) * stride + i + 1
            if profile[row][0] > 1e-9:
                triangles.append((vertices[b], vertices[a], vertices[d]))
            if profile[row + 1][0] > 1e-9:
                triangles.append((vertices[a], vertices[c], vertices[d]))
    return triangles


def _implicit_dimensions(mesh, shape):
    coords = [vertex.co for vertex in mesh.vertices]
    mins = [min(point[i] for point in coords) for i in range(3)]
    maxs = [max(point[i] for point in coords) for i in range(3)]
    if shape == "SPHERE":
        center = [(mins[i] + maxs[i]) * 0.5 for i in range(3)]
        radius = max((point - Vector(center)).length for point in coords)
        return round(radius, 6),
    if shape == "BOX":
        return tuple(round(max(maxs[i] - mins[i], 0.001), 6) * 0.5 for i in range(3))
    middle = (mins[2] + maxs[2]) * 0.5
    top = max((math.hypot(point.x, point.y) for point in coords if point.z >= middle), default=0)
    bottom = max((math.hypot(point.x, point.y) for point in coords if point.z < middle), default=0)
    return round(maxs[2] - mins[2], 6), round(top, 6), round(bottom, 6)


def _proxy_triangles(mesh, shape):
    if shape in {"MESH", "CONVEX_HULL"}:
        if shape == "MESH":
            return [tuple(mesh.vertices[i].co.copy() for i in tri.vertices)
                    for tri in mesh.loop_triangles]
        bm = bmesh.new()
        try:
            bm.from_mesh(mesh)
            result = bmesh.ops.convex_hull(bm, input=list(bm.verts), use_existing_faces=False)
            faces = list(result.get("geom", ()))
            faces = [item for item in faces if isinstance(item, bmesh.types.BMFace)]
            if not faces:
                return [tuple(mesh.vertices[i].co.copy() for i in tri.vertices)
                        for tri in mesh.loop_triangles]
            bmesh.ops.triangulate(bm, faces=list(bm.faces))
            return [tuple(vertex.co.copy() for vertex in face.verts)
                    for face in bm.faces if len(face.verts) == 3]
        except (RuntimeError, ValueError):
            return [tuple(mesh.vertices[i].co.copy() for i in tri.vertices)
                    for tri in mesh.loop_triangles]
        finally:
            bm.free()

    if shape == "BOX":
        hx, hy, hz = _implicit_dimensions(mesh, shape)
        local = [Vector((x * hx, y * hy, z * hz))
                 for z in (-1, 1) for y in (-1, 1) for x in (-1, 1)]
        quads = (((0, 1, 3, 2), (0, 0, -1)), ((4, 6, 7, 5), (0, 0, 1)),
                 ((0, 4, 5, 1), (0, -1, 0)), ((2, 3, 7, 6), (0, 1, 0)),
                 ((0, 2, 6, 4), (-1, 0, 0)), ((1, 5, 7, 3), (1, 0, 0)))
        triangles = []
        for (a, b, c, d), desired in quads:
            if (local[b] - local[a]).cross(local[c] - local[a]).dot(Vector(desired)) < 0:
                b, d = d, b
            triangles.extend(((local[a], local[b], local[c]), (local[a], local[c], local[d])))
        return triangles
    if shape == "SPHERE":
        (radius,) = _implicit_dimensions(mesh, shape)
        profile = [(radius * math.sin(math.pi * row / 8),
                    radius * math.cos(math.pi * row / 8)) for row in range(9)]
        return _revolve(profile)

    height, top, bottom = _implicit_dimensions(mesh, shape)
    if shape == "CAPSULE":
        rings = 4
        half = height * 0.5
        profile = []
        for row in range(rings + 1):
            theta = row / rings * math.pi * 0.5
            profile.append((top * math.sin(theta), half + top * math.cos(theta)))
        for row in range(rings + 1):
            theta = row / rings * math.pi * 0.5
            profile.append((bottom * math.cos(theta), -half - bottom * math.sin(theta)))
        return _revolve(profile)
    if shape == "CONE":
        top = 0.0
    # The runtime cylinder has closed end caps; pole rows make those triangles.
    return _revolve([(0, height * 0.5), (top, height * 0.5),
                     (bottom, -height * 0.5), (0, -height * 0.5)])


class NavigationExporter:
    def __init__(self, buffer: "BufferBuilder", settings: "ExportSettings") -> None:
        self.buffer = buffer
        self.settings = settings
        self.extensions_used: set[str] = set()

    def gather(self, context: "bpy.types.Context", exported_nodes: dict[str, int]) -> dict:
        if self.settings.export_all_scenes:
            raise ValueError(
                "Navigation export does not support All Scenes: one root grid cannot "
                "describe multiple scene coordinate spaces. Export each scene separately"
            )
        if self.settings.export_external_assets:
            raise ValueError(
                "Navigation export does not support External Assets: collection-scoped "
                "collision cannot be represented by one root grid"
            )
        if self.settings.collection_scope is not None:
            raise ValueError("Navigation export does not support collection-scoped sub-assets")
        cell = float(self.settings.navigation_cell_size)
        radius = float(self.settings.navigation_agent_radius)
        height = float(self.settings.navigation_agent_height)
        step = float(self.settings.navigation_step_height)
        slope = float(self.settings.navigation_slope_limit)
        values = (cell, radius, height, step, slope)
        if (not all(math.isfinite(value) for value in values)
                or cell <= 0 or height <= 0 or radius < 0 or step < 0
                or not 0 <= slope < 90):
            raise ValueError("Navigation profile dimensions are invalid")

        objects = []
        for obj in context.scene.objects:
            rb = obj.rigid_body
            props = getattr(obj, "khr_physics", None)
            if rb is None or (props is not None and props.is_trigger):
                continue
            if rb.collision_shape == "COMPOUND":
                continue
            compound = self._compound_parent(obj)
            if rb.type != "PASSIVE" and not (compound and compound.rigid_body.type == "PASSIVE"):
                continue
            if exported_nodes.get(obj.name) is None:
                continue
            if self.settings.export_only_visible and not obj.visible_get():
                continue
            if obj.type == "MESH":
                objects.append(obj)
        if not objects:
            raise ValueError("Navigation export found no passive, non-trigger physics colliders")

        bounds_obj = context.scene.objects.get(self.settings.navigation_bounds_object)
        if bounds_obj is not None:
            lo, hi = _bounds_from_object(bounds_obj)
        else:
            boxes = [_bounds_from_object(obj) for obj in objects]
            lo = [min(box[0][i] for box in boxes) for i in range(3)]
            hi = [max(box[1][i] for box in boxes) for i in range(3)]

        region_min = [value - cell for value in lo]
        region_max = [hi[0] + cell, hi[1] + height + cell, hi[2] + cell]
        span = [region_max[i] - region_min[i] for i in range(3)]
        dims = [int(math.ceil(value / cell)) + 2 * NAV_PAD + 1 for value in span]
        grid_center_origin = [value - NAV_PAD * cell for value in region_min]
        format_origin = [value - 0.5 * cell for value in grid_center_origin]
        total = dims[0] * dims[1] * dims[2]
        if total > MAX_VOXELS:
            source = f"object '{bounds_obj.name}'" if bounds_obj else "physics collider bounds"
            raise ValueError(
                f"Navigation {source} produces {total:,} voxels; limit is {MAX_VOXELS:,}. "
                f"Add or tighten '{self.settings.navigation_bounds_object}', or increase Cell Size"
            )

        triangles, source_records = self._triangles(context, objects)
        cells, solid_count, floor_count = self._voxelize(
            triangles, grid_center_origin, dims, cell, radius, height, slope,
        )
        if not cells:
            raise ValueError("Navigation bake produced no standing-room cells")
        components, largest = self._components(cells, dims, step, cell)

        ordered = np.asarray(sorted(cells), dtype=np.uint32)
        accessor = self.buffer.add_accessor(
            ordered, ComponentType.UNSIGNED_INT, DataType.SCALAR,
        )
        digest = hashlib.sha256()
        digest.update(b"CUSTOM_physics_navigation\0version=1\0")
        digest.update(struct.pack("<3I", *dims))
        digest.update(struct.pack("<6f", *region_min, *region_max))
        for tri in triangles:
            for point in tri:
                digest.update(struct.pack("<3f", *point))
        for record in source_records:
            digest.update(record)
        digest.update(struct.pack("<8f", cell, radius, height, step, slope, *format_origin))
        digest.update((self.settings.navigation_profile_name or "player").encode("utf8") + b"\0")

        profile = {
            "name": self.settings.navigation_profile_name or "player",
            "cellSize": cell,
            "agentRadius": radius,
            "agentHeight": height,
            "stepHeight": step,
            "slopeLimit": slope,
            "origin": format_origin,
            "dimensions": dims,
            "cells": accessor,
            "components": components,
            "largestComponent": largest,
            "solidCells": solid_count,
            "floorCells": floor_count,
            "sourceHash": digest.hexdigest(),
        }
        self.extensions_used.add(EXT_NAVIGATION)
        return {EXT_NAVIGATION: {"version": 1, "profiles": [profile]}}

    @staticmethod
    def _compound_parent(obj):
        parent = obj.parent
        while parent is not None:
            rb = parent.rigid_body
            if rb is not None and rb.collision_shape == "COMPOUND":
                return parent
            parent = parent.parent
        return None

    @staticmethod
    def _triangles(context, objects):
        depsgraph = context.evaluated_depsgraph_get()
        result = []
        records = []
        for obj in sorted(objects, key=lambda item: item.name_full):
            evaluated = obj.evaluated_get(depsgraph)
            mesh = evaluated.to_mesh()
            if mesh is None:
                continue
            try:
                mesh.calc_loop_triangles()
                world = evaluated.matrix_world
                shape = obj.rigid_body.collision_shape
                identity = f"{obj.name_full}\0{shape}\0".encode("utf8")
                transform = b"".join(struct.pack("<f", float(value))
                                     for row in world for value in row)
                records.append(identity + transform)
                try:
                    raw = _proxy_triangles(mesh, shape)
                except Exception as error:
                    raise ValueError(
                        f"Navigation collider '{obj.name_full}' ({shape}) could not be built: {error}"
                    ) from error
                for source in raw:
                    points = tuple(_y_up(world @ point) for point in source)
                    # Preserve winding, but rotate the starting vertex to make
                    # loop/mesh iteration order irrelevant.
                    rotations = [points[i:] + points[:i] for i in range(3)]
                    result.append(min(rotations))
            finally:
                evaluated.to_mesh_clear()
        result.sort()
        records.sort()
        return result, records

    @staticmethod
    def _voxelize(triangles, origin, dims, cell, radius, height, slope):
        nx, ny, nz = dims
        solid: set[int] = set()
        floor: set[int] = set()
        cos_limit = math.cos(math.radians(slope))

        for tri in triangles:
            a, b, c = tri
            ab = Vector(b) - Vector(a)
            ac = Vector(c) - Vector(a)
            normal = ab.cross(ac)
            length = normal.length
            upward = length > 1e-10 and normal.y / length >= cos_limit
            mins = [min(p[i] for p in tri) for i in range(3)]
            maxs = [max(p[i] for p in tri) for i in range(3)]
            ranges = []
            for axis, count in enumerate(dims):
                first = max(0, int(math.floor((mins[axis] - origin[axis]) / cell + 0.5)) - 1)
                last = min(count - 1, int(math.floor((maxs[axis] - origin[axis]) / cell + 0.5)) + 1)
                ranges.append(range(first, last + 1))
            for z in ranges[2]:
                for y in ranges[1]:
                    base = (z * ny + y) * nx
                    for x in ranges[0]:
                        center = Vector((
                            origin[0] + x * cell,
                            origin[1] + y * cell,
                            origin[2] + z * cell,
                        ))
                        if _triangle_box_overlap(tri, center, Vector((cell, cell, cell)) * 0.5):
                            at = base + x
                            solid.add(at)
                            if upward:
                                floor.add(at)

        grow = int(math.ceil(max(radius, 0.0) / cell - 0.5))
        blocked = set(solid)
        if grow > 0:
            for at in solid - floor:
                x = at % nx
                yz = at // nx
                y = yz % ny
                z = yz // ny
                for cz in range(max(z - grow, 0), min(z + grow, nz - 1) + 1):
                    for cx in range(max(x - grow, 0), min(x + grow, nx - 1) + 1):
                        blocked.add((cz * ny + y) * nx + cx)

        walkable: set[int] = set()
        head = int(math.ceil(height / cell))
        for z in range(NAV_PAD, nz - NAV_PAD):
            for y in range(NAV_PAD, ny - NAV_PAD):
                for x in range(NAV_PAD, nx - NAV_PAD):
                    at = (z * ny + y) * nx + x
                    if at in blocked or at - nx not in floor:
                        continue
                    clear = True
                    for up in range(1, head + 1):
                        if y + up >= ny - NAV_PAD or at + up * nx in blocked:
                            clear = False
                            break
                    if clear:
                        walkable.add(at)
        return walkable, len(solid), len(floor)

    @staticmethod
    def _components(cells, dims, step, cell):
        nx, ny, _ = dims
        left = set(cells)
        count = largest = 0
        max_dy = min(ny - 1, max(0, int(math.floor(step / cell + 1e-5))))

        def index(x, y, z):
            return (z * ny + y) * nx + x

        while left:
            count += 1
            start = left.pop()
            queue = deque([start])
            size = 0
            while queue:
                at = queue.popleft()
                size += 1
                x = at % nx
                yz = at // nx
                y = yz % ny
                z = yz // ny
                for dz in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        if dx == dz == 0:
                            continue
                        if not (0 <= x + dx < nx and 0 <= z + dz < dims[2]):
                            continue
                        for dy in range(-max_dy, max_dy + 1):
                            if not 0 <= y + dy < ny:
                                continue
                            candidate = index(x + dx, y + dy, z + dz)
                            if candidate not in left:
                                continue
                            if not _linked(cells, dims, step, cell, at, dx, dy, dz):
                                continue
                            left.remove(candidate)
                            queue.append(candidate)
            largest = max(largest, size)
        return count, largest
