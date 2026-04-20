from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .converter import convert_location, convert_rotation, convert_scale

if TYPE_CHECKING:
    import bpy
    from ..gltf.types import Gltf, Node
    from ..importer import ImportSettings


EXT_IMPLICIT_SHAPES = "KHR_implicit_shapes"
EXT_RIGID_BODIES = "KHR_physics_rigid_bodies"


class PhysicsImporter:
    def __init__(self, gltf: "Gltf", settings: "ImportSettings") -> None:
        self.gltf = gltf
        self.settings = settings
        self.shapes: list[dict] = []
        self.physics_materials: list[dict] = []
        self.collision_filters: list[dict] = []
        self.physics_joints: list[dict] = []
        self._joint_nodes: list[tuple[int, dict, dict]] = []  # (node_index, joint_data, node_ext)
        self._load_root_extensions()

    def _load_root_extensions(self) -> None:
        if self.gltf.extensions is None:
            return
        shapes_ext = self.gltf.extensions.get(EXT_IMPLICIT_SHAPES, {})
        self.shapes = shapes_ext.get("shapes", [])
        rb_ext = self.gltf.extensions.get(EXT_RIGID_BODIES, {})
        self.physics_materials = rb_ext.get("physicsMaterials", [])
        self.collision_filters = rb_ext.get("collisionFilters", [])
        self.physics_joints = rb_ext.get("physicsJoints", [])

    def has_physics(self) -> bool:
        """Check if the glTF file has any physics extensions."""
        if self.gltf.extensions is None:
            return False
        return (
            EXT_IMPLICIT_SHAPES in self.gltf.extensions
            or EXT_RIGID_BODIES in self.gltf.extensions
        )

    def import_node(
        self,
        context: "bpy.types.Context",
        obj: "bpy.types.Object",
        node: "Node",
        node_index: int,
    ) -> None:
        """Apply physics to a Blender object based on glTF node extensions."""
        if node.extensions is None:
            return

        rb_ext = node.extensions.get(EXT_RIGID_BODIES)
        if rb_ext is None:
            return

        collider = rb_ext.get("collider")
        motion = rb_ext.get("motion")
        trigger = rb_ext.get("trigger")
        joint = rb_ext.get("joint")

        if joint is not None:
            self._joint_nodes.append((node_index, joint, rb_ext))

        if collider or motion or trigger:
            self._create_rigid_body(context, obj, collider, motion, trigger)

    def fixup_joints(
        self,
        context: "bpy.types.Context",
        node_to_blender: "dict[int, bpy.types.Object]",
    ) -> None:
        """Post-pass: create rigid body constraints for joint nodes."""
        import bpy

        for node_index, joint_data, node_ext in self._joint_nodes:
            connected_node_idx = joint_data.get("connectedNode")
            joint_desc_idx = joint_data.get("joint")

            if connected_node_idx is None or joint_desc_idx is None:
                continue
            if joint_desc_idx >= len(self.physics_joints):
                continue

            joint_desc = self.physics_joints[joint_desc_idx]

            # Find the two bodies: the joint node's parent body (A) and connected body (B)
            # The joint node itself is a child of body A, connected_node is child of body B
            body_a_obj = self._find_parent_body(node_index, node_to_blender)
            body_b_obj = self._find_parent_body(connected_node_idx, node_to_blender)

            if body_a_obj is None or body_b_obj is None:
                continue

            # Get joint world position from the joint node
            joint_obj = node_to_blender.get(node_index)
            if joint_obj is None:
                # Joint pivot node might not be a real object — use body A's position
                joint_loc = body_a_obj.location
            else:
                joint_loc = joint_obj.location

            # Create constraint empty
            empty = bpy.data.objects.new(f"Joint_{node_index}", None)
            context.scene.collection.objects.link(empty)
            empty.location = joint_loc
            empty.empty_display_type = "ARROWS"
            empty.empty_display_size = 0.1

            # Ensure rigid body world exists
            _ensure_rigid_body_world(context)

            # Add rigid body constraint
            with context.temp_override(object=empty, selected_objects=[empty]):
                bpy.ops.rigidbody.constraint_add()

            rbc = empty.rigid_body_constraint
            rbc.object1 = body_a_obj
            rbc.object2 = body_b_obj

            enable_collision = joint_data.get("enableCollision", False)
            rbc.disable_collisions = not enable_collision

            # Map joint description to Blender constraint type
            self._apply_joint_description(rbc, joint_desc)

    def _create_rigid_body(
        self,
        context: "bpy.types.Context",
        obj: "bpy.types.Object",
        collider: dict | None,
        motion: dict | None,
        trigger: dict | None,
    ) -> None:
        import bpy

        _ensure_rigid_body_world(context)

        # Add rigid body
        with context.temp_override(object=obj, selected_objects=[obj]):
            bpy.ops.rigidbody.object_add()

        rb = obj.rigid_body

        # Default to passive/static
        rb.type = "PASSIVE"
        rb.enabled = False

        # Apply collider
        geom_data = collider or (trigger if trigger else None)
        if geom_data:
            geometry = geom_data.get("geometry", {})
            self._apply_collision_shape(rb, geometry)

            # Physics material
            mat_idx = geom_data.get("physicsMaterial")
            if mat_idx is not None and mat_idx < len(self.physics_materials):
                mat = self.physics_materials[mat_idx]
                rb.friction = mat.get("staticFriction", rb.friction)
                rb.restitution = mat.get("restitution", rb.restitution)

            # Collision filter
            filter_idx = geom_data.get("collisionFilter")
            if filter_idx is not None and filter_idx < len(self.collision_filters):
                filt = self.collision_filters[filter_idx]
                self._apply_collision_filter(rb, filt)

        # Apply motion (dynamic body)
        if motion is not None:
            rb.type = "ACTIVE"
            rb.enabled = True
            mass = motion.get("mass")
            if mass is not None:
                rb.mass = mass
            if motion.get("isKinematic", False):
                rb.kinematic = True

    def _apply_collision_shape(self, rb, geometry: dict) -> None:
        """Set the rigid body collision shape from geometry data."""
        shape_idx = geometry.get("shape")
        mesh_idx = geometry.get("mesh")
        convex_hull = geometry.get("convexHull", False)

        if mesh_idx is not None:
            rb.collision_shape = "CONVEX_HULL" if convex_hull else "MESH"
            return

        if shape_idx is None or shape_idx >= len(self.shapes):
            return

        shape = self.shapes[shape_idx]
        shape_type = shape.get("type", "")

        if shape_type == "sphere":
            rb.collision_shape = "SPHERE"
        elif shape_type == "box":
            rb.collision_shape = "BOX"
        elif shape_type == "capsule":
            rb.collision_shape = "CAPSULE"
        elif shape_type == "cylinder":
            cyl = shape.get("cylinder", {})
            if cyl.get("radiusTop", 0.25) == 0:
                rb.collision_shape = "CONE"
            else:
                rb.collision_shape = "CYLINDER"

    def _apply_collision_filter(self, rb, filt: dict) -> None:
        """Map named collision systems back to Blender collision_collections."""
        systems = filt.get("collisionSystems", [])
        # Parse "System_N" names to indices
        indices = set()
        for name in systems:
            match = re.match(r"System_(\d+)", name)
            if match:
                idx = int(match.group(1))
                if 0 <= idx < 20:
                    indices.add(idx)

        if indices:
            for i in range(20):
                rb.collision_collections[i] = (i in indices)

    def _find_parent_body(
        self, node_index: int, node_to_blender: "dict[int, bpy.types.Object]",
    ) -> "bpy.types.Object | None":
        """Walk up the node tree to find the nearest object with a rigid body."""
        if self.gltf.nodes is None:
            return None

        # First check if this node itself has a body
        obj = node_to_blender.get(node_index)
        if obj is not None and obj.rigid_body is not None:
            return obj

        # Walk up through parent nodes
        for parent_idx, parent_node in enumerate(self.gltf.nodes):
            if parent_node.children and node_index in parent_node.children:
                parent_obj = node_to_blender.get(parent_idx)
                if parent_obj is not None and parent_obj.rigid_body is not None:
                    return parent_obj
                # Recurse up
                return self._find_parent_body(parent_idx, node_to_blender)

        return None

    def _apply_joint_description(self, rbc, joint_desc: dict) -> None:
        """Map glTF 6DOF joint limits to a Blender constraint type."""
        limits = joint_desc.get("limits", [])
        if not limits:
            rbc.type = "GENERIC"
            return

        # Analyze locked/free DOFs to determine best Blender constraint type
        locked_linear = set()
        locked_angular = set()
        limited_linear: dict[int, tuple[float, float]] = {}
        limited_angular: dict[int, tuple[float, float]] = {}

        for lim in limits:
            lo = lim.get("min")
            hi = lim.get("max")
            is_locked = (lo is not None and hi is not None
                         and abs(lo) < 1e-6 and abs(hi) < 1e-6)

            for ax in lim.get("linearAxes", []):
                if is_locked:
                    locked_linear.add(ax)
                elif lo is not None or hi is not None:
                    limited_linear[ax] = (
                        lo if lo is not None else float("-inf"),
                        hi if hi is not None else float("inf"),
                    )

            for ax in lim.get("angularAxes", []):
                if is_locked:
                    locked_angular.add(ax)
                elif lo is not None or hi is not None:
                    limited_angular[ax] = (
                        lo if lo is not None else float("-inf"),
                        hi if hi is not None else float("inf"),
                    )

        # Axis mapping: glTF Y-up → Blender Z-up
        # glTF axis 0=X, 1=Y, 2=Z → Blender 0=X, 2=Y, 1=Z
        GL_X, GL_Y, GL_Z = 0, 1, 2

        # Determine constraint type
        all_lin_locked = locked_linear == {GL_X, GL_Y, GL_Z}
        all_ang_locked = locked_angular == {GL_X, GL_Y, GL_Z}

        if all_lin_locked and all_ang_locked:
            rbc.type = "FIXED"
        elif all_lin_locked and not locked_angular and not limited_angular:
            rbc.type = "POINT"
        elif (all_lin_locked
              and len(locked_angular) == 2
              and len(limited_angular) <= 1):
            # Hinge: one free/limited angular axis
            rbc.type = "HINGE"
            # Find the free angular axis
            for ax, (lo, hi) in limited_angular.items():
                bl_lo, bl_hi = _convert_limit_from_gltf(ax, lo, hi)
                rbc.use_limit_ang_z = True
                rbc.limit_ang_z_lower = bl_lo
                rbc.limit_ang_z_upper = bl_hi
        elif (all_ang_locked
              and len(locked_linear) == 2
              and len(limited_linear) <= 1):
            # Slider: one free/limited linear axis
            rbc.type = "SLIDER"
            for ax, (lo, hi) in limited_linear.items():
                bl_lo, bl_hi = _convert_limit_from_gltf(ax, lo, hi)
                rbc.use_limit_lin_x = True
                rbc.limit_lin_x_lower = bl_lo
                rbc.limit_lin_x_upper = bl_hi
        else:
            # Generic
            rbc.type = "GENERIC"
            # Apply per-axis limits
            bl_axis_map = {GL_X: "x", GL_Y: "z", GL_Z: "y"}

            for ax, (lo, hi) in limited_linear.items():
                bl_ax = bl_axis_map.get(ax, "x")
                bl_lo, bl_hi = _convert_limit_from_gltf(ax, lo, hi)
                setattr(rbc, f"use_limit_lin_{bl_ax}", True)
                setattr(rbc, f"limit_lin_{bl_ax}_lower", bl_lo)
                setattr(rbc, f"limit_lin_{bl_ax}_upper", bl_hi)

            for ax, (lo, hi) in limited_angular.items():
                bl_ax = bl_axis_map.get(ax, "x")
                bl_lo, bl_hi = _convert_limit_from_gltf(ax, lo, hi)
                setattr(rbc, f"use_limit_ang_{bl_ax}", True)
                setattr(rbc, f"limit_ang_{bl_ax}_lower", bl_lo)
                setattr(rbc, f"limit_ang_{bl_ax}_upper", bl_hi)

            # Apply spring properties if present
            for lim in limits:
                stiffness = lim.get("stiffness")
                damping = lim.get("damping")
                if stiffness is not None or damping is not None:
                    for ax in lim.get("linearAxes", []):
                        bl_ax = bl_axis_map.get(ax, "x")
                        setattr(rbc, f"use_spring_{bl_ax}", True)
                        if stiffness is not None:
                            setattr(rbc, f"spring_stiffness_{bl_ax}", stiffness)
                        if damping is not None:
                            setattr(rbc, f"spring_damping_{bl_ax}", damping)
                    for ax in lim.get("angularAxes", []):
                        bl_ax = bl_axis_map.get(ax, "x")
                        setattr(rbc, f"use_spring_ang_{bl_ax}", True)
                        if stiffness is not None:
                            setattr(rbc, f"spring_stiffness_ang_{bl_ax}", stiffness)
                        if damping is not None:
                            setattr(rbc, f"spring_damping_ang_{bl_ax}", damping)


def _ensure_rigid_body_world(context: "bpy.types.Context") -> None:
    """Ensure the scene has a rigid body world."""
    import bpy
    if context.scene.rigidbody_world is None:
        bpy.ops.rigidbody.world_add()


def _convert_limit_from_gltf(gl_axis: int, lo: float, hi: float) -> tuple[float, float]:
    """Convert glTF axis limit values to Blender, handling Y-axis negation."""
    # glTF Y axis = Blender Z axis (no negation needed for Z)
    # glTF Z axis = Blender -Y axis (negate)
    GL_Z = 2
    if gl_axis == GL_Z:
        return (-hi, -lo)
    return (lo, hi)
