from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .gltf.serialize import read_glb, read_gltf, parse_glb
from .gltf.types import Gltf
from .import_.buffer_reader import BufferReader
from .import_.texture import TextureImporter
from .import_.material import MaterialImporter
from .import_.mesh import MeshImporter
from .import_.skin import SkinImporter
from .import_.scene import SceneImporter
from .import_.animation import AnimationImporter
from .import_.physics import PhysicsImporter
from .import_.particles import ParticleImporter
from .import_.interactivity import InteractivityImporter
from .import_.audio import AudioImporter
from .import_.walkability import WalkabilityImporter

if TYPE_CHECKING:
    import bpy


# Extensions this importer understands. Anything listed in a file's
# `extensionsRequired` but absent here means the file cannot be imported
# faithfully — we warn rather than silently producing wrong geometry/data.
# KHR_mesh_quantization has no dedicated module: it is handled transparently
# in BufferReader (normalized integer accessors), hence it is listed here.
SUPPORTED_EXTENSIONS = frozenset({
    "CUSTOM_animation_events",
    "CUSTOM_materials_layers",
    "CUSTOM_particle_emitter",
    "CUSTOM_walkability_mask",
    "EXT_mesh_gpu_instancing",
    "KHR_audio_emitter",
    "KHR_implicit_shapes",
    "KHR_interactivity",
    "KHR_lights_punctual",
    "KHR_materials_emissive_strength",
    "KHR_materials_unlit",
    "KHR_mesh_quantization",
    "KHR_node_visibility",
    "KHR_physics_rigid_bodies",
    "KHR_texture_transform",
    "MSFT_lod",
})


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
    import_physics: bool = True
    import_particles: bool = True
    import_interactivity: bool = True
    import_audio: bool = True
    import_walkability: bool = True
    import_uids: bool = True
    import_external_assets: bool = True


class GltfImporter:
    def __init__(self, context: "bpy.types.Context", settings: ImportSettings) -> None:
        self.context = context
        self.settings = settings

    def import_file(self) -> None:
        path = Path(self.settings.filepath)

        # 1. Read file. Detect the container by content (GLB starts with the
        # "glTF" magic) rather than trusting the extension — tools sometimes
        # write JSON glTF to a .glb name or vice-versa.
        with open(path, "rb") as fh:
            magic = fh.read(4)
        if magic == b"glTF":
            gltf_dict, binary = read_glb(path)
        else:
            gltf_dict, binary = read_gltf(path)

        # 2. Deserialize
        gltf = Gltf.from_dict(gltf_dict)

        self._run_pipeline(gltf, binary, path.parent)

    def _run_pipeline(
        self,
        gltf: "Gltf",
        binary: bytes | None,
        base_dir: Path,
        *,
        target_collection: "bpy.types.Collection | None" = None,
        external_depth: int = 0,
    ) -> dict:
        """Run the import pipeline for a deserialized glTF.

        When ``target_collection`` is given, all nodes are imported into that
        collection instead of creating Blender scenes — used when instantiating
        a glTF 2.1 [DRAFT] external/packaged sub-asset. Returns node->object map.
        """
        # 2b. Warn about required extensions we don't support — the resulting
        # import may be incomplete or visually wrong.
        required = set(gltf.extensions_required or [])
        unsupported = sorted(required - SUPPORTED_EXTENSIONS)
        if unsupported:
            print(
                "[glTF import] WARNING: file requires unsupported extension(s): "
                + ", ".join(unsupported)
                + ". Import may be incomplete or incorrect."
            )

        # 3. Buffer reader
        buffer_reader = BufferReader(gltf, binary or b"", base_dir)

        # 3b. External-asset resolver (glTF 2.1 [DRAFT] files array)
        file_resolver = None
        if gltf.files and self.settings.import_external_assets:
            file_resolver = FileResolver(gltf, buffer_reader, base_dir)

        # 4. Import textures
        texture_importer = TextureImporter(gltf, buffer_reader, self.settings, base_dir)
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

        # 7b. Prepare physics importer
        physics_importer = None
        if self.settings.import_physics:
            physics_importer = PhysicsImporter(gltf, self.settings)
            if not physics_importer.has_physics():
                physics_importer = None

        # 7c. Prepare particle importer
        particle_importer = None
        if self.settings.import_particles:
            particle_importer = ParticleImporter(gltf, self.settings)
            if not particle_importer.has_particles():
                particle_importer = None

        # 7d. Prepare interactivity importer
        interactivity_importer = None
        if self.settings.import_interactivity:
            interactivity_importer = InteractivityImporter(gltf, self.settings)
            if not interactivity_importer.has_interactivity():
                interactivity_importer = None

        # 7e. Prepare audio importer
        audio_importer = None
        if self.settings.import_audio:
            audio_importer = AudioImporter(gltf, buffer_reader, self.settings, base_dir)
            if not audio_importer.has_audio():
                audio_importer = None

        # 8. Import scene hierarchy (creates armatures for skinned meshes)
        scene_importer = SceneImporter(
            gltf, buffer_reader, mesh_importer, self.settings,
            skin_importer=skin_importer,
            physics_importer=physics_importer,
            particle_importer=particle_importer,
            interactivity_importer=interactivity_importer,
            audio_importer=audio_importer,
            file_resolver=file_resolver,
            target_collection=target_collection,
            external_depth=external_depth,
        )
        node_to_blender = scene_importer.import_scene(self.context)

        # 8a. Walkability masks (needs node mapping + loaded images)
        if self.settings.import_walkability:
            WalkabilityImporter(gltf, texture_importer, self.settings).apply(node_to_blender)

        # 8b. Physics joint fixup (needs node mapping)
        if physics_importer:
            physics_importer.fixup_joints(self.context, node_to_blender)

        # 8c. Interactivity pointer fixup (needs node + material mapping)
        if interactivity_importer:
            interactivity_importer.fixup_pointers(node_to_blender, material_importer)

        # 9. Import animations
        if self.settings.import_animations:
            bone_mapping = skin_importer.bone_node_to_armature if skin_importer else None
            anim_importer = AnimationImporter(
                gltf, buffer_reader, node_to_blender, material_importer, self.settings,
                bone_node_to_armature=bone_mapping,
            )
            anim_importer.import_all(self.context)

        return node_to_blender


# Max external-asset nesting depth — a backstop against malformed self-references.
MAX_EXTERNAL_DEPTH = 8


class FileResolver:
    """Resolves glTF 2.1 [DRAFT] ``files`` entries to (gltf_dict, binary).

    A file entry is either an external ``uri`` (relative path or data: URI) or
    an embedded GLB blob referenced by ``bufferView`` (packaging — the host
    file acts as a virtual filesystem)."""

    def __init__(self, gltf: "Gltf", buffer_reader: "BufferReader", base_dir: Path) -> None:
        self.gltf = gltf
        self.buffer_reader = buffer_reader
        self.base_dir = base_dir

    def load(self, file_index: int) -> tuple[dict, bytes | None]:
        f = self.gltf.files[file_index]
        if f.buffer_view is not None:
            blob = self.buffer_reader.read_buffer_view_bytes(f.buffer_view)
            return self._parse_blob(bytes(blob))
        if f.uri:
            if f.uri.startswith("data:"):
                import base64
                blob = base64.b64decode(f.uri.split(",", 1)[1])
                return self._parse_blob(blob)
            p = self.base_dir / f.uri
            data = p.read_bytes()
            if data[:4] == b"glTF":
                return parse_glb(data)
            return read_gltf(p)
        raise ValueError(f"File {file_index} has neither uri nor bufferView")

    @staticmethod
    def _parse_blob(blob: bytes) -> tuple[dict, bytes | None]:
        if blob[:4] == b"glTF":
            return parse_glb(blob)
        import json
        return json.loads(blob), None


def import_subasset(
    context: "bpy.types.Context",
    gltf_dict: dict,
    binary: bytes | None,
    base_dir: Path,
    target_collection: "bpy.types.Collection",
    settings: "ImportSettings",
    *,
    external_depth: int,
) -> None:
    """Import a glTF 2.1 [DRAFT] sub-asset into ``target_collection``."""
    gltf = Gltf.from_dict(gltf_dict)
    importer = GltfImporter(context, settings)
    importer._run_pipeline(
        gltf, binary, base_dir,
        target_collection=target_collection,
        external_depth=external_depth,
    )
