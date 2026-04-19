from __future__ import annotations

from typing import TYPE_CHECKING

from ..gltf.buffer import BufferBuilder
from ..gltf.constants import TextureFilter, TextureWrap
from ..gltf.types import Texture, Image, Sampler, TextureInfo

if TYPE_CHECKING:
    import bpy
    from ..exporter import ExportSettings


class TextureExporter:
    def __init__(self, buffer: BufferBuilder, settings: "ExportSettings") -> None:
        self.buffer = buffer
        self.settings = settings
        self.textures: list[Texture] = []
        self.images: list[Image] = []
        self.samplers: list[Sampler] = []
        self._image_cache: dict[str, int] = {}  # blender image name -> image index
        self._sampler_cache: dict[tuple, int] = {}

    def gather_texture_info(
        self,
        image_node: "bpy.types.ShaderNodeTexImage",
        tex_coord: int = 0,
    ) -> TextureInfo | None:
        """Create a TextureInfo for an Image Texture node. Returns None if no image."""
        if image_node.image is None:
            return None

        texture_index = self._gather_texture(image_node)
        return TextureInfo(
            index=texture_index,
            tex_coord=tex_coord if tex_coord > 0 else None,
        )

    def _gather_texture(self, image_node: "bpy.types.ShaderNodeTexImage") -> int:
        sampler_index = self._gather_sampler(image_node)
        image_index = self._gather_image(image_node.image)

        tex_index = len(self.textures)
        self.textures.append(Texture(
            source=image_index,
            sampler=sampler_index,
            name=image_node.image.name,
        ))
        return tex_index

    def _gather_sampler(self, image_node: "bpy.types.ShaderNodeTexImage") -> int:
        # Map Blender texture settings to glTF sampler
        if image_node.interpolation == "Closest":
            mag_filter = TextureFilter.NEAREST
            min_filter = TextureFilter.NEAREST
        else:
            mag_filter = TextureFilter.LINEAR
            min_filter = TextureFilter.LINEAR_MIPMAP_LINEAR

        if image_node.extension == "REPEAT":
            wrap = TextureWrap.REPEAT
        elif image_node.extension == "EXTEND":
            wrap = TextureWrap.CLAMP_TO_EDGE
        else:
            wrap = TextureWrap.REPEAT

        key = (mag_filter, min_filter, wrap, wrap)
        if key in self._sampler_cache:
            return self._sampler_cache[key]

        index = len(self.samplers)
        self.samplers.append(Sampler(
            mag_filter=mag_filter.value,
            min_filter=min_filter.value,
            wrap_s=wrap.value,
            wrap_t=wrap.value,
        ))
        self._sampler_cache[key] = index
        return index

    def _gather_image(self, blender_image: "bpy.types.Image") -> int:
        if blender_image.name in self._image_cache:
            return self._image_cache[blender_image.name]

        if self.settings.format == "GLB":
            image_index = self._pack_image_to_buffer(blender_image)
        elif self.settings.format == "GLTF_EMBEDDED":
            image_index = self._embed_image_as_data_uri(blender_image)
        else:
            image_index = self._write_image_file(blender_image)

        self._image_cache[blender_image.name] = image_index
        return image_index

    def _pack_image_to_buffer(self, blender_image: "bpy.types.Image") -> int:
        """Pack image data into the GLB buffer."""
        image_data, mime_type = self._get_image_bytes(blender_image)
        bv_index = self.buffer.add_image_data(image_data)

        index = len(self.images)
        self.images.append(Image(
            buffer_view=bv_index,
            mime_type=mime_type,
            name=blender_image.name,
        ))
        return index

    def _write_image_file(self, blender_image: "bpy.types.Image") -> int:
        """Write image to a file alongside the .gltf and reference by URI."""
        import os
        from pathlib import Path

        # Determine output filename
        base_dir = Path(self.settings.filepath).parent
        ext = ".png"
        mime_type = "image/png"

        if blender_image.file_format == "JPEG":
            ext = ".jpg"
            mime_type = "image/jpeg"

        filename = blender_image.name + ext
        filepath = base_dir / filename

        # Save image to file
        original_path = blender_image.filepath_raw
        original_format = blender_image.file_format
        try:
            blender_image.filepath_raw = str(filepath)
            if ext == ".jpg":
                blender_image.file_format = "JPEG"
            else:
                blender_image.file_format = "PNG"
            blender_image.save()
        finally:
            blender_image.filepath_raw = original_path
            blender_image.file_format = original_format

        index = len(self.images)
        self.images.append(Image(
            uri=filename,
            mime_type=mime_type,
            name=blender_image.name,
        ))
        return index

    def _embed_image_as_data_uri(self, blender_image: "bpy.types.Image") -> int:
        """Embed image as a base64 data URI in the glTF JSON."""
        import base64

        image_data, mime_type = self._get_image_bytes(blender_image)
        encoded = base64.b64encode(image_data).decode("ascii")
        data_uri = f"data:{mime_type};base64,{encoded}"

        index = len(self.images)
        self.images.append(Image(
            uri=data_uri,
            mime_type=mime_type,
            name=blender_image.name,
        ))
        return index

    def _get_image_bytes(self, blender_image: "bpy.types.Image") -> tuple[bytes, str]:
        """Get PNG or JPEG bytes for a Blender image."""
        import tempfile
        import os
        from pathlib import Path

        # Use a temporary file to save the image
        if blender_image.file_format == "JPEG":
            ext = ".jpg"
            mime_type = "image/jpeg"
        else:
            ext = ".png"
            mime_type = "image/png"

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp_path = tmp.name

        original_path = blender_image.filepath_raw
        original_format = blender_image.file_format
        try:
            blender_image.filepath_raw = tmp_path
            if ext == ".jpg":
                blender_image.file_format = "JPEG"
            else:
                blender_image.file_format = "PNG"
            blender_image.save()

            with open(tmp_path, "rb") as f:
                data = f.read()
        finally:
            blender_image.filepath_raw = original_path
            blender_image.file_format = original_format
            os.unlink(tmp_path)

        return data, mime_type
