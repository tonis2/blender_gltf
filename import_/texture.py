from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from .. import ktx_lib

if TYPE_CHECKING:
    import bpy
    from ..gltf.types import Gltf
    from .buffer_reader import BufferReader
    from ..importer import ImportSettings


# KTX2 decoding is slow (a transcode plus a full float conversion per texture),
# so it runs on this background worker while the operator stays modal and
# Blender keeps responding — the mirror image of the encode worker in
# export/texture.py, and one worker for the same two reasons: libktx already
# fans out internally, and a single thread serializes the library's global
# last-error state. Shared across imports and kept for the whole session,
# because the native library allocates per-thread scratch on first use.
_ktx_executor = None

# Decodes submitted but not yet landed as Blender images. Each one holds a
# full float32 RGBA buffer — 67 MB for a 2K texture — so the queue is drained
# as it fills rather than submitted all at once. Two is the useful depth with
# one worker: one being landed on the main thread while the next decodes.
_MAX_INFLIGHT = 2


def _executor():
    global _ktx_executor
    if _ktx_executor is None:
        from concurrent.futures import ThreadPoolExecutor
        _ktx_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="ktx-decode")
    return _ktx_executor


def _decode_job(data: bytes):
    """Worker: KTX2 blob -> (pixels ready for Image.pixels, width, height).

    Everything that does not need bpy happens here — the transcode itself
    (ctypes releases the GIL) and the uint8 -> float32 conversion and row flip,
    which for a 2K texture is the larger half of the wall clock. The main
    thread is left with bpy.data.images.new + foreach_set.
    """
    import numpy as np

    rgba, w, h = ktx_lib.decode_rgba(data)
    px = np.frombuffer(rgba, dtype=np.uint8).astype(np.float32)
    px /= 255.0
    # KTX2 rows are top-down; Blender stores rows bottom-up. ravel() on the
    # reversed view returns the contiguous copy foreach_set wants.
    return px.reshape(h, w * 4)[::-1].ravel(), w, h


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
        # Background KTX2 decoding (see prefetch_ktx / pump_ktx).
        self._ktx_queue: list[tuple[int, bytes]] = []      # not yet submitted
        self._ktx_inflight: dict[int, object] = {}         # index -> Future
        self._ktx_total = 0

    # ------------------------------------------------------------------
    # Background KTX2 decoding
    # ------------------------------------------------------------------

    def prefetch_ktx(self) -> None:
        """Main thread: pull every KTX2 payload out of the file and queue it.

        Only the payload extraction needs the file/buffer (and so this thread);
        the decode itself is handed to the worker. Nothing is created in
        bpy.data here, so an import cancelled before pump_ktx lands anything
        leaves no trace.
        """
        if self.gltf.images is None:
            return
        for i, gltf_image in enumerate(self.gltf.images):
            data = self._ktx_payload(gltf_image)
            if data is not None:
                self._ktx_queue.append((i, data))
        self._ktx_total = len(self._ktx_queue)
        self._submit_ktx()

    def pump_ktx(self, *, wait: bool = False) -> tuple[int, int]:
        """Land finished decodes as Blender images and keep the worker fed.

        Main thread only. Call it from a modal timer (wait=False) so the UI
        stays alive, or in a loop with wait=True for the blocking path.
        Returns (landed, total) for progress reporting.
        """
        for index in [i for i, f in self._ktx_inflight.items() if f.done()]:
            self._land_ktx(index, self._ktx_inflight.pop(index))
        if wait and self._ktx_inflight:
            # dicts keep insertion order, so this blocks on the oldest decode.
            index = next(iter(self._ktx_inflight))
            self._land_ktx(index, self._ktx_inflight.pop(index))
        self._submit_ktx()
        outstanding = len(self._ktx_queue) + len(self._ktx_inflight)
        return self._ktx_total - outstanding, self._ktx_total

    def ktx_pending_done(self) -> bool:
        return not self._ktx_queue and not self._ktx_inflight

    def cancel_ktx(self) -> None:
        """Abandon queued decodes (an in-flight one finishes and is dropped)."""
        for f in self._ktx_inflight.values():
            f.cancel()
        self._ktx_inflight.clear()
        self._ktx_queue.clear()

    def _submit_ktx(self) -> None:
        while self._ktx_queue and len(self._ktx_inflight) < _MAX_INFLIGHT:
            index, data = self._ktx_queue.pop(0)
            self._ktx_inflight[index] = _executor().submit(_decode_job, data)

    def _land_ktx(self, index: int, future) -> None:
        gltf_image = self.gltf.images[index]
        name = gltf_image.name or f"Image_{index}"
        try:
            px, w, h = future.result()
        except Exception as e:
            print(f"[glTF import] Could not decode KTX2 image '{name}': {e}")
            img = self._placeholder(name)
        else:
            img = self._image_from_pixels(name, px, w, h)
        self._apply_colorspace(index, gltf_image, img)
        self.blender_images[index] = img

    def _ktx_payload(self, gltf_image) -> bytes | None:
        """The image's KTX2 bytes, or None when it is not a KTX2 image."""
        if gltf_image.buffer_view is not None:
            # A declared PNG/JPEG needs no look at the payload at all — reading
            # the buffer view copies the whole image out of the GLB blob.
            mime = gltf_image.mime_type or ""
            if mime and mime != "image/ktx2":
                return None
            data = self.buffer_reader.read_buffer_view_bytes(gltf_image.buffer_view)
            if mime == "image/ktx2" or ktx_lib.is_ktx2(data):
                return bytes(data)
            return None
        if gltf_image.uri is None:
            return None
        if gltf_image.uri.startswith("data:"):
            try:
                data = base64.b64decode(gltf_image.uri.split(",", 1)[1])
            except (ValueError, IndexError):
                return None
            mime = gltf_image.uri.split(";")[0].split(":")[1]
            if ktx_lib.is_ktx2(data) or mime == "image/ktx2":
                return data
            return None
        filepath = self.base_dir / gltf_image.uri
        if filepath.suffix.lower() != ".ktx2":
            return None
        try:
            return filepath.read_bytes()
        except OSError as e:
            print(f"[glTF import] Could not read image '{gltf_image.uri}': {e}")
            return None

    # ------------------------------------------------------------------

    def import_all(self) -> None:
        if self.gltf.images is None:
            return
        # Anything prefetch_ktx queued is landed first; without a modal caller
        # draining it, this is where the wait happens.
        while not self.ktx_pending_done():
            self.pump_ktx(wait=True)
        for i, gltf_image in enumerate(self.gltf.images):
            if i in self.blender_images:
                continue  # landed by pump_ktx
            self.blender_images[i] = self._import_image(i, gltf_image)

    def _import_image(self, index, gltf_image) -> "bpy.types.Image":
        img = self._create_image(index, gltf_image)
        self._apply_colorspace(index, gltf_image, img)
        return img

    def _apply_colorspace(self, index, gltf_image, img) -> None:
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
                if filepath.suffix.lower() == ".ktx2":
                    try:
                        data = filepath.read_bytes()
                    except OSError as e:
                        print(f"[glTF import] Could not read image '{gltf_image.uri}': {e}")
                        return self._placeholder(name)
                    return self._load_ktx2(name, data)
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

        # Blender cannot load KTX2 itself; decode via the ktx native library.
        if ktx_lib.is_ktx2(data) or (mime_type or "") == "image/ktx2":
            return self._load_ktx2(name, data)

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

    def _load_ktx2(self, name: str, data: bytes) -> "bpy.types.Image":
        """Decode a KTX2 payload (BCn or Basis) to a packed Blender image, on
        this thread.

        The fallback for payloads prefetch_ktx never queued — a walkability-only
        import, or a caller that skipped the prefetch. The queued path runs the
        same job on the worker and lands it in _land_ktx.
        """
        try:
            px, w, h = _decode_job(data)
        except Exception as e:
            print(f"[glTF import] Could not decode KTX2 image '{name}': {e}")
            return self._placeholder(name)
        return self._image_from_pixels(name, px, w, h)

    @staticmethod
    def _image_from_pixels(name: str, px, w: int, h: int) -> "bpy.types.Image":
        import bpy

        img = bpy.data.images.new(name, width=w, height=h, alpha=True)
        img.pixels.foreach_set(px)
        try:
            img.pack()
        except RuntimeError:
            pass  # unpacked images still work for the session
        return img

    def get_blender_image(self, image_index: int) -> "bpy.types.Image | None":
        return self.blender_images.get(image_index)
