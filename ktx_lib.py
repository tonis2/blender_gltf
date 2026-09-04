"""ctypes bindings for the libktx shared library (KTX2 encode/decode).

The library is built from https://github.com/tonis2/ktx.c3 (src/capi) and
published per platform as a raw asset on that repo's GitHub releases
(ktx-<os>-<arch>.{so,dylib,dll}). It is NOT bundled with the addon: the
operator in ktx_download.py asks the user once and fetches only the asset
matching this machine into download_dest(). A lib placed in bin/<os>-<arch>/
next to this file (e.g. by ktx.c3's native/bundle-blender.sh during
development) takes precedence over a downloaded one.

It encodes raw RGBA8 pixels to a complete KTX2 blob (BCn, ETC1S, UASTC,
mipmaps, supercompression) and decodes/transcodes any of those back to RGBA8
— no temp files or subprocesses involved.
"""
from __future__ import annotations

import contextlib
import ctypes
import platform
import struct
from pathlib import Path

GITHUB_REPO = "tonis2/ktx.c3"
# Release tag whose assets are downloaded; None means the newest release.
RELEASE_TAG: str | None = None

_LIB_NAMES = {
    ("Linux", "x86_64"): ("linux-x64", "ktx.so"),
    ("Linux", "aarch64"): ("linux-aarch64", "ktx.so"),
    ("Darwin", "arm64"): ("macos-aarch64", "ktx.dylib"),
    ("Darwin", "x86_64"): ("macos-x64", "ktx.dylib"),
    ("Windows", "AMD64"): ("windows-x64", "ktx.dll"),
}

_lib = None
_load_error: str | None = None


def _entry() -> tuple[str, str] | None:
    """(target triple, lib filename) for this machine, or None."""
    return _LIB_NAMES.get((platform.system(), platform.machine()))


def _download_root(create: bool = False) -> Path:
    """Directory downloaded libs live under (<root>/<triple>/<filename>).

    As a Blender 4.2+ extension the addon directory is replaced on every
    update, so downloads go to the extension's persistent user dir. Legacy
    scripts/addons installs fall back to the addon's own bin/ — the same
    place the libs used to be bundled.
    """
    pkg = __package__ or ""
    if pkg.startswith("bl_ext."):
        import bpy
        return Path(bpy.utils.extension_path_user(pkg, path="bin", create=create))
    return Path(__file__).parent / "bin"


def asset_name() -> str | None:
    """Release asset for this machine, e.g. 'ktx-macos-aarch64.dylib'."""
    entry = _entry()
    if entry is None:
        return None
    triple, filename = entry
    return f"ktx-{triple}.{filename.rsplit('.', 1)[1]}"


def download_url() -> str | None:
    asset = asset_name()
    if asset is None:
        return None
    base = f"https://github.com/{GITHUB_REPO}/releases"
    if RELEASE_TAG is None:
        return f"{base}/latest/download/{asset}"
    return f"{base}/download/{RELEASE_TAG}/{asset}"


def download_dest(create: bool = False) -> Path | None:
    """Where ktx_download.py installs the lib for this machine."""
    entry = _entry()
    if entry is None:
        return None
    triple, filename = entry
    dest_dir = _download_root(create) / triple
    if create:
        dest_dir.mkdir(parents=True, exist_ok=True)
    return dest_dir / filename


def _candidates() -> list[Path]:
    entry = _entry()
    if entry is None:
        return []
    triple, filename = entry
    paths = [Path(__file__).parent / "bin" / triple / filename,
             _download_root() / triple / filename]
    return list(dict.fromkeys(paths))


def installed_path() -> Path | None:
    """First existing lib (dev-bundled or downloaded), or None."""
    return next((p for p in _candidates() if p.is_file()), None)


def reset() -> None:
    """Forget the cached handle/error so the next call retries loading."""
    global _lib, _load_error
    _lib = None
    _load_error = None


def _load():
    global _lib, _load_error
    if _lib is not None or _load_error is not None:
        return _lib
    entry = _entry()
    if entry is None:
        _load_error = (f"no prebuilt ktx library for "
                       f"{platform.system()}/{platform.machine()}")
        return None
    path = installed_path()
    if path is None:
        _load_error = ("ktx native library is not installed — use the "
                       "'Download KTX Binaries' button in the export "
                       "dialog's Material panel")
        return None
    try:
        lib = ctypes.CDLL(str(path))
    except OSError as e:
        _load_error = f"could not load {path}: {e}"
        return None

    lib.ktx_encode.restype = ctypes.c_int
    lib.ktx_encode.argtypes = [
        ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_char_p,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    # Only libs built after the cubemap entry point landed export this, so it
    # is bound conditionally and reported by is_cube_available(); callers can
    # then degrade instead of dying on an attribute lookup.
    if hasattr(lib, "ktx_encode_cube"):
        lib.ktx_encode_cube.restype = ctypes.c_int
        lib.ktx_encode_cube.argtypes = [
            ctypes.POINTER(ctypes.c_char_p), ctypes.c_uint, ctypes.c_char_p,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
            ctypes.POINTER(ctypes.c_size_t),
        ]
    lib.ktx_decode.restype = ctypes.c_int
    lib.ktx_decode.argtypes = [
        ctypes.c_char_p, ctypes.c_size_t,
        ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
        ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
    ]
    lib.ktx_free.argtypes = [ctypes.c_void_p]
    lib.ktx_last_error.restype = ctypes.c_char_p
    _lib = lib
    return lib


def is_available() -> bool:
    return _load() is not None


def load_error() -> str | None:
    _load()
    return _load_error


def is_cube_available() -> bool:
    """Whether the loaded lib can encode cubemaps (ktx_encode_cube)."""
    lib = _load()
    return lib is not None and hasattr(lib, "ktx_encode_cube")


def encode_cube(faces: "list[bytes]", size: int, fmt: str, *,
                mipmaps: bool = True, quality: int = 90, effort: int = 2,
                zstd_level: int = 0) -> bytes:
    """Encode six square RGBA8 faces to a KTX2 cubemap blob (faceCount 6).

    faces must be in KTX order — +X, -X, +Y, -Y, +Z, -Z — each holding
    size*size*4 top-down bytes. fmt takes the same names as encode_rgba.
    """
    lib = _load()
    if lib is None:
        raise RuntimeError(_load_error)
    if not hasattr(lib, "ktx_encode_cube"):
        raise RuntimeError(
            "the installed ktx library predates cubemap support — re-download "
            "it from the export dialog's Material panel")
    if len(faces) != 6:
        raise ValueError(f"need exactly 6 faces, got {len(faces)}")
    expect = size * size * 4
    for i, f in enumerate(faces):
        if len(f) != expect:
            raise ValueError(f"face {i}: expected {expect} bytes, got {len(f)}")

    arr = (ctypes.c_char_p * 6)(*faces)
    out = ctypes.POINTER(ctypes.c_ubyte)()
    n = ctypes.c_size_t()
    rc = lib.ktx_encode_cube(arr, size, fmt.encode(), int(mipmaps), 0,
                             quality, effort, zstd_level,
                             ctypes.byref(out), ctypes.byref(n))
    if rc != 0:
        raise RuntimeError(f"ktx cube encode ({fmt}): {lib.ktx_last_error().decode()}")
    try:
        return bytes(ctypes.cast(out, ctypes.POINTER(ctypes.c_ubyte * n.value)).contents)
    finally:
        lib.ktx_free(out)


def encode_rgba(rgba: bytes, width: int, height: int, fmt: str, *,
                mipmaps: bool = True, normal_map: bool = False,
                quality: int = 90, effort: int = 2,
                zstd_level: int = 0) -> bytes:
    """Encode top-down RGBA8 pixels (width*height*4 bytes) to a KTX2 blob.

    fmt: "uastc", "etc1s" ("-linear" variants for non-color data), or a
    VkFormat alias like "bc7-srgb" / "bc5" / "rgba8-srgb".
    """
    lib = _load()
    if lib is None:
        raise RuntimeError(_load_error)
    out = ctypes.POINTER(ctypes.c_ubyte)()
    n = ctypes.c_size_t()
    rc = lib.ktx_encode(rgba, width, height, fmt.encode(),
                        int(mipmaps), int(normal_map), quality, effort,
                        zstd_level, ctypes.byref(out), ctypes.byref(n))
    if rc != 0:
        raise RuntimeError(f"ktx encode ({fmt}): {lib.ktx_last_error().decode()}")
    try:
        return bytes(ctypes.cast(out, ctypes.POINTER(ctypes.c_ubyte * n.value)).contents)
    finally:
        lib.ktx_free(out)


@contextlib.contextmanager
def decode_rgba_buffer(blob: bytes, level: int = 0, layer: int = 0, face: int = 0):
    """Decode one image of a KTX2 blob and yield (buffer, w, h) without copying.

    ``buffer`` is the library's own allocation exposed through the buffer
    protocol, so ``np.frombuffer`` wraps it with no copy at all. It is freed
    when the block exits and must not outlive it — a caller that keeps the
    pixels has to read them out inside, which converting to float32 does
    anyway. ``decode_rgba`` below is the copying convenience wrapper.
    """
    lib = _load()
    if lib is None:
        raise RuntimeError(_load_error)
    out = ctypes.POINTER(ctypes.c_ubyte)()
    w = ctypes.c_uint()
    h = ctypes.c_uint()
    rc = lib.ktx_decode(blob, len(blob), level, layer, face,
                        ctypes.byref(out), ctypes.byref(w), ctypes.byref(h))
    if rc != 0:
        raise RuntimeError(f"ktx decode: {lib.ktx_last_error().decode()}")
    try:
        size = w.value * h.value * 4
        yield (ctypes.cast(out, ctypes.POINTER(ctypes.c_ubyte * size)).contents,
               w.value, h.value)
    finally:
        lib.ktx_free(out)


def decode_rgba(blob: bytes, level: int = 0, layer: int = 0,
                face: int = 0) -> tuple[bytes, int, int]:
    """Decode one image of a KTX2 blob to top-down RGBA8 → (pixels, w, h)."""
    with decode_rgba_buffer(blob, level, layer, face) as (buf, w, h):
        return bytes(buf), w, h


def dimensions(blob: bytes) -> tuple[int, int] | None:
    """(width, height) straight out of the KTX2 header, decoding nothing.

    The level-0 extents live at fixed offsets 20 and 24 of every KTX2 file,
    which is what lets the importer size a decode before it runs one and keep
    its in-flight queue to a memory budget rather than to a job count.
    ``pixelHeight`` is 0 for a 1D texture; that reads as one row here.
    """
    if len(blob) < 32 or not is_ktx2(blob):
        return None
    w, h = struct.unpack_from("<II", blob, 20)
    return (w, h or 1)


KTX2_MAGIC = b"\xabKTX 20\xbb\r\n\x1a\n"


def is_ktx2(data: bytes) -> bool:
    return data[:12] == KTX2_MAGIC
