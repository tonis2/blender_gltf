from __future__ import annotations

from typing import TYPE_CHECKING

from ..gltf.buffer import BufferBuilder
from ..gltf.constants import TextureFilter, TextureWrap
from ..gltf.types import Texture, Image, Sampler, TextureInfo

if TYPE_CHECKING:
    import bpy
    from ..exporter import ExportSettings


EXT_TEXTURE_TRANSFORM = "KHR_texture_transform"


class TextureExporter:
    def __init__(self, buffer: BufferBuilder, settings: "ExportSettings") -> None:
        self.buffer = buffer
        self.settings = settings
        self.textures: list[Texture] = []
        self.images: list[Image] = []
        self.samplers: list[Sampler] = []
        self._image_cache: dict[str, int] = {}  # blender image name -> image index
        self._sampler_cache: dict[tuple, int] = {}
        self._texture_cache: dict[tuple[int, int], int] = {}  # (image, sampler) -> texture index
        self.extensions_used: set[str] = set()

    def gather_texture_info(
        self,
        image_node: "bpy.types.ShaderNodeTexImage",
        tex_coord: int = 0,
    ) -> TextureInfo | None:
        """Create a TextureInfo for an Image Texture node. Returns None if no image."""
        if image_node.image is None:
            return None

        texture_index = self._gather_texture(image_node)
        extensions = self._gather_texture_transform(image_node)
        return TextureInfo(
            index=texture_index,
            tex_coord=tex_coord if tex_coord > 0 else None,
            extensions=extensions,
        )

    @staticmethod
    def _find_mapping_node(socket):
        """Follow a texture's Vector input upstream through Reroute nodes and
        return the Mapping node driving it, or None. Guards against cycles.
        """
        seen = set()
        while socket is not None and socket.is_linked:
            up = socket.links[0].from_node
            if id(up) in seen:
                return None
            seen.add(id(up))
            if up.type == "MAPPING":
                return up
            if up.type == "REROUTE":
                socket = up.inputs[0] if up.inputs else None
                continue
            return None
        return None

    def _gather_texture_transform(
        self, image_node: "bpy.types.ShaderNodeTexImage",
    ) -> dict | None:
        """Check for a Mapping node and return KHR_texture_transform extension dict."""
        vector_input = image_node.inputs.get("Vector")
        if vector_input is None or not vector_input.is_linked:
            return None

        # Follow the Vector input through any Reroute nodes to reach the Mapping
        # node. Materials frequently share one Mapping across several textures by
        # fanning it out through Reroutes; without this walk the transform (and
        # thus the texture scale) would be silently dropped for those textures.
        linked_node = self._find_mapping_node(vector_input)
        if linked_node is None:
            return None

        loc = linked_node.inputs["Location"].default_value
        rot = linked_node.inputs["Rotation"].default_value
        scale = linked_node.inputs["Scale"].default_value

        offset_x = float(loc[0])
        offset_y = float(loc[1])
        rotation = float(rot[2])  # Z-axis rotation in radians
        scale_x = float(scale[0])
        scale_y = float(scale[1])

        # Convert Blender UV space to glTF UV space (V is flipped)
        # offset_y_gltf = 1 - scale_y - offset_y_blender
        # rotation_gltf = -rotation_blender
        gltf_offset_x = offset_x
        gltf_offset_y = 1.0 - scale_y - offset_y
        gltf_rotation = -rotation
        gltf_scale_x = scale_x
        gltf_scale_y = scale_y

        # Only emit non-default values
        is_default_offset = abs(gltf_offset_x) < 1e-6 and abs(gltf_offset_y) < 1e-6
        is_default_rotation = abs(gltf_rotation) < 1e-6
        is_default_scale = abs(gltf_scale_x - 1.0) < 1e-6 and abs(gltf_scale_y - 1.0) < 1e-6

        if is_default_offset and is_default_rotation and is_default_scale:
            return None

        transform: dict = {}
        if not is_default_offset:
            transform["offset"] = [gltf_offset_x, gltf_offset_y]
        if not is_default_rotation:
            transform["rotation"] = gltf_rotation
        if not is_default_scale:
            transform["scale"] = [gltf_scale_x, gltf_scale_y]

        self.extensions_used.add(EXT_TEXTURE_TRANSFORM)
        return {EXT_TEXTURE_TRANSFORM: transform}

    def _gather_texture(self, image_node: "bpy.types.ShaderNodeTexImage") -> int:
        sampler_index = self._gather_sampler(image_node)
        image_index = self._gather_image(image_node.image)

        key = (image_index, sampler_index)
        if key in self._texture_cache:
            return self._texture_cache[key]

        tex_index = len(self.textures)
        self.textures.append(Texture(
            source=image_index,
            sampler=sampler_index,
            name=image_node.image.name,
        ))
        self._texture_cache[key] = tex_index
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
        elif image_node.extension == "MIRROR":
            wrap = TextureWrap.MIRRORED_REPEAT
        elif image_node.extension == "CLIP":
            # glTF has no "clip to transparent black"; clamp-to-edge is the
            # closest representable wrap mode.
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

        if self.settings.export_format == "GLB":
            image_index = self._pack_image_to_buffer(blender_image)
        elif self.settings.export_format == "GLTF_EMBEDDED":
            image_index = self._embed_image_as_data_uri(blender_image)
        else:
            image_index = self._write_image_file(blender_image)

        # glTF core has no per-image colorspace; preserve the Blender colorspace
        # in extras so a round-trip keeps non-standard setups (e.g. a diffuse
        # authored as Non-Color) instead of resetting them to the sRGB default.
        cs = getattr(blender_image.colorspace_settings, "name", None)
        if cs:
            img_obj = self.images[image_index]
            extras = dict(img_obj.extras) if isinstance(img_obj.extras, dict) else {}
            extras["colorspace"] = cs
            img_obj.extras = extras

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
        from pathlib import Path

        # Determine output format
        base_dir = Path(self.settings.filepath).parent
        use_jpeg = self._should_use_jpeg(blender_image)

        if use_jpeg:
            ext, mime_type = ".jpg", "image/jpeg"
        else:
            ext, mime_type = ".png", "image/png"

        filename = blender_image.name + ext
        filepath = base_dir / filename

        self._save_image_to_path(blender_image, str(filepath), use_jpeg)

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

    def _should_use_jpeg(self, blender_image: "bpy.types.Image") -> bool:
        """Decide whether to export as JPEG based on settings and image properties."""
        fmt = self.settings.image_format
        if fmt == "JPEG":
            return True
        if fmt == "PNG":
            return False
        # AUTO: use source format
        return blender_image.file_format == "JPEG"

    def _save_image_to_path(
        self, blender_image: "bpy.types.Image", filepath: str, use_jpeg: bool,
    ) -> None:
        """Save a Blender image to a file path.

        For PNG, creates a temporary RGBA image so the output always has an
        alpha channel (games expect RGBA textures).
        """
        import bpy

        if use_jpeg:
            original_path = blender_image.filepath_raw
            original_format = blender_image.file_format
            try:
                blender_image.filepath_raw = filepath
                blender_image.file_format = "JPEG"
                blender_image.save()
            finally:
                blender_image.filepath_raw = original_path
                blender_image.file_format = original_format
        else:
            # Create a temporary RGBA image to guarantee 4-channel PNG output.
            # Blender's image.pixels always stores RGBA internally, but
            # image.save() uses the source channel count (e.g. 3 for JPEG
            # sources), which produces RGB-only PNGs.
            import numpy as np

            w, h = blender_image.size
            pixel_count = w * h * 4
            pixels = np.empty(pixel_count, dtype=np.float32)
            blender_image.pixels.foreach_get(pixels)

            tmp_img = bpy.data.images.new("__gltf_export_tmp__", w, h, alpha=True)
            try:
                tmp_img.pixels.foreach_set(pixels)
                tmp_img.file_format = "PNG"
                tmp_img.filepath_raw = filepath
                tmp_img.save()
            finally:
                bpy.data.images.remove(tmp_img)

    def _get_image_bytes(self, blender_image: "bpy.types.Image") -> tuple[bytes, str]:
        """Get PNG or JPEG bytes for a Blender image."""
        import tempfile
        import os

        use_jpeg = self._should_use_jpeg(blender_image)
        if use_jpeg:
            ext, mime_type = ".jpg", "image/jpeg"
        else:
            ext, mime_type = ".png", "image/png"

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp_path = tmp.name

        try:
            self._save_image_to_path(blender_image, tmp_path, use_jpeg)
            with open(tmp_path, "rb") as f:
                data = f.read()
        finally:
            os.unlink(tmp_path)

        return data, mime_type
