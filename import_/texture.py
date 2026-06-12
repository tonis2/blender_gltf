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
        # Indices of images that carried an explicit colorspace hint in their
        # extras on export. The material importer consults this to avoid
        # clobbering a round-tripped colorspace with per-slot Non-Color forcing.
        self.hinted_images: set[int] = set()

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
                self.hinted_images.add(index)
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
                try:
                    img = bpy.data.images.load(str(filepath))
                except RuntimeError as e:
                    print(f"[glTF import] Could not load image '{gltf_image.uri}': {e}")
                    return self._placeholder(name)
                img.name = name
                return img

        # No buffer view or URI: create a placeholder
        return self._placeholder(name)

    def _placeholder(self, name: str) -> "bpy.types.Image":
        """1x1 stand-in so a missing/undecodable image never aborts the import."""
        import bpy
        return bpy.data.images.new(name, width=1, height=1)

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
        except RuntimeError as e:
            print(f"[glTF import] Could not decode embedded image '{name}': {e}")
            return self._placeholder(name)
        finally:
            os.unlink(tmp_path)
        return img

    def get_blender_image(self, image_index: int) -> "bpy.types.Image | None":
        return self.blender_images.get(image_index)
