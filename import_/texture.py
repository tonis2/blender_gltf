from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import bpy
    from ..gltf.types import Gltf
    from .buffer_reader import BufferReader
    from ..importer import ImportSettings


class TextureImporter:
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
        self.blender_images: dict[int, "bpy.types.Image"] = {}

    def import_all(self) -> None:
        if self.gltf.images is None:
            return
        for i, gltf_image in enumerate(self.gltf.images):
            self.blender_images[i] = self._import_image(i, gltf_image)

    def _import_image(self, index, gltf_image) -> "bpy.types.Image":
        img = self._create_image(index, gltf_image)
        # Restore the Blender colorspace stamped into extras on export, so a
        # round-trip preserves non-standard setups (e.g. a Non-Color diffuse).
        # Falls back to Blender's default / the per-slot Non-Color forcing in
        # the material importer when no hint is present.
        extras = getattr(gltf_image, "extras", None)
        if img is not None and isinstance(extras, dict):
            cs = extras.get("colorspace")
            if cs:
                try:
                    img.colorspace_settings.name = cs
                except (TypeError, ValueError):
                    pass
        return img

    def _create_image(self, index, gltf_image) -> "bpy.types.Image":
        import bpy

        name = gltf_image.name or f"Image_{index}"

        if gltf_image.buffer_view is not None:
            data = self.buffer_reader.read_buffer_view_bytes(gltf_image.buffer_view)
            return self._load_from_bytes(name, data, gltf_image.mime_type)
        elif gltf_image.uri is not None:
            if gltf_image.uri.startswith("data:"):
                encoded = gltf_image.uri.split(",", 1)[1]
                data = base64.b64decode(encoded)
                mime = gltf_image.uri.split(";")[0].split(":")[1]
                return self._load_from_bytes(name, data, mime)
            else:
                filepath = self.base_dir / gltf_image.uri
                img = bpy.data.images.load(str(filepath))
                img.name = name
                return img

        # Fallback: create a placeholder
        import bpy
        img = bpy.data.images.new(name, width=1, height=1)
        return img

    def _load_from_bytes(self, name: str, data: bytes, mime_type: str | None) -> "bpy.types.Image":
        import bpy

        ext = ".png" if "png" in (mime_type or "") else ".jpg"
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
            f.write(data)
            tmp_path = f.name
        try:
            img = bpy.data.images.load(tmp_path)
            img.name = name
            img.pack()
        finally:
            os.unlink(tmp_path)
        return img

    def get_blender_image(self, image_index: int) -> "bpy.types.Image | None":
        return self.blender_images.get(image_index)
