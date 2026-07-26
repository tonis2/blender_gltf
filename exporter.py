from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from dataclasses import replace

from .gltf.buffer import BufferBuilder
from .gltf.serialize import write_glb, write_gltf, write_gltf_embedded, serialize_glb
from .gltf.types import Asset, Gltf, File
from .export.animation import AnimationExporter
from .export.texture import TextureExporter
from .export.material import MaterialExporter
from .export.mesh import MeshExporter
from .export.scene import SceneExporter
from .export.skin import SkinExporter
from .export.physics import PhysicsExporter
from .export.particles import ParticleExporter
from .export.interactivity import InteractivityExporter
from .export.audio import AudioExporter
from .export.walkability import WalkabilityExporter
from .export.environment import EnvironmentExporter

if TYPE_CHECKING:
    import bpy


@dataclass
class ExportSettings:
    filepath: str = ""
    export_format: str = "GLB"  # "GLB", "GLTF_SEPARATE", or "GLTF_EMBEDDED"
    export_normals: bool = True
    export_texcoords: bool = True
    export_tangents: bool = False
    export_quantization: bool = False
    export_materials: bool = True
    export_colors: bool = True
    export_animations: bool = True
    export_animation_events: bool = True
    export_morph_targets: bool = True
    export_gpu_instancing: bool = True
    export_lods: bool = True
    export_skinning: bool = True
    export_physics: bool = True
    export_extras: bool = True
    export_particles: bool = True
    export_interactivity: bool = True
    export_audio: bool = True
    export_walkability: bool = True
    export_environment_map: bool = True
    environment_map_size: str = "AUTO"  # "AUTO", or a face size like "512"
    environment_map_codec: str = "rgba8"  # "rgba8", "bc7", "uastc", "etc1s"
    export_only_visible: bool = False
    export_all_scenes: bool = False
    export_camera_y_up: bool = True
    image_format: str = "AUTO"  # "AUTO", "JPEG", "PNG", or "KTX2"
    ktx_codec: str = "uastc"  # "uastc" or "etc1s" — color textures (KTX2 only)
    ktx_quality: int = 90  # 0-100, color textures (KTX2 only)
    ktx_normal_codec: str = "uastc"  # "uastc" or "etc1s" — normal + height maps
    ktx_normal_quality: int = 100  # 0-100, normal + height maps (KTX2 only)
    ktx_effort: int = 2  # 0-10 encoder search effort, all KTX2 encodes
    bake_materials: bool = False
    bake_resolution: str = "1024"  # "512" / "1024" / "2048" / "4096"
    force_64bit: bool = False
    export_uids: bool = True
    export_external_assets: bool = False
    external_assets_mode: str = "PACKAGED"  # "PACKAGED" or "REFERENCES"
    # Internal: set on nested sub-exports to scope gathering to one collection.
    # Not a UI/operator property.
    collection_scope: object = None


class GltfExporter:
    def __init__(
        self,
        context: "bpy.types.Context",
        settings: ExportSettings,
        external_seen: set | None = None,
        external_depth: int = 0,
    ) -> None:
        self.context = context
        self.settings = settings
        # glTF 2.1 [DRAFT] external-asset recursion guards.
        self._external_seen: set = external_seen or set()
        self._external_depth = external_depth
        self.buffer = BufferBuilder()
        self.texture_exporter = TextureExporter(self.buffer, settings)
        self.material_exporter = MaterialExporter(self.texture_exporter, settings)
        self.mesh_exporter = MeshExporter(
            self.buffer, settings, self.material_exporter,
        )
        self.skin_exporter = SkinExporter(self.buffer, settings) if settings.export_skinning else None
        self.physics_exporter = PhysicsExporter(settings) if settings.export_physics else None
        self.particle_exporter = ParticleExporter(
            settings, self.mesh_exporter, self.material_exporter,
        ) if settings.export_particles else None
        self.interactivity_exporter = InteractivityExporter(
            settings,
        ) if settings.export_interactivity else None
        self.audio_exporter = AudioExporter(self.buffer, settings) if settings.export_audio else None
        self.walkability_exporter = WalkabilityExporter(
            self.texture_exporter, settings,
        ) if settings.export_walkability else None
        self.environment_exporter = EnvironmentExporter(
            self.texture_exporter, settings,
        ) if settings.export_environment_map else None
        self.scene_exporter = SceneExporter(
            self.mesh_exporter, self.material_exporter, self.buffer, settings,
            skin_exporter=self.skin_exporter,
            physics_exporter=self.physics_exporter,
            particle_exporter=self.particle_exporter,
            interactivity_exporter=self.interactivity_exporter,
            audio_exporter=self.audio_exporter,
            walkability_exporter=self.walkability_exporter,
        )
        if self.interactivity_exporter is not None:
            self.interactivity_exporter.scene_exporter = self.scene_exporter
            self.interactivity_exporter.material_exporter = self.material_exporter
        if self.settings.export_external_assets:
            self.scene_exporter.external_asset_builder = self._build_external_file

    def _assign_uids(self) -> None:
        """Give every object-backed node a glTF 2.1 [DRAFT] ``uid``.

        Reuses an existing ``obj['gltf_uid']`` when present and unique; otherwise
        generates a stable one and writes it back so re-exports round-trip. A
        uid must not collide with any other uid or any node name in the file.
        """
        import bpy
        import uuid

        nodes = self.scene_exporter.nodes
        # Names share the uid identifier namespace per the draft spec.
        reserved = {n.name for n in nodes if n.name}
        used_uids: set[str] = set()

        for obj_name, idx in self.scene_exporter.object_to_node_index.items():
            if idx is None or idx >= len(nodes):
                continue
            node = nodes[idx]
            obj = bpy.data.objects.get(obj_name)
            if obj is None:
                continue

            existing = obj.get("gltf_uid")
            uid = existing if isinstance(existing, str) and existing else None
            if uid is not None and (uid in used_uids or uid in reserved):
                print(
                    f"glTF export: object '{obj_name}' has a colliding gltf_uid "
                    f"'{uid}'; regenerating"
                )
                uid = None

            if uid is None:
                while True:
                    cand = f"{obj.name}-{uuid.uuid4().hex[:8]}"
                    if cand not in used_uids and cand not in reserved:
                        uid = cand
                        break
                obj["gltf_uid"] = uid  # store back for stable round-trips

            node.uid = uid
            used_uids.add(uid)

    def build(self) -> tuple[dict, bytes]:
        """Gather and assemble the glTF, returning (gltf_dict, binary) without
        writing to disk. Reused to produce external sub-assets in memory.
        Blocks on background KTX2 encodes; the export operator instead calls
        build_begin(), waits modally, then build_finish()."""
        self.build_begin()
        return self.build_finish()

    def build_begin(self) -> None:
        """Everything that reads bpy data (main thread): scene, physics,
        animations. KTX2 texture encodes gathered here keep running on the
        background worker after this returns; poll
        texture_exporter.ktx_pending_done() before build_finish()."""
        # 1. Gather scene data
        scenes, active_scene = self.scene_exporter.gather(self.context)
        self._scenes = scenes
        self._active_scene = active_scene

        # 1a. KHR_environment_map. Scenes come back named after the bpy scene
        # they were gathered from, which is the only handle back to the world;
        # a collection-scoped sub-export matches nothing here and is skipped.
        if self.environment_exporter:
            import bpy
            for scene in scenes:
                bl_scene = bpy.data.scenes.get(scene.name or "")
                if bl_scene is None:
                    continue
                ext = self.environment_exporter.gather_scene(bl_scene)
                if ext:
                    merged = dict(scene.extensions or {})
                    merged.update(ext)
                    scene.extensions = merged

        # 1b. Physics joint post-pass (needs node mapping from scene pass)
        if self.physics_exporter:
            self.physics_exporter.gather_joints(
                self.scene_exporter.object_to_node_index,
                self.scene_exporter.nodes,
            )

        # 1c. Assign unique IDs (glTF 2.1 [DRAFT]) to object-backed nodes.
        if self.settings.export_uids:
            self._assign_uids()

        # 2. Gather animations (needs node mapping from scene pass)
        self._animations = None
        self._animation_exporter = None
        if self.settings.export_animations:
            animation_exporter = AnimationExporter(
                self.buffer,
                self.settings,
                self.scene_exporter.object_to_node_index,
                self.material_exporter.material_index_by_name,
                bone_to_node_index=self.skin_exporter.bone_to_node_index if self.skin_exporter else None,
            )
            if self.settings.export_all_scenes:
                import bpy
                exported_scenes = list(bpy.data.scenes)
            else:
                exported_scenes = [self.context.scene]
            animation_exporter.gather(self.context, scenes=exported_scenes)
            self._animation_exporter = animation_exporter
            if animation_exporter.animations:
                self._animations = animation_exporter.animations

    def build_finish(self) -> tuple[dict, bytes]:
        """Assemble the glTF from gathered state. Blocks until any still-
        running KTX2 encodes finish and lands them in the buffer first."""
        scenes = self._scenes
        active_scene = self._active_scene
        animations = self._animations
        animation_exporter = self._animation_exporter

        # 2b. Land background-encoded KTX2 images before the buffer freezes.
        self.texture_exporter.resolve_ktx_images()

        # 3. Finalize buffer
        accessors, buffer_views, buffer_desc, binary = self.buffer.finalize()

        # 4. Handle .bin URI for separate format
        if buffer_desc and self.settings.export_format == "GLTF_SEPARATE":
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
        if self.interactivity_exporter:
            all_extensions |= self.interactivity_exporter.extensions_used
        if self.audio_exporter:
            all_extensions |= self.audio_exporter.extensions_used
        if self.walkability_exporter:
            all_extensions |= self.walkability_exporter.extensions_used
        if self.environment_exporter:
            all_extensions |= self.environment_exporter.extensions_used
        required = []
        if self.mesh_exporter.used_quantization:
            # Quantized attribute types are invalid core glTF, so loaders
            # must understand the extension.
            all_extensions.add("KHR_mesh_quantization")
            required.append("KHR_mesh_quantization")
        if "KHR_texture_basisu" in all_extensions:
            # KTX2 textures carry no PNG fallback, so loaders must decode them.
            required.append("KHR_texture_basisu")
        extensions_required = required or None
        extensions_used = sorted(all_extensions) or None

        # 5b. Collect root-level extensions
        root_extensions: dict = {}
        if self.physics_exporter:
            physics_root = self.physics_exporter.get_root_extensions()
            if physics_root:
                root_extensions.update(physics_root)

        # 5c. KHR_lights_punctual root extension
        if self.scene_exporter.lights:
            root_extensions["KHR_lights_punctual"] = {
                "lights": self.scene_exporter.lights,
            }

        # 5d. KHR_interactivity root extension
        if self.interactivity_exporter:
            interactivity_root = self.interactivity_exporter.get_root_extension()
            if interactivity_root:
                root_extensions.update(interactivity_root)

        # 5e. KHR_audio_emitter root extension
        if self.audio_exporter:
            audio_root = self.audio_exporter.get_root_extension()
            if audio_root:
                root_extensions.update(audio_root)

        # 5f. KHR_environment_map root extension
        if self.environment_exporter:
            environment_root = self.environment_exporter.get_root_extension()
            if environment_root:
                root_extensions.update(environment_root)

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
            extensions=root_extensions or None,
            extensions_used=extensions_used,
            extensions_required=extensions_required,
            files=self.scene_exporter.files or None,
        )

        # Image bytes were captured eagerly during material gather, so any
        # temporary baked images/materials/UVs can now be torn down.
        self.material_exporter.cleanup()

        return gltf.to_dict(), binary

    def export(self) -> None:
        gltf_dict, binary = self.build()
        self.write_output(gltf_dict, binary)

    def write_output(self, gltf_dict: dict, binary: bytes) -> None:
        path = Path(self.settings.filepath)

        if self.settings.export_format == "GLB":
            write_glb(path, gltf_dict, binary, force_64bit=self.settings.force_64bit)
        elif self.settings.export_format == "GLTF_EMBEDDED":
            write_gltf_embedded(path, gltf_dict, binary)
        else:
            write_gltf(path, gltf_dict, binary)

    # --- glTF 2.1 [DRAFT] external assets ---

    def _build_external_file(self, collection) -> "File | None":
        """Build a File entry for a collection-instance reference. Embeds the
        sub-asset GLB into the host buffer (PACKAGED) or writes it as a sibling
        file (REFERENCES). Returns None on a reference cycle or excessive depth."""
        if collection.name in self._external_seen or self._external_depth >= _MAX_EXTERNAL_DEPTH:
            print(
                f"glTF export: skipping external asset '{collection.name}' "
                f"(reference cycle or nesting too deep)"
            )
            return None

        seen = self._external_seen | {collection.name}
        sub_dict, sub_binary = build_collection_subasset(
            self.context, self.settings, collection,
            depth=self._external_depth + 1, seen=seen,
        )

        if self.settings.external_assets_mode == "REFERENCES":
            safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in collection.name)
            sub_path = Path(self.settings.filepath).parent / f"{safe}.glb"
            write_glb(sub_path, sub_dict, sub_binary, force_64bit=self.settings.force_64bit)
            return File(uri=sub_path.name, name=collection.name)

        # PACKAGED: embed the sub-asset GLB as a host bufferView.
        glb_bytes = serialize_glb(sub_dict, sub_binary, force_64bit=self.settings.force_64bit)
        bv = self.buffer.add_image_data(glb_bytes)
        return File(buffer_view=bv, mime_type="model/gltf-binary", name=collection.name)


# Max external-asset nesting depth — a backstop against malformed self-references.
_MAX_EXTERNAL_DEPTH = 8


def build_collection_subasset(
    context: "bpy.types.Context",
    base_settings: ExportSettings,
    collection,
    *,
    depth: int,
    seen: set,
) -> tuple[dict, bytes]:
    """Export a single collection as a standalone glTF, returning (dict, binary).
    Runs a nested GltfExporter scoped to the collection; nested collection
    instances recurse into their own external assets (cycle-guarded by ``seen``)."""
    sub_settings = replace(
        base_settings,
        collection_scope=collection,
        export_all_scenes=False,
    )
    sub = GltfExporter(context, sub_settings, external_seen=seen, external_depth=depth)
    return sub.build()
