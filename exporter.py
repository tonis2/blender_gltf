from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .gltf.buffer import BufferBuilder
from .gltf.serialize import write_glb, write_gltf, write_gltf_embedded
from .gltf.types import Asset, Gltf
from .export.texture import TextureExporter
from .export.material import MaterialExporter
from .export.mesh import MeshExporter
from .export.scene import SceneExporter

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


class GltfExporter:
    def __init__(self, context: "bpy.types.Context", settings: ExportSettings) -> None:
        self.context = context
        self.settings = settings
        self.buffer = BufferBuilder()
        self.texture_exporter = TextureExporter(self.buffer, settings)
        self.material_exporter = MaterialExporter(self.texture_exporter, settings)
        self.mesh_exporter = MeshExporter(self.buffer, settings)
        self.scene_exporter = SceneExporter(
            self.mesh_exporter, self.material_exporter, settings,
        )

    def export(self) -> None:
        # 1. Gather scene data
        scenes, active_scene = self.scene_exporter.gather(self.context)

        # 2. Finalize buffer
        accessors, buffer_views, buffer_desc, binary = self.buffer.finalize()

        # 3. Handle .bin URI for separate format
        if buffer_desc and self.settings.format == "GLTF_SEPARATE":
            bin_filename = Path(self.settings.filepath).stem + ".bin"
            buffer_desc.uri = bin_filename

        # 4. Collect extensions used
        extensions_used = sorted(self.scene_exporter.extensions_used) or None

        # 5. Assemble glTF
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
