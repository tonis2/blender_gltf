from __future__ import annotations

import math
from typing import TYPE_CHECKING

from ..gltf.buffer import BufferBuilder

if TYPE_CHECKING:
    import bpy
    from ..exporter import ExportSettings


EXT_AUDIO = "KHR_audio_emitter"

# The spec only mandates audio/mpeg; other formats are fine for our own engine.
_MIME_BY_EXT = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
}


class AudioExporter:
    """Exports Blender Speaker objects as KHR_audio_emitter."""

    def __init__(self, buffer: BufferBuilder, settings: "ExportSettings") -> None:
        self.buffer = buffer
        self.settings = settings
        self.audio: list[dict] = []
        self.sources: list[dict] = []
        self.emitters: list[dict] = []
        self._audio_cache: dict[str, int] = {}  # blender sound name -> audio index
        self._emitter_cache: dict[str, int] = {}  # speaker data name -> emitter index
        self.extensions_used: set[str] = set()

    def gather_node(self, obj: "bpy.types.Object") -> dict | None:
        """Return the node-level extension dict for a Speaker object, or None."""
        if obj.type != "SPEAKER":
            return None
        speaker = obj.data
        if speaker is None or speaker.sound is None:
            return None

        if speaker.name in self._emitter_cache:
            return {EXT_AUDIO: {"emitter": self._emitter_cache[speaker.name]}}

        audio_index = self._gather_audio(speaker.sound)
        if audio_index is None:
            return None

        # auto_play/loop have no Blender-native equivalents; they live on the
        # Speaker.gltf_audio PropertyGroup (see operator.py).
        gltf_props = getattr(speaker, "gltf_audio", None)
        source = {
            "name": speaker.sound.name,
            "audio": audio_index,
            "gain": 1.0,
            "autoPlay": gltf_props.auto_play if gltf_props else True,
            "loop": gltf_props.loop if gltf_props else True,
        }
        if abs(speaker.pitch - 1.0) > 1e-6:
            # No spec field for pitch; preserve it for the round-trip.
            source["extras"] = {"pitch": speaker.pitch}
        source_index = len(self.sources)
        self.sources.append(source)

        # Blender's speaker attenuation is OpenAL's inverse-distance-clamped
        # model; cone angles are stored in degrees, glTF wants radians.
        emitter = {
            "name": speaker.name,
            "type": "positional",
            "gain": 0.0 if speaker.muted else speaker.volume,
            "sources": [source_index],
            "positional": {
                "coneInnerAngle": math.radians(speaker.cone_angle_inner),
                "coneOuterAngle": math.radians(speaker.cone_angle_outer),
                "coneOuterGain": speaker.cone_volume_outer,
                "distanceModel": "inverse",
                "refDistance": speaker.distance_reference,
                "maxDistance": speaker.distance_max,
                "rolloffFactor": speaker.attenuation,
            },
        }
        emitter_index = len(self.emitters)
        self.emitters.append(emitter)
        self._emitter_cache[speaker.name] = emitter_index
        self.extensions_used.add(EXT_AUDIO)
        return {EXT_AUDIO: {"emitter": emitter_index}}

    def get_root_extension(self) -> dict | None:
        if not self.emitters:
            return None
        return {
            EXT_AUDIO: {
                "audio": self.audio,
                "sources": self.sources,
                "emitters": self.emitters,
            }
        }

    def _gather_audio(self, sound: "bpy.types.Sound") -> int | None:
        """Pack the sound's bytes per export format. Returns audio index."""
        if sound.name in self._audio_cache:
            return self._audio_cache[sound.name]

        from pathlib import Path
        import bpy

        suffix = Path(sound.filepath).suffix.lower()
        mime_type = _MIME_BY_EXT.get(suffix, "audio/mpeg")

        try:
            if sound.packed_file is not None:
                data = bytes(sound.packed_file.data)
            else:
                data = Path(bpy.path.abspath(sound.filepath)).read_bytes()
        except OSError as e:
            print(f"glTF export: cannot read sound '{sound.name}': {e}")
            return None

        entry: dict = {"name": sound.name, "mimeType": mime_type}
        if self.settings.format == "GLB":
            entry["bufferView"] = self.buffer.add_image_data(data)
        elif self.settings.format == "GLTF_EMBEDDED":
            import base64
            encoded = base64.b64encode(data).decode("ascii")
            entry["uri"] = f"data:{mime_type};base64,{encoded}"
        else:
            filename = Path(sound.filepath).name or f"{sound.name}{suffix or '.mp3'}"
            out_path = Path(self.settings.filepath).parent / filename
            out_path.write_bytes(data)
            entry["uri"] = filename

        index = len(self.audio)
        self.audio.append(entry)
        self._audio_cache[sound.name] = index
        return index
