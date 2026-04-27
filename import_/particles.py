from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import bpy
    from ..gltf.types import Gltf, Node
    from ..importer import ImportSettings


EXT_PARTICLE_EMITTER = "CUSTOM_particle_emitter"

# glTF render type -> Blender render_type
_RENDER_TYPE_MAP = {
    "billboard": "HALO",
    "mesh": "OBJECT",
}

# glTF emitFrom -> Blender emit_from
_EMIT_FROM_MAP = {
    "vert": "VERT",
    "face": "FACE",
    "volume": "VOLUME",
}

# glTF angular velocity mode -> Blender angular_velocity_mode
_ANG_VEL_MODE_MAP = {
    "none": "NONE",
    "velocity": "VELOCITY",
    "horizontal": "HORIZONTAL",
    "vertical": "VERTICAL",
    "global_x": "GLOBAL_X",
    "global_y": "GLOBAL_Y",
    "global_z": "GLOBAL_Z",
    "rand": "RAND",
}


class ParticleImporter:
    def __init__(self, gltf: "Gltf", settings: "ImportSettings") -> None:
        self.gltf = gltf
        self.settings = settings

    def has_particles(self) -> bool:
        """Check if any node in the glTF has particle extensions."""
        if self.gltf.nodes is None:
            return False
        for node in self.gltf.nodes:
            if node.extensions and EXT_PARTICLE_EMITTER in node.extensions:
                return True
        return False

    def import_node(
        self,
        context: "bpy.types.Context",
        obj: "bpy.types.Object",
        node: "Node",
    ) -> None:
        """Create particle systems from extension data."""
        if node.extensions is None:
            return

        ext = node.extensions.get(EXT_PARTICLE_EMITTER)
        if ext is None:
            return

        emitters = ext.get("emitters", [])
        fps = context.scene.render.fps

        for emitter_data in emitters:
            self._create_particle_system(context, obj, emitter_data, fps)

    def _create_particle_system(
        self,
        context: "bpy.types.Context",
        obj: "bpy.types.Object",
        data: dict,
        fps: float,
    ) -> None:
        import bpy

        name = data.get("name", "ParticleSystem")

        # Add particle system modifier
        mod = obj.modifiers.new(name=name, type="PARTICLE_SYSTEM")
        ps = mod.particle_system
        s = ps.settings
        s.name = name

        # --- Emission ---
        emission = data.get("emission", {})
        s.count = emission.get("count", 1000)

        duration_sec = emission.get("duration", 0)
        if duration_sec > 0:
            s.frame_start = 1
            s.frame_end = 1 + duration_sec * fps
        else:
            # Continuous: emit over a long range
            s.frame_start = 1
            s.frame_end = 200

        emit_from = emission.get("emitFrom", "face")
        s.emit_from = _EMIT_FROM_MAP.get(emit_from, "FACE")

        # --- Lifetime ---
        lifetime = data.get("lifetime", {})
        lifetime_sec = lifetime.get("value", 2.0)
        s.lifetime = lifetime_sec * fps
        s.lifetime_random = lifetime.get("random", 0.0)

        # --- Velocity ---
        velocity = data.get("velocity", {})
        if "normalFactor" in velocity:
            s.normal_factor = velocity["normalFactor"]
        if "tangentFactor" in velocity:
            s.tangent_factor = velocity["tangentFactor"]
        if "randomFactor" in velocity:
            s.factor_random = velocity["randomFactor"]
        if "objectFactor" in velocity:
            s.object_factor = velocity["objectFactor"]

        # --- Size ---
        size = data.get("size", {})
        s.particle_size = size.get("value", 0.05)
        s.size_random = size.get("random", 0.0)

        # --- Physics ---
        physics = data.get("physics", {})
        if "mass" in physics:
            s.mass = physics["mass"]
        if "damping" in physics:
            s.damping = physics["damping"]
        if "gravityFactor" in physics and hasattr(s, "effector_weights"):
            s.effector_weights.gravity = physics["gravityFactor"]

        # --- Rotation ---
        rotation = data.get("rotation", {})
        if rotation:
            s.use_rotations = True
            if "angularVelocity" in rotation:
                s.angular_velocity_factor = rotation["angularVelocity"]
            mode = rotation.get("mode", "none")
            s.angular_velocity_mode = _ANG_VEL_MODE_MAP.get(mode, "NONE")
            if "randomFactor" in rotation:
                s.rotation_factor_random = rotation["randomFactor"]

        # --- Render ---
        render = data.get("render", {})
        render_type = render.get("type", "billboard")
        bl_render_type = _RENDER_TYPE_MAP.get(render_type, "HALO")

        # If render type is LINE, it's a stretched billboard
        if render.get("stretchWithVelocity", False):
            bl_render_type = "LINE"

        s.render_type = bl_render_type

        # Material slot (1-based in Blender)
        if "material" in render:
            s.material = render["material"] + 1
