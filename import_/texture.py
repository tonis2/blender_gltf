from __future__ import annotations

import base64
import os
import struct
import tempfile
import time
import zlib
from pathlib import Path
from typing import TYPE_CHECKING

from .. import ktx_lib

if TYPE_CHECKING:
    import bpy
    from ..gltf.types import Gltf
    from .buffer_reader import BufferReader
    from ..importer import ImportSettings


# KTX2 decoding is slow (a transcode plus a full float conversion per texture),
# so it runs on these background workers while the operator stays modal and
# Blender keeps responding — the mirror image of the encode worker in
# export/texture.py.
#
# More than one, unlike the encode side, because the two halves of a decode
# scale differently. The transcode itself fans out across every core inside
# libktx and takes about 11 ms for a 2K texture; the PNG deflate beside it is
# single-threaded and takes ninety. A small pool overlaps one job's deflate
# with the next job's transcode, and both halves release the GIL. Kept to half
# the cores so the deflates do not crowd out the transcode they run beside.
#
# The one piece of shared state in the decode path is the library's last-error
# string, a single global, so two decodes failing at the same instant can
# report each other's message. That is cosmetic and it is all: the codecs'
# lazily built tables are on the encode side, and each thread gets its own
# scratch allocator on first call. Shared across imports and kept for the whole
# session, because that per-thread scratch is allocated once.
_ktx_executor = None

# Decodes submitted but not yet landed, as a budget in bytes rather than a count
# of jobs. A job's peak is the transcoded RGBA plus the scanline buffer the PNG
# is deflated from — twice the pixels, 33 MB for a 2K texture — and what it
# holds afterwards is only the compressed blob.
#
# It used to be a count of two whatever their size, and that was the import's
# real bottleneck: the queue is only refilled from the operator's modal timer,
# so two-per-tick put a ceiling on the whole import that had nothing to do with
# how fast anything decoded. A kit of 512x512 maps would decode in two
# milliseconds a piece and still arrive at twenty a second. A budget lets
# hundreds of small textures fly while still refusing to hold several 4K images
# at once.
_INFLIGHT_BUDGET = 512 * 1024 * 1024

# Deflate level for the PNG the worker builds. 1 rather than zlib's default 6:
# on a 2K texture it is 90 ms against 400 for a blob 10% larger, and the result
# is still slightly smaller than what Blender's own packing produced before.
_PNG_LEVEL = 1

# Main-thread work one pump is allowed before returning to Blender's event
# loop. Landing an image is bpy work and cannot move off this thread, so the
# choice is between a responsive UI and fewer round trips; 50 ms is the usual
# frame-budget answer to that.
_LAND_BUDGET_S = 0.05


def _worker_count() -> int:
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def _executor():
    global _ktx_executor
    if _ktx_executor is None:
        from concurrent.futures import ThreadPoolExecutor
        _ktx_executor = ThreadPoolExecutor(
            max_workers=_worker_count(), thread_name_prefix="ktx-decode")
    return _ktx_executor


def _decoded_bytes(data: bytes) -> int:
    """How much memory this payload will occupy once decoded, near enough.

    The transcoded pixels plus the scanline buffer beside them, sized from the
    KTX2 header without decoding anything. A payload whose header will not parse is charged
    a pessimistic guess rather than nothing, so a malformed file cannot slip the
    budget by claiming to be free.
    """
    size = ktx_lib.dimensions(data)
    if size is None:
        return len(data) * 8
    w, h = size
    return w * h * 8


def _png_from_rgba(buf, w: int, h: int) -> bytes:
    """Top-down RGBA8 to a PNG blob, on the worker thread.

    A PNG rather than the pixels themselves because of what the main thread can
    then do with it. Handing Blender floats means `bpy.data.images.new` plus a
    `pack()`, and packing a *generated* image has no file to store — Blender
    re-encodes the buffer, 269 ms for a 2K texture, on the main thread, once per
    texture. Handing it an already-encoded PNG makes `pack()` a copy of these
    bytes: 1 ms, and the decode back to pixels happens lazily when the texture is
    first drawn rather than during the import.

    So the encode does not disappear, it moves — off the main thread and onto a
    worker where it runs beside three others. Filter type 0 on every scanline:
    the adaptive filters cost a pass over the image to choose and win little on
    texture data that is about to be deflated at level 1 anyway.

    No row flip. KTX2 and PNG are both top-down and Blender's image loader
    applies its own bottom-up convention on the way in, which is exactly the
    flip the float path had to do by hand.
    """
    import numpy as np

    rows = np.frombuffer(buf, dtype=np.uint8).reshape(h, w * 4)
    raw = np.empty((h, w * 4 + 1), dtype=np.uint8)
    raw[:, 0] = 0
    raw[:, 1:] = rows

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw.tobytes(), _PNG_LEVEL))
            + chunk(b"IEND", b""))


def _decode_job(data: bytes):
    """Worker: KTX2 blob -> (PNG bytes, width, height).

    Everything that does not need bpy happens here — the transcode (ctypes
    releases the GIL) and the PNG encode (zlib does too) — leaving the main
    thread with an `images.new` and a `pack`, which together are about a
    millisecond however large the texture is.

    `decode_rgba_buffer` hands over the library's own allocation rather than a
    `bytes` copy of it, so the pixels are read once, straight into the scanline
    buffer the deflate consumes.
    """
    with ktx_lib.decode_rgba_buffer(data) as (buf, w, h):
        return _png_from_rgba(buf, w, h), w, h


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
        self._ktx_queue: list[tuple[int, bytes, int]] = []  # (index, blob, cost)
        self._ktx_inflight: dict[int, object] = {}          # index -> Future
        self._ktx_cost: dict[int, int] = {}                 # index -> bytes held
        self._inflight_bytes = 0
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
                self._ktx_queue.append((i, data, _decoded_bytes(data)))
        self._ktx_total = len(self._ktx_queue)
        self._submit_ktx()

    def pump_ktx(self, *, wait: bool = False) -> tuple[int, int]:
        """Land finished decodes as Blender images and keep the worker fed.

        Main thread only. Call it from a modal timer (wait=False) so the UI
        stays alive, or in a loop with wait=True for the blocking path.
        Returns (landed, total) for progress reporting.
        """
        # Every decode that has finished, not a fixed number of them — the
        # workers run well ahead of the timer now — but bounded in time so a
        # long run of landings does not hold the event loop.
        deadline = time.perf_counter() + _LAND_BUDGET_S
        for index in [i for i, f in self._ktx_inflight.items() if f.done()]:
            self._land_ktx(index, self._ktx_inflight.pop(index))
            if time.perf_counter() >= deadline:
                break
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
        self._ktx_cost.clear()
        self._inflight_bytes = 0
        self._ktx_queue.clear()

    def _submit_ktx(self) -> None:
        """Fill the workers up to the memory budget, not to a job count.

        One job always goes through however large it is: a single 8K texture is
        over any budget worth setting and still has to be decoded.
        """
        while self._ktx_queue:
            index, data, cost = self._ktx_queue[0]
            if (self._ktx_inflight
                    and self._inflight_bytes + cost > _INFLIGHT_BUDGET):
                break
            self._ktx_queue.pop(0)
            self._ktx_cost[index] = cost
            self._inflight_bytes += cost
            self._ktx_inflight[index] = _executor().submit(_decode_job, data)

    def _land_ktx(self, index: int, future) -> None:
        gltf_image = self.gltf.images[index]
        name = gltf_image.name or f"Image_{index}"
        self._inflight_bytes -= self._ktx_cost.pop(index, 0)
        try:
            blob, w, h = future.result()
        except Exception as e:
            print(f"[glTF import] Could not decode KTX2 image '{name}': {e}")
            img = self._placeholder(name)
        else:
            img = self._image_from_png(name, blob, w, h)
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
            blob, w, h = _decode_job(data)
        except Exception as e:
            print(f"[glTF import] Could not decode KTX2 image '{name}': {e}")
            return self._placeholder(name)
        return self._image_from_png(name, blob, w, h)

    @staticmethod
    def _image_from_png(name: str, blob: bytes, w: int, h: int) -> "bpy.types.Image":
        """A Blender image holding the worker's PNG, without decoding it here.

        `pack(data=...)` stores the blob as the image's embedded file, so the
        image is packed from the moment it exists — no second pass, and it
        survives a .blend save and reload exactly as a packed loaded image does.
        `source = FILE` with an empty filepath is what a packed image is; the
        reload is what makes Blender read the extents and pixels back out of the
        blob it now holds.

        The extents are passed even though the reload overwrites them: if the
        blob were ever unreadable, a correctly sized blank image is a better
        thing to be left with than a 1x1 one.
        """
        import bpy

        img = bpy.data.images.new(name, width=w, height=h, alpha=True)
        img.pack(data=blob, data_len=len(blob))
        img.source = 'FILE'
        img.reload()
        return img

    def get_blender_image(self, image_index: int) -> "bpy.types.Image | None":
        return self.blender_images.get(image_index)
