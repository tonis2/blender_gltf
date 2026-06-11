from __future__ import annotations

import base64
import math
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import bpy
    from ..gltf.types import Gltf
    from .buffer_reader import BufferReader
    from ..importer import ImportSettings


_EXT_BY_MIME = {
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
}


class AudioImporter:
    """Recreates Blender Speaker data from KHR_audio_emitter."""

    def __init__(
        self,
        gltf: "Gltf",
        buffer_reader: "BufferReader",
        settings: "ImportSettings",
        base_dir: Path,
    ) -> None:
        self.gltf = gltf
        self.buffer_reader = buffer_reader
        self.settings = settings
        self.base_dir = base_dir
        self._sounds: dict[int, "bpy.types.Sound | None"] = {}

    def has_audio(self) -> bool:
        return bool((self.gltf.extensions or {}).get("KHR_audio_emitter"))

    def _root(self) -> dict:
        return (self.gltf.extensions or {}).get("KHR_audio_emitter", {})

    def create_speaker_data(self, emitter_index: int) -> "bpy.types.Speaker | None":
        import bpy

        emitters = self._root().get("emitters", [])
        if emitter_index >= len(emitters):
            return None
        emitter = emitters[emitter_index]

        speaker = bpy.data.speakers.new(emitter.get("name", f"Emitter_{emitter_index}"))
        speaker.volume = emitter.get("gain", 1.0)

        pos = emitter.get("positional", {})
        speaker.distance_reference = pos.get("refDistance", 1.0)
        speaker.distance_max = pos.get("maxDistance", speaker.distance_max)
        speaker.attenuation = pos.get("rolloffFactor", 1.0)
        # glTF cone angles are radians, Blender stores degrees (max 360)
        speaker.cone_angle_inner = min(
            math.degrees(pos.get("coneInnerAngle", math.tau)), 360.0,
        )
        speaker.cone_angle_outer = min(
            math.degrees(pos.get("coneOuterAngle", math.tau)), 360.0,
        )
        speaker.cone_volume_outer = pos.get("coneOuterGain", speaker.cone_volume_outer)

        sources = self._root().get("sources", [])
        source_indices = emitter.get("sources", [])
        if source_indices and source_indices[0] < len(sources):
            source = sources[source_indices[0]]
            audio_index = source.get("audio")
            if audio_index is not None:
                speaker.sound = self._import_sound(audio_index)
            extras = source.get("extras") or {}
            if "pitch" in extras:
                speaker.pitch = extras["pitch"]
            gltf_props = getattr(speaker, "gltf_audio", None)
            if gltf_props is not None:
                gltf_props.auto_play = source.get("autoPlay", True)
                gltf_props.loop = source.get("loop", True)

        return speaker

    def _import_sound(self, audio_index: int) -> "bpy.types.Sound | None":
        import bpy

        if audio_index in self._sounds:
            return self._sounds[audio_index]

        audio_list = self._root().get("audio", [])
        sound = None
        if audio_index < len(audio_list):
            entry = audio_list[audio_index]
            name = entry.get("name", f"Audio_{audio_index}")
            mime = entry.get("mimeType")
            uri = entry.get("uri")
            if entry.get("bufferView") is not None:
                data = self.buffer_reader.read_buffer_view_bytes(entry["bufferView"])
                sound = self._load_from_bytes(name, data, mime)
            elif uri is not None:
                if uri.startswith("data:"):
                    data = base64.b64decode(uri.split(",", 1)[1])
                    mime = mime or uri.split(";")[0].split(":")[1]
                    sound = self._load_from_bytes(name, data, mime)
                else:
                    try:
                        sound = bpy.data.sounds.load(str(self.base_dir / uri))
                        sound.name = name
                    except RuntimeError as e:
                        print(f"glTF import: cannot load sound '{uri}': {e}")

        self._sounds[audio_index] = sound
        return sound

    def _load_from_bytes(
        self, name: str, data: bytes, mime_type: str | None,
    ) -> "bpy.types.Sound | None":
        import bpy

        ext = _EXT_BY_MIME.get(mime_type or "", ".mp3")
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
            f.write(data)
            tmp_path = f.name
        try:
            sound = bpy.data.sounds.load(tmp_path)
            sound.name = name
            sound.pack()
        except RuntimeError as e:
            print(f"glTF import: cannot decode sound '{name}': {e}")
            sound = None
        finally:
            os.unlink(tmp_path)
        return sound

    def import_global_emitters(
        self,
        gltf_scene,
        collection: "bpy.types.Collection",
    ) -> None:
        """Scene-level (non-positional) emitters become speakers at the origin,
        tagged so a re-export can identify them."""
        import bpy

        scene_ext = (gltf_scene.extensions or {}).get("KHR_audio_emitter", {})
        for emitter_index in scene_ext.get("emitters", []):
            speaker = self.create_speaker_data(emitter_index)
            if speaker is None:
                continue
            obj = bpy.data.objects.new(speaker.name, speaker)
            obj["gltf_global_audio"] = True
            collection.objects.link(obj)
