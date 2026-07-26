from __future__ import annotations

from typing import TYPE_CHECKING

from ..gltf.buffer import BufferBuilder
from ..gltf.constants import TextureFilter, TextureWrap
from ..gltf.types import Texture, Image, Sampler, TextureInfo
from .. import ktx_lib

if TYPE_CHECKING:
    import bpy
    from ..exporter import ExportSettings


EXT_TEXTURE_TRANSFORM = "KHR_texture_transform"
EXT_TEXTURE_BASISU = "KHR_texture_basisu"

# KTX2 encoding is slow (seconds per texture), so it runs on this background
# worker while the operator stays modal and Blender keeps responding. One
# worker is deliberate: libktx already fans out across every core internally,
# and a single thread serializes access to the library's last-error state.
# The worker is shared across exports and lives for the whole session — the
# native library allocates per-thread scratch on first use, so churning
# threads would leak it.
_ktx_executor = None


def _executor():
    global _ktx_executor
    if _ktx_executor is None:
        from concurrent.futures import ThreadPoolExecutor
        _ktx_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="ktx-encode")
    return _ktx_executor


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
        self._ktx2_images: set[int] = set()  # image indices holding KTX2 payloads
        # (image index, Future[bytes], blender image name) awaiting resolve.
        self._pending_ktx: list[tuple[int, object, str]] = []
        self.extensions_used: set[str] = set()

    def gather_texture_info(
        self,
        image_node: "bpy.types.ShaderNodeTexImage",
        tex_coord: int = 0,
        usage: str = "color",
    ) -> TextureInfo | None:
        """Create a TextureInfo for an Image Texture node. Returns None if no image.

        usage ("color" / "normal" / "height") routes KTX2 encoding: normal and
        height maps get the dedicated codec/quality settings since they need
        more detail than color textures survive with.
        """
        if image_node.image is None:
            return None

        texture_index = self._gather_texture(image_node, usage)
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

    def _gather_texture(
        self, image_node: "bpy.types.ShaderNodeTexImage", usage: str = "color",
    ) -> int:
        sampler_index = self._gather_sampler(image_node)
        image_index = self._gather_image(image_node.image, usage)

        key = (image_index, sampler_index)
        if key in self._texture_cache:
            return self._texture_cache[key]

        tex_index = len(self.textures)
        if image_index in self._ktx2_images:
            # KHR_texture_basisu: the KTX2 image is referenced from the
            # extension, not texture.source (we ship no PNG fallback).
            self.extensions_used.add(EXT_TEXTURE_BASISU)
            self.textures.append(Texture(
                sampler=sampler_index,
                name=image_node.image.name,
                extensions={EXT_TEXTURE_BASISU: {"source": image_index}},
            ))
        else:
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

    def _gather_image(
        self, blender_image: "bpy.types.Image", usage: str = "color",
    ) -> int:
        # Cache is keyed by image name only, so an image reused with different
        # usages (e.g. as both color and normal map) encodes once with
        # whichever usage is gathered first — a pathological setup anyway.
        if blender_image.name in self._image_cache:
            return self._image_cache[blender_image.name]

        image_index = None
        if self.settings.image_format == "KTX2":
            try:
                image_index = self._gather_image_ktx2(blender_image, usage)
                self._ktx2_images.add(image_index)
            except Exception as e:
                print(f"[glTF export] KTX2 encode failed for "
                      f"'{blender_image.name}', falling back to PNG/JPEG: {e}")
                image_index = None

        if image_index is None:
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

    def _extract_rgba(
        self, blender_image: "bpy.types.Image", usage: str = "color",
    ) -> tuple[bytes, int, int, str]:
        """Pull top-down RGBA8 pixels + ktx format string. Main thread only
        (touches bpy data); the returned bytes are safe to hand to a worker."""
        import numpy as np

        w, h = blender_image.size
        if not w or not h:
            raise ValueError("image has no pixel data")
        pixels = np.empty(w * h * 4, dtype=np.float32)
        blender_image.pixels.foreach_get(pixels)
        # Blender stores rows bottom-up; KTX2 wants the top-down orientation a
        # PNG export would have, so glTF UVs keep working unchanged.
        rgba = np.clip(
            pixels.reshape(h, w * 4)[::-1].ravel() * 255.0 + 0.5, 0.0, 255.0,
        ).astype(np.uint8).tobytes()

        cs = getattr(blender_image.colorspace_settings, "name", "sRGB")
        detail = usage in ("normal", "height")
        codec = (self.settings.ktx_normal_codec if detail
                 else self.settings.ktx_codec) or "uastc"
        fmt = codec if cs == "sRGB" else codec + "-linear"
        return rgba, w, h, fmt

    def _gather_image_ktx2(
        self, blender_image: "bpy.types.Image", usage: str = "color",
    ) -> int:
        """Queue a KTX2 image for background encoding and emit a placeholder.

        Pixels are extracted here (bpy access needs the main thread), the
        encode itself runs on the shared worker — ctypes releases the GIL, so
        Blender's event loop keeps running. resolve_ktx_images() patches the
        placeholder once the blob exists; until then the Image has only a
        name/mime_type. ktx_lib availability was checked by the operator.
        """
        rgba, w, h, fmt = self._extract_rgba(blender_image, usage)
        detail = usage in ("normal", "height")
        quality = (self.settings.ktx_normal_quality if detail
                   else self.settings.ktx_quality)
        # normal_map only matters on the VkFormat path (mip renormalization);
        # passing it for basis codecs is a harmless no-op.
        future = _executor().submit(
            ktx_lib.encode_rgba, rgba, w, h, fmt, mipmaps=True,
            normal_map=(usage == "normal"), quality=quality,
            effort=self.settings.ktx_effort)

        index = len(self.images)
        self.images.append(Image(mime_type="image/ktx2", name=blender_image.name))
        self._pending_ktx.append((index, future, blender_image.name))
        return index

    def add_ktx_blob(self, blob: bytes, name: str) -> int:
        """Append an already-encoded KTX2 blob as an image, returning its index.

        For payloads built outside the bpy.types.Image path — the environment
        cubemap, for one. Placement follows the same GLB-buffer / sidecar-file
        rule as resolve_ktx_images().
        """
        index = len(self.images)
        img = Image(mime_type="image/ktx2", name=name)
        if self.settings.export_format == "GLB":
            img.buffer_view = self.buffer.add_image_data(blob)
        else:
            from pathlib import Path
            filename = name + ".ktx2"
            (Path(self.settings.filepath).parent / filename).write_bytes(blob)
            img.uri = filename
        self.images.append(img)
        self._ktx2_images.add(index)
        return index

    def ktx_progress(self) -> tuple[int, int]:
        """(finished, total) background KTX encodes for this export."""
        done = sum(1 for _, f, _ in self._pending_ktx if f.done())
        return done, len(self._pending_ktx)

    def ktx_pending_done(self) -> bool:
        return all(f.done() for _, f, _ in self._pending_ktx)

    def cancel_ktx(self) -> None:
        """Abandon queued encodes (an in-flight one finishes and is dropped)."""
        for _, f, _ in self._pending_ktx:
            f.cancel()
        self._pending_ktx.clear()

    def resolve_ktx_images(self) -> None:
        """Land finished KTX blobs in their placeholders (main thread).

        Blocks on any encode still running, so callers wanting a responsive UI
        should wait for ktx_pending_done() first. A failed encode falls back
        to PNG/JPEG like the synchronous path used to, which also means
        rewriting any texture that referenced the image via
        KHR_texture_basisu back to a plain source.
        """
        pending, self._pending_ktx = self._pending_ktx, []
        for index, future, image_name in pending:
            try:
                blob = future.result()
            except Exception as e:
                print(f"[glTF export] KTX2 encode failed for "
                      f"'{image_name}', falling back to PNG/JPEG: {e}")
                self._fallback_ktx_image(index, image_name)
                continue

            img = self.images[index]
            if self.settings.export_format == "GLB":
                img.buffer_view = self.buffer.add_image_data(blob)
            elif self.settings.export_format == "GLTF_EMBEDDED":
                import base64
                encoded = base64.b64encode(blob).decode("ascii")
                img.uri = f"data:image/ktx2;base64,{encoded}"
            else:
                from pathlib import Path
                filename = image_name + ".ktx2"
                (Path(self.settings.filepath).parent / filename).write_bytes(blob)
                img.uri = filename

        # If every KTX image fell back, the basisu extension is no longer used
        # (extension lists are assembled after this runs).
        if not any(EXT_TEXTURE_BASISU in (t.extensions or {}) for t in self.textures):
            self.extensions_used.discard(EXT_TEXTURE_BASISU)

    def _fallback_ktx_image(self, index: int, image_name: str) -> None:
        """Replace a failed KTX2 placeholder with PNG/JPEG bytes in place."""
        import bpy

        img = self.images[index]
        img.mime_type = None
        self._ktx2_images.discard(index)
        # Textures point at KTX2 images through KHR_texture_basisu; move the
        # reference back to the core source field.
        for tex in self.textures:
            ext = tex.extensions or {}
            if ext.get(EXT_TEXTURE_BASISU, {}).get("source") == index:
                del ext[EXT_TEXTURE_BASISU]
                tex.extensions = ext or None
                tex.source = index

        blender_image = bpy.data.images.get(image_name)
        if blender_image is None:
            print(f"[glTF export] image '{image_name}' vanished during export")
            return
        image_data, mime_type = self._get_image_bytes(blender_image)
        img.mime_type = mime_type
        if self.settings.export_format == "GLB":
            img.buffer_view = self.buffer.add_image_data(image_data)
        elif self.settings.export_format == "GLTF_EMBEDDED":
            import base64
            encoded = base64.b64encode(image_data).decode("ascii")
            img.uri = f"data:{mime_type};base64,{encoded}"
        else:
            from pathlib import Path
            ext = ".jpg" if mime_type == "image/jpeg" else ".png"
            filename = image_name + ext
            (Path(self.settings.filepath).parent / filename).write_bytes(image_data)
            img.uri = filename

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
