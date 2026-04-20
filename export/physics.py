from __future__ import annotations

from typing import TYPE_CHECKING

from .converter import convert_location, convert_scale

if TYPE_CHECKING:
    import bpy
    from ..gltf.types import Node
    from ..exporter import ExportSettings


EXT_IMPLICIT_SHAPES = "KHR_implicit_shapes"
EXT_RIGID_BODIES = "KHR_physics_rigid_bodies"

# Axis indices for Y-up coordinate system
_AX_X, _AX_Y, _AX_Z = 0, 2, 1


class PhysicsExporter:
    def __init__(self, settings: "ExportSettings") -> None:
        self.settings = settings
        self.shapes: list[dict] = []
        self.physics_materials: list[dict] = []
        self.collision_filters: list[dict] = []
        self.physics_joints: list[dict] = []
        self._shape_cache: dict[tuple, int] = {}
        self._material_cache: dict[tuple, int] = {}
        self._filter_cache: dict[tuple, int] = {}
        self.extensions_used: set[str] = set()
        self._constraint_objects: list["bpy.types.Object"] = []

    def gather_node(
        self, obj: "bpy.types.Object", mesh_index: int | None = None,
    ) -> dict | None:
        """Build physics extension data for a node. Returns dict to merge into node.extensions."""
        rb = obj.rigid_body
        if rb is None:
            # Track constraint objects for deferred joint export
            if obj.rigid_body_constraint is not None:
                self._constraint_objects.append(obj)
            return None

        # Skip compound parent shapes (children carry their own colliders)
        if rb.collision_shape == "COMPOUND":
            # Still export motion if active
            ext: dict = {}
            if rb.type == "ACTIVE" and rb.enabled:
                ext["motion"] = self._gather_motion(obj, rb)
            if obj.rigid_body_constraint is not None:
                self._constraint_objects.append(obj)
            if not ext:
                return None
            self.extensions_used.add(EXT_RIGID_BODIES)
            return {EXT_RIGID_BODIES: ext}

        is_compound_child = self._get_compound_parent(obj) is not None

        ext = {}

        # Collider geometry
        geometry = self._gather_geometry(obj, rb, mesh_index)
        if geometry is not None:
            collider: dict = {"geometry": geometry}
            mat_idx = self._gather_physics_material(rb)
            if mat_idx is not None:
                collider["physicsMaterial"] = mat_idx
            filter_idx = self._gather_collision_filter(rb)
            if filter_idx is not None:
                collider["collisionFilter"] = filter_idx
            ext["collider"] = collider

        # Motion (dynamic rigid body) — only on standalone bodies, not compound children
        if rb.type == "ACTIVE" and rb.enabled and not is_compound_child:
            ext["motion"] = self._gather_motion(obj, rb)

        if obj.rigid_body_constraint is not None:
            self._constraint_objects.append(obj)

        if not ext:
            return None

        self.extensions_used.add(EXT_RIGID_BODIES)
        return {EXT_RIGID_BODIES: ext}

    def gather_joints(
        self,
        object_to_node_index: dict[str, int],
        nodes: list["Node"],
    ) -> None:
        """Post-pass: export rigid body constraints as joints."""
        for obj in self._constraint_objects:
            rbc = obj.rigid_body_constraint
            if rbc is None:
                continue

            body_a = rbc.object1
            body_b = rbc.object2
            if body_a is None or body_b is None:
                continue

            a_name = body_a.name
            b_name = body_b.name
            if a_name not in object_to_node_index or b_name not in object_to_node_index:
                continue

            a_node_idx = object_to_node_index[a_name]
            b_node_idx = object_to_node_index[b_name]

            # Compute joint pivot in body-local space
            a_world_inv = body_a.matrix_world.inverted()
            b_world_inv = body_b.matrix_world.inverted()
            joint_world = obj.matrix_world

            joint_in_a = a_world_inv @ joint_world
            joint_in_b = b_world_inv @ joint_world

            # Create pivot child node for body B
            loc_b, rot_b, _ = joint_in_b.decompose()
            pivot_b_node = _make_pivot_node(
                "jointPivotB", loc_b, rot_b,
            )
            pivot_b_idx = len(nodes)
            nodes.append(pivot_b_node)
            a_node = nodes[a_node_idx]
            b_node = nodes[b_node_idx]
            if b_node.children is None:
                b_node.children = []
            b_node.children.append(pivot_b_idx)

            # Build joint description
            joint_desc = self._gather_joint_description(rbc)
            joint_desc_idx = len(self.physics_joints)
            self.physics_joints.append(joint_desc)

            # Build joint data on pivot A node
            joint_data: dict = {
                "connectedNode": pivot_b_idx,
                "joint": joint_desc_idx,
            }
            if not rbc.disable_collisions:
                joint_data["enableCollision"] = True

            # Create pivot child node for body A with joint extension
            loc_a, rot_a, _ = joint_in_a.decompose()
            pivot_a_node = _make_pivot_node(
                "jointPivotA", loc_a, rot_a,
            )
            pivot_a_node.extensions = {
                EXT_RIGID_BODIES: {"joint": joint_data},
            }
            pivot_a_idx = len(nodes)
            nodes.append(pivot_a_node)
            if a_node.children is None:
                a_node.children = []
            a_node.children.append(pivot_a_idx)

            self.extensions_used.add(EXT_RIGID_BODIES)

    def get_root_extensions(self) -> dict | None:
        """Assemble document-level extension dicts."""
        result: dict = {}

        if self.shapes:
            result[EXT_IMPLICIT_SHAPES] = {"shapes": self.shapes}

        rb_ext: dict = {}
        if self.physics_materials:
            rb_ext["physicsMaterials"] = self.physics_materials
        if self.collision_filters:
            rb_ext["collisionFilters"] = self.collision_filters
        if self.physics_joints:
            rb_ext["physicsJoints"] = self.physics_joints
        if rb_ext:
            result[EXT_RIGID_BODIES] = rb_ext

        return result if result else None

    # --- Private helpers ---

    @staticmethod
    def _get_compound_parent(obj: "bpy.types.Object") -> "bpy.types.Object | None":
        cur = obj.parent
        while cur:
            if cur.rigid_body and cur.rigid_body.collision_shape == "COMPOUND":
                return cur
            cur = cur.parent
        return None

    def _gather_geometry(
        self, obj: "bpy.types.Object", rb, mesh_index: int | None,
    ) -> dict | None:
        shape_type = rb.collision_shape

        if shape_type in ("CONVEX_HULL", "MESH"):
            if mesh_index is None:
                return None
            geometry: dict = {"mesh": mesh_index}
            if shape_type == "CONVEX_HULL":
                geometry["convexHull"] = True
            return geometry

        shape_idx = self._gather_implicit_shape(obj, shape_type)
        if shape_idx is None:
            return None
        return {"shape": shape_idx}

    def _gather_implicit_shape(
        self, obj: "bpy.types.Object", shape_type: str,
    ) -> int | None:
        import bpy

        # Get evaluated mesh for vertex-based dimension calculation
        depsgraph = bpy.context.evaluated_depsgraph_get()
        eval_obj = obj.evaluated_get(depsgraph)
        mesh = eval_obj.to_mesh()
        if mesh is None:
            # Fallback for empties or objects without mesh data
            shape = self._compute_shape_from_dimensions(obj, shape_type)
        else:
            try:
                shape = self._compute_shape(mesh, shape_type)
            finally:
                eval_obj.to_mesh_clear()

        if shape is None:
            return None

        # Dedup via cache
        cache_key = _shape_cache_key(shape)
        if cache_key in self._shape_cache:
            return self._shape_cache[cache_key]

        idx = len(self.shapes)
        self.shapes.append(shape)
        self._shape_cache[cache_key] = idx
        self.extensions_used.add(EXT_IMPLICIT_SHAPES)
        return idx

    def _compute_shape(self, mesh, shape_type: str) -> dict | None:
        verts = mesh.vertices
        if len(verts) == 0:
            return None

        if shape_type == "SPHERE":
            max_r_sq = 0.0
            for v in verts:
                max_r_sq = max(max_r_sq, v.co.length_squared)
            radius = max_r_sq ** 0.5
            if radius <= 0:
                return None
            return {
                "type": "sphere",
                "sphere": {"radius": round(radius, 6)},
            }

        elif shape_type == "BOX":
            max_half = [0.0, 0.0, 0.0]
            for v in verts:
                for i in range(3):
                    max_half[i] = max(max_half[i], abs(v.co[i]))
            size = convert_scale([max_half[0] * 2, max_half[1] * 2, max_half[2] * 2])
            if any(s <= 0 for s in size):
                return None
            return {
                "type": "box",
                "box": {"size": [round(s, 6) for s in size]},
            }

        elif shape_type in ("CAPSULE", "CYLINDER", "CONE"):
            height, radius_top, radius_bottom = _compute_capsule_params(verts)
            if height <= 0:
                return None
            if shape_type == "CONE":
                radius_top = 0.0
            if shape_type == "CAPSULE":
                return {
                    "type": "capsule",
                    "capsule": {
                        "height": round(height, 6),
                        "radiusTop": round(radius_top, 6),
                        "radiusBottom": round(radius_bottom, 6),
                    },
                }
            else:
                return {
                    "type": "cylinder",
                    "cylinder": {
                        "height": round(height, 6),
                        "radiusTop": round(radius_top, 6),
                        "radiusBottom": round(radius_bottom, 6),
                    },
                }

        return None

    def _compute_shape_from_dimensions(
        self, obj: "bpy.types.Object", shape_type: str,
    ) -> dict | None:
        """Fallback shape computation for empties using display size or dimensions."""
        # Use empty_display_size as a uniform half-extent, or dimensions if available
        if hasattr(obj, "empty_display_size"):
            half = obj.empty_display_size
        else:
            half = 0.5

        if shape_type == "SPHERE":
            return {"type": "sphere", "sphere": {"radius": round(half, 6)}}
        elif shape_type == "BOX":
            size = convert_scale([half * 2, half * 2, half * 2])
            return {"type": "box", "box": {"size": [round(s, 6) for s in size]}}
        elif shape_type == "CAPSULE":
            return {
                "type": "capsule",
                "capsule": {
                    "height": round(half * 2, 6),
                    "radiusTop": round(half * 0.5, 6),
                    "radiusBottom": round(half * 0.5, 6),
                },
            }
        elif shape_type in ("CYLINDER", "CONE"):
            r_top = 0.0 if shape_type == "CONE" else round(half * 0.5, 6)
            return {
                "type": "cylinder",
                "cylinder": {
                    "height": round(half * 2, 6),
                    "radiusTop": r_top,
                    "radiusBottom": round(half * 0.5, 6),
                },
            }
        return None

    def _gather_physics_material(self, rb) -> int | None:
        key = (round(rb.friction, 6), round(rb.restitution, 6))
        if key in self._material_cache:
            return self._material_cache[key]

        mat: dict = {
            "staticFriction": key[0],
            "dynamicFriction": key[0],
            "restitution": key[1],
        }
        idx = len(self.physics_materials)
        self.physics_materials.append(mat)
        self._material_cache[key] = idx
        return idx

    def _gather_collision_filter(self, rb) -> int | None:
        enabled = tuple(rb.collision_collections)
        if enabled in self._filter_cache:
            return self._filter_cache[enabled]

        systems = [
            f"System_{i}" for i, on in enumerate(enabled) if on
        ]
        if not systems:
            return None

        filt: dict = {
            "collisionSystems": systems,
            "collideWithSystems": systems,
        }
        idx = len(self.collision_filters)
        self.collision_filters.append(filt)
        self._filter_cache[enabled] = idx
        return idx

    def _gather_motion(self, obj: "bpy.types.Object", rb) -> dict:
        motion: dict = {"mass": rb.mass}
        if rb.kinematic:
            motion["isKinematic"] = True
        return motion

    def _gather_joint_description(self, rbc) -> dict:
        """Convert Blender rigid body constraint to glTF 6DOF joint description."""
        desc: dict = {}
        limits: list[dict] = []

        X, Y, Z = _AX_X, _AX_Y, _AX_Z

        ctype = rbc.type

        if ctype == "FIXED":
            limits.append({"linearAxes": [X, Y, Z], "min": 0, "max": 0})
            limits.append({"angularAxes": [X, Y, Z], "min": 0, "max": 0})

        elif ctype == "POINT":
            limits.append({"linearAxes": [X, Y, Z], "min": 0, "max": 0})

        elif ctype == "HINGE":
            limits.append({"linearAxes": [X, Y, Z], "min": 0, "max": 0})
            limits.append({"angularAxes": [X, Y], "min": 0, "max": 0})
            if rbc.use_limit_ang_z:
                limits.append({
                    "angularAxes": [Z],
                    "min": rbc.limit_ang_z_lower,
                    "max": rbc.limit_ang_z_upper,
                })

        elif ctype == "SLIDER":
            limits.append({"angularAxes": [X, Y, Z], "min": 0, "max": 0})
            limits.append({"linearAxes": [Y, Z], "min": 0, "max": 0})
            if rbc.use_limit_lin_x:
                limits.append({
                    "linearAxes": [X],
                    "min": rbc.limit_lin_x_lower,
                    "max": rbc.limit_lin_x_upper,
                })

        elif ctype == "PISTON":
            limits.append({"angularAxes": [Y, Z], "min": 0, "max": 0})
            limits.append({"linearAxes": [Y, Z], "min": 0, "max": 0})
            if rbc.use_limit_lin_x:
                limits.append({
                    "linearAxes": [X],
                    "min": rbc.limit_lin_x_lower,
                    "max": rbc.limit_lin_x_upper,
                })
            if rbc.use_limit_ang_x:
                limits.append({
                    "angularAxes": [X],
                    "min": rbc.limit_ang_x_lower,
                    "max": rbc.limit_ang_x_upper,
                })

        elif ctype in ("GENERIC", "GENERIC_SPRING"):
            axis_map = {
                "x": X, "y": Y, "z": Z,
            }
            # Linear limits
            for bl_axis, gl_axis in axis_map.items():
                use_attr = f"use_limit_lin_{bl_axis}"
                if getattr(rbc, use_attr, False):
                    lo = getattr(rbc, f"limit_lin_{bl_axis}_lower")
                    hi = getattr(rbc, f"limit_lin_{bl_axis}_upper")
                    # Negate Y-axis values (Blender Y → glTF -Z)
                    if bl_axis == "y":
                        lo, hi = -hi, -lo
                    lim: dict = {"linearAxes": [gl_axis], "min": lo, "max": hi}
                    if ctype == "GENERIC_SPRING" and getattr(rbc, f"use_spring_{bl_axis}", False):
                        lim["stiffness"] = getattr(rbc, f"spring_stiffness_{bl_axis}")
                        lim["damping"] = getattr(rbc, f"spring_damping_{bl_axis}")
                    limits.append(lim)
            # Angular limits
            for bl_axis, gl_axis in axis_map.items():
                use_attr = f"use_limit_ang_{bl_axis}"
                if getattr(rbc, use_attr, False):
                    lo = getattr(rbc, f"limit_ang_{bl_axis}_lower")
                    hi = getattr(rbc, f"limit_ang_{bl_axis}_upper")
                    if bl_axis == "y":
                        lo, hi = -hi, -lo
                    lim = {"angularAxes": [gl_axis], "min": lo, "max": hi}
                    if ctype == "GENERIC_SPRING" and getattr(rbc, f"use_spring_ang_{bl_axis}", False):
                        lim["stiffness"] = getattr(rbc, f"spring_stiffness_ang_{bl_axis}")
                        lim["damping"] = getattr(rbc, f"spring_damping_ang_{bl_axis}")
                    limits.append(lim)

        if limits:
            desc["limits"] = limits
        return desc


def _compute_capsule_params(verts) -> tuple[float, float, float]:
    """Compute height, radiusTop, radiusBottom from mesh vertices.

    Assumes the shape is aligned along the local Z axis.
    """
    if len(verts) == 0:
        return (0.0, 0.0, 0.0)

    z_min = float("inf")
    z_max = float("-inf")
    for v in verts:
        z_min = min(z_min, v.co.z)
        z_max = max(z_max, v.co.z)

    height = z_max - z_min
    if height <= 0:
        return (0.0, 0.0, 0.0)

    z_mid = (z_min + z_max) / 2.0
    radius_top = 0.0
    radius_bottom = 0.0

    for v in verts:
        r = (v.co.x ** 2 + v.co.y ** 2) ** 0.5
        if v.co.z >= z_mid:
            radius_top = max(radius_top, r)
        else:
            radius_bottom = max(radius_bottom, r)

    return (height, radius_top, radius_bottom)


def _shape_cache_key(shape: dict) -> tuple:
    """Create a hashable key from a shape dict for deduplication."""
    shape_type = shape["type"]
    if shape_type == "sphere":
        return ("sphere", shape["sphere"]["radius"])
    elif shape_type == "box":
        return ("box", tuple(shape["box"]["size"]))
    elif shape_type == "capsule":
        c = shape["capsule"]
        return ("capsule", c["height"], c["radiusTop"], c["radiusBottom"])
    elif shape_type == "cylinder":
        c = shape["cylinder"]
        return ("cylinder", c["height"], c["radiusTop"], c["radiusBottom"])
    return (shape_type,)


def _make_pivot_node(name: str, loc, rot) -> "Node":
    """Create a Node for a joint pivot point."""
    from ..gltf.types import Node
    from .converter import convert_rotation

    translation = convert_location((loc.x, loc.y, loc.z))
    rotation_gltf = convert_rotation((rot.w, rot.x, rot.y, rot.z))

    return Node(
        name=name,
        translation=translation,
        rotation=rotation_gltf,
    )
