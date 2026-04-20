from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .gltf.serialize import read_glb, read_gltf
from .gltf.types import Gltf
from .import_.buffer_reader import BufferReader
from .import_.texture import TextureImporter
from .import_.material import MaterialImporter
from .import_.mesh import MeshImporter
from .import_.skin import SkinImporter
from .import_.scene import SceneImporter
from .import_.animation import AnimationImporter

if TYPE_CHECKING:
    import bpy


@dataclass
class ImportSettings:
    filepath: str = ""
    import_normals: bool = True
    import_texcoords: bool = True
    import_materials: bool = True
    import_colors: bool = True
    import_animations: bool = True
    import_morph_targets: bool = True
    import_skinning: bool = True


class GltfImporter:
    def __init__(self, context: "bpy.types.Context", settings: ImportSettings) -> None:
        self.context = context
        self.settings = settings

    def import_file(self) -> None:
        path = Path(self.settings.filepath)

        # 1. Read file
        if path.suffix.lower() == ".glb":
            gltf_dict, binary = read_glb(path)
        else:
            gltf_dict, binary = read_gltf(path)

        # 2. Deserialize
        gltf = Gltf.from_dict(gltf_dict)

        # 3. Buffer reader
        buffer_reader = BufferReader(gltf, binary or b"", path.parent)

        # 4. Import textures
        texture_importer = TextureImporter(gltf, buffer_reader, self.settings, path.parent)
        if self.settings.import_materials:
            texture_importer.import_all()

        # 5. Import materials
        material_importer = MaterialImporter(gltf, texture_importer, self.settings)
        if self.settings.import_materials:
            material_importer.import_all()

        # 6. Import meshes
        mesh_importer = MeshImporter(gltf, buffer_reader, material_importer, self.settings)
        mesh_importer.import_all()

        # 7. Prepare skin importer (needs mesh data for vertex weights)
        skin_importer = None
        if self.settings.import_skinning and gltf.skins:
            skin_importer = SkinImporter(gltf, buffer_reader, mesh_importer, self.settings)

        # 8. Import scene hierarchy (creates armatures for skinned meshes)
        scene_importer = SceneImporter(
            gltf, buffer_reader, mesh_importer, self.settings,
            skin_importer=skin_importer,
        )
        node_to_blender = scene_importer.import_scene(self.context)

        # 9. Import animations
        if self.settings.import_animations:
            bone_mapping = skin_importer.bone_node_to_armature if skin_importer else None
            anim_importer = AnimationImporter(
                gltf, buffer_reader, node_to_blender, material_importer, self.settings,
                bone_node_to_armature=bone_mapping,
            )
            anim_importer.import_all(self.context)
