"""ctypes bindings for the bundled libktx shared library (KTX2 encode/decode).

The library is built from https://github.com/tonis2/ktx.c3 (src/capi) by
native/build-shared.sh and bundled per platform under bin/<os>-<arch>/.
It encodes raw RGBA8 pixels to a complete KTX2 blob (BCn, ETC1S, UASTC,
mipmaps, supercompression) and decodes/transcodes any of those back to RGBA8
— no temp files or subprocesses involved.
"""
from __future__ import annotations

import ctypes
import platform
from pathlib import Path

_LIB_NAMES = {
    ("Linux", "x86_64"): ("linux-x64", "ktx.so"),
    ("Linux", "aarch64"): ("linux-aarch64", "ktx.so"),
    ("Darwin", "arm64"): ("macos-aarch64", "ktx.dylib"),
    ("Darwin", "x86_64"): ("macos-x64", "ktx.dylib"),
    ("Windows", "AMD64"): ("windows-x64", "ktx.dll"),
}

_lib = None
_load_error: str | None = None


def _load():
    global _lib, _load_error
    if _lib is not None or _load_error is not None:
        return _lib
    key = (platform.system(), platform.machine())
    entry = _LIB_NAMES.get(key)
    if entry is None:
        _load_error = f"no bundled ktx library for {key[0]}/{key[1]}"
        return None
    triple, filename = entry
    path = Path(__file__).parent / "bin" / triple / filename
    if not path.is_file():
        _load_error = f"bundled ktx library missing: {path}"
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


def decode_rgba(blob: bytes, level: int = 0, layer: int = 0,
                face: int = 0) -> tuple[bytes, int, int]:
    """Decode one image of a KTX2 blob to top-down RGBA8 → (pixels, w, h)."""
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
        return (bytes(ctypes.cast(out, ctypes.POINTER(ctypes.c_ubyte * size)).contents),
                w.value, h.value)
    finally:
        lib.ktx_free(out)


KTX2_MAGIC = b"\xabKTX 20\xbb\r\n\x1a\n"


def is_ktx2(data: bytes) -> bool:
    return data[:12] == KTX2_MAGIC
