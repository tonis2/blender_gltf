from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import bpy
    from ..exporter import ExportSettings
    from .mesh import MeshExporter
    from .material import MaterialExporter


EXT_PARTICLE_EMITTER = "CUSTOM_particle_emitter"

# Blender render_type -> glTF render type
_RENDER_TYPE_MAP = {
    "HALO": "billboard",
    "LINE": "billboard",
    "OBJECT": "mesh",
    "COLLECTION": "mesh",
}


class ParticleExporter:
    def __init__(
        self,
        settings: "ExportSettings",
        mesh_exporter: "MeshExporter",
        material_exporter: "MaterialExporter",
    ) -> None:
        self.settings = settings
        self.mesh_exporter = mesh_exporter
        self.material_exporter = material_exporter
        self.extensions_used: set[str] = set()

    def gather_node(
        self,
        obj: "bpy.types.Object",
        fps: float,
    ) -> dict | None:
        """Build particle extension data for a node. Returns dict to merge into node.extensions."""
        if not obj.particle_systems:
            return None

        emitters: list[dict] = []
        for ps in obj.particle_systems:
            s = ps.settings
            # Only export emitter-type particles with Newtonian or No physics
            if s.type != "EMITTER":
                continue
            if hasattr(s, "physics_type") and s.physics_type not in ("NEWTONIAN", "NO", "NEWTON"):
                continue

            emitter = self._gather_emitter(obj, ps, fps)
            if emitter is not None:
                emitters.append(emitter)

        if not emitters:
            return None

        self.extensions_used.add(EXT_PARTICLE_EMITTER)
        return {EXT_PARTICLE_EMITTER: {"emitters": emitters}}

    def _gather_emitter(
        self,
        obj: "bpy.types.Object",
        ps: "bpy.types.ParticleSystem",
        fps: float,
    ) -> dict:
        s = ps.settings
        emitter: dict = {}

        if ps.name:
            emitter["name"] = ps.name

        # --- Emission ---
        duration_frames = max(s.frame_end - s.frame_start, 1)
        duration_sec = duration_frames / fps
        rate = s.count / duration_sec if duration_sec > 0 else float(s.count)

        emission: dict = {
            "count": s.count,
            "rate": round(rate, 4),
        }
        if duration_sec > 0:
            emission["duration"] = round(duration_sec, 4)

        emit_from = getattr(s, "emit_from", "FACE")
        emission["emitFrom"] = emit_from.lower()
        emitter["emission"] = emission

        # --- Lifetime ---
        lifetime_sec = s.lifetime / fps
        lifetime: dict = {"value": round(lifetime_sec, 4)}
        if s.lifetime_random > 0:
            lifetime["random"] = round(s.lifetime_random, 4)
        emitter["lifetime"] = lifetime

        # --- Velocity ---
        velocity: dict = {}
        normal_factor = getattr(s, "normal_factor", 0.0)
        tangent_factor = getattr(s, "tangent_factor", 0.0)
        factor_random = getattr(s, "factor_random", 0.0)
        object_factor = getattr(s, "object_factor", 0.0)

        if normal_factor != 0:
            velocity["normalFactor"] = round(normal_factor, 6)
        if tangent_factor != 0:
            velocity["tangentFactor"] = round(tangent_factor, 6)
        if factor_random != 0:
            velocity["randomFactor"] = round(factor_random, 6)
        if object_factor != 0:
            velocity["objectFactor"] = round(object_factor, 6)

        if velocity:
            emitter["velocity"] = velocity

        # --- Size ---
        size: dict = {"value": round(s.particle_size, 6)}
        if s.size_random > 0:
            size["random"] = round(s.size_random, 4)
        emitter["size"] = size

        # --- Physics ---
        physics: dict = {}
        if s.mass != 1.0:
            physics["mass"] = round(s.mass, 6)
        if s.damping > 0:
            physics["damping"] = round(s.damping, 6)

        gravity = getattr(s.effector_weights, "gravity", 1.0) if hasattr(s, "effector_weights") else 1.0
        if gravity != 1.0:
            physics["gravityFactor"] = round(gravity, 6)

        if physics:
            emitter["physics"] = physics

        # --- Rotation ---
        rotation: dict = {}
        ang_vel = getattr(s, "angular_velocity_factor", 0.0)
        ang_mode = getattr(s, "angular_velocity_mode", "NONE")
        rot_random = getattr(s, "rotation_factor_random", 0.0)

        if ang_vel != 0:
            rotation["angularVelocity"] = round(ang_vel, 6)
        if ang_mode != "NONE":
            rotation["mode"] = ang_mode.lower()
        if rot_random > 0:
            rotation["randomFactor"] = round(rot_random, 4)

        if rotation:
            emitter["rotation"] = rotation

        # --- Render ---
        render = self._gather_render(obj, s)
        if render:
            emitter["render"] = render

        return emitter

    def _gather_render(
        self,
        obj: "bpy.types.Object",
        settings: "bpy.types.ParticleSettings",
    ) -> dict | None:
        render_type = getattr(settings, "render_type", "HALO")

        gltf_type = _RENDER_TYPE_MAP.get(render_type)
        if gltf_type is None:
            return None

        render: dict = {"type": gltf_type}

        # Stretched billboard for LINE type
        if render_type == "LINE":
            render["stretchWithVelocity"] = True

        # Material reference from emitter object's material slot
        mat_slot = getattr(settings, "material_slot", "")
        if mat_slot and obj.material_slots:
            # material_slot is 1-based in Blender display, but the property
            # stores the slot name. Try resolving by index.
            mat_idx_bl = getattr(settings, "material", 1) - 1
            if 0 <= mat_idx_bl < len(obj.material_slots):
                bl_mat = obj.material_slots[mat_idx_bl].material
                if bl_mat is not None:
                    gltf_mat_idx = self.material_exporter.gather(bl_mat)
                    if gltf_mat_idx is not None:
                        render["material"] = gltf_mat_idx

        # Instance mesh for OBJECT render type
        if render_type == "OBJECT" and settings.instance_object:
            inst_obj = settings.instance_object
            if inst_obj.type == "MESH":
                material_map = self._gather_instance_materials(inst_obj)
                mesh_idx = self.mesh_exporter.gather(inst_obj, material_map)
                if mesh_idx is not None:
                    render["instanceMesh"] = mesh_idx

        # Instance mesh for COLLECTION render type (first mesh in collection)
        if render_type == "COLLECTION" and settings.instance_collection:
            for coll_obj in settings.instance_collection.objects:
                if coll_obj.type == "MESH":
                    material_map = self._gather_instance_materials(coll_obj)
                    mesh_idx = self.mesh_exporter.gather(coll_obj, material_map)
                    if mesh_idx is not None:
                        render["instanceMesh"] = mesh_idx
                    break

        return render

    def _gather_instance_materials(
        self, obj: "bpy.types.Object",
    ) -> dict[int, int]:
        material_map: dict[int, int] = {}
        for i, slot in enumerate(obj.material_slots):
            if slot.material is not None:
                gltf_idx = self.material_exporter.gather(slot.material)
                if gltf_idx is not None:
                    material_map[i] = gltf_idx
        return material_map
