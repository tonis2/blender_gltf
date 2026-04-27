from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .gltf.buffer import BufferBuilder
from .gltf.serialize import write_glb, write_gltf, write_gltf_embedded
from .gltf.types import Asset, Gltf
from .export.animation import AnimationExporter
from .export.texture import TextureExporter
from .export.material import MaterialExporter
from .export.mesh import MeshExporter
from .export.scene import SceneExporter
from .export.skin import SkinExporter
from .export.physics import PhysicsExporter
from .export.particles import ParticleExporter

if TYPE_CHECKING:
    import bpy


@dataclass
class ExportSettings:
    filepath: str = ""
    format: str = "GLB"  # "GLB", "GLTF_SEPARATE", or "GLTF_EMBEDDED"
    export_normals: bool = True
    export_texcoords: bool = True
    export_materials: bool = True
    export_colors: bool = True
    export_animations: bool = True
    export_morph_targets: bool = True
    export_gpu_instancing: bool = True
    export_skinning: bool = True
    export_physics: bool = True
    export_extras: bool = True
    export_particles: bool = True
    export_only_visible: bool = False
    export_all_scenes: bool = False
    export_camera_y_up: bool = True
    image_format: str = "AUTO"  # "AUTO", "JPEG", or "PNG"


class GltfExporter:
    def __init__(self, context: "bpy.types.Context", settings: ExportSettings) -> None:
        self.context = context
        self.settings = settings
        self.buffer = BufferBuilder()
        self.texture_exporter = TextureExporter(self.buffer, settings)
        self.material_exporter = MaterialExporter(self.texture_exporter, settings)
        self.mesh_exporter = MeshExporter(self.buffer, settings)
        self.skin_exporter = SkinExporter(self.buffer, settings) if settings.export_skinning else None
        self.physics_exporter = PhysicsExporter(settings) if settings.export_physics else None
        self.particle_exporter = ParticleExporter(
            settings, self.mesh_exporter, self.material_exporter,
        ) if settings.export_particles else None
        self.scene_exporter = SceneExporter(
            self.mesh_exporter, self.material_exporter, self.buffer, settings,
            skin_exporter=self.skin_exporter,
            physics_exporter=self.physics_exporter,
            particle_exporter=self.particle_exporter,
        )

    def export(self) -> None:
        # 1. Gather scene data
        scenes, active_scene = self.scene_exporter.gather(self.context)

        # 1b. Physics joint post-pass (needs node mapping from scene pass)
        if self.physics_exporter:
            self.physics_exporter.gather_joints(
                self.scene_exporter.object_to_node_index,
                self.scene_exporter.nodes,
            )

        # 2. Gather animations (needs node mapping from scene pass)
        animations = None
        animation_exporter = None
        if self.settings.export_animations:
            animation_exporter = AnimationExporter(
                self.buffer,
                self.settings,
                self.scene_exporter.object_to_node_index,
                self.material_exporter._cache,
                bone_to_node_index=self.skin_exporter.bone_to_node_index if self.skin_exporter else None,
            )
            if self.settings.export_all_scenes:
                import bpy
                exported_scenes = list(bpy.data.scenes)
            else:
                exported_scenes = [self.context.scene]
            animation_exporter.gather(self.context, scenes=exported_scenes)
            if animation_exporter.animations:
                animations = animation_exporter.animations

        # 3. Finalize buffer
        accessors, buffer_views, buffer_desc, binary = self.buffer.finalize()

        # 4. Handle .bin URI for separate format
        if buffer_desc and self.settings.format == "GLTF_SEPARATE":
            bin_filename = Path(self.settings.filepath).stem + ".bin"
            buffer_desc.uri = bin_filename

        # 5. Collect extensions used
        all_extensions = set(self.scene_exporter.extensions_used)
        all_extensions |= self.material_exporter.extensions_used
        all_extensions |= self.texture_exporter.extensions_used
        if animation_exporter:
            all_extensions |= animation_exporter.extensions_used
        if self.physics_exporter:
            all_extensions |= self.physics_exporter.extensions_used
        if self.particle_exporter:
            all_extensions |= self.particle_exporter.extensions_used
        extensions_used = sorted(all_extensions) or None

        # 5b. Collect root-level extensions
        root_extensions = None
        if self.physics_exporter:
            root_extensions = self.physics_exporter.get_root_extensions()

        # 5c. KHR_lights_punctual root extension
        if self.scene_exporter.lights:
            if root_extensions is None:
                root_extensions = {}
            root_extensions["KHR_lights_punctual"] = {
                "lights": self.scene_exporter.lights,
            }

        # 6. Assemble glTF
        gltf = Gltf(
            asset=Asset(generator="gltf-exporter", version="2.0"),
            scene=active_scene,
            scenes=scenes,
            nodes=self.scene_exporter.nodes or None,
            meshes=self.mesh_exporter.meshes or None,
            accessors=accessors or None,
            buffer_views=buffer_views or None,
            buffers=[buffer_desc] if buffer_desc else None,
            materials=self.material_exporter.materials or None,
            textures=self.texture_exporter.textures or None,
            images=self.texture_exporter.images or None,
            samplers=self.texture_exporter.samplers or None,
            cameras=self.scene_exporter.cameras or None,
            animations=animations,
            skins=self.skin_exporter.skins if self.skin_exporter and self.skin_exporter.skins else None,
            extensions=root_extensions,
            extensions_used=extensions_used,
        )

        # 6. Serialize
        gltf_dict = gltf.to_dict()
        path = Path(self.settings.filepath)

        if self.settings.format == "GLB":
            write_glb(path, gltf_dict, binary)
        elif self.settings.format == "GLTF_EMBEDDED":
            write_gltf_embedded(path, gltf_dict, binary)
        else:
            write_gltf(path, gltf_dict, binary)
