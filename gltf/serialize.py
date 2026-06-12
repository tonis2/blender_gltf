from __future__ import annotations

import base64
import json
import struct
from collections import OrderedDict
from pathlib import Path

# glTF JSON key ordering per spec convention
SORT_ORDER = [
    "asset",
    "extensionsUsed",
    "extensionsRequired",
    "extensions",
    "extras",
    "scene",
    "scenes",
    "nodes",
    "cameras",
    "animations",
    "materials",
    "meshes",
    "textures",
    "images",
    "skins",
    "accessors",
    "bufferViews",
    "samplers",
    "buffers",
    "files",
]


def _encode_json(gltf_dict: dict, pretty: bool = False) -> bytes:
    ordered = OrderedDict(
        sorted(
            gltf_dict.items(),
            key=lambda item: SORT_ORDER.index(item[0]) if item[0] in SORT_ORDER else len(SORT_ORDER),
        )
    )
    if pretty:
        text = json.dumps(ordered, indent="\t", separators=(",", ":"), allow_nan=False)
    else:
        text = json.dumps(ordered, separators=(",", ":"), allow_nan=False)
    return text.encode("utf-8")


# Largest total length expressible by a 32-bit (version-2) GLB length field.
_GLB_V2_MAX = 0xFFFFFFFF


def serialize_glb(gltf_dict: dict, binary: bytes, *, force_64bit: bool = False) -> bytes:
    """Serialize a GLB (binary glTF) container to bytes.

    Emits the version-2 container (32-bit length fields) by default, and the
    glTF 2.1 [DRAFT] version-3 container (64-bit length fields) when the file
    would exceed 4 GiB or when ``force_64bit`` is set. Version-2 output is
    byte-identical to the historical writer when 64-bit is not required.
    """
    json_data = _encode_json(gltf_dict)

    # Pad JSON to 4-byte alignment with spaces
    json_pad = (4 - (len(json_data) % 4)) % 4
    json_length = len(json_data) + json_pad

    # Pad binary to 4-byte alignment with zeros
    bin_pad = (4 - (len(binary) % 4)) % 4
    bin_length = len(binary) + bin_pad

    # Version-2 total: header(12) + JSON chunk header(8) + JSON + BIN chunk header(8) + BIN
    total_length = 12 + 8 + json_length
    if bin_length > 0:
        total_length += 8 + bin_length

    if not force_64bit and total_length <= _GLB_V2_MAX:
        return _serialize_glb_v2(json_data, json_pad, json_length, binary, bin_pad, bin_length)
    return _serialize_glb_v3(json_data, json_pad, json_length, binary, bin_pad, bin_length)


def _serialize_glb_v2(json_data, json_pad, json_length, binary, bin_pad, bin_length) -> bytes:
    total_length = 12 + 8 + json_length
    if bin_length > 0:
        total_length += 8 + bin_length

    out = bytearray()
    # GLB header
    out += b"glTF"
    out += struct.pack("<I", 2)  # version
    out += struct.pack("<I", total_length)

    # JSON chunk
    out += struct.pack("<I", json_length)
    out += b"JSON"
    out += json_data
    out += b" " * json_pad

    # BIN chunk
    if bin_length > 0:
        out += struct.pack("<I", bin_length)
        out += b"BIN\x00"
        out += binary
        out += b"\x00" * bin_pad
    return bytes(out)


def _serialize_glb_v3(json_data, json_pad, json_length, binary, bin_pad, bin_length) -> bytes:
    # glTF 2.1 [DRAFT] version-3 container:
    #   header: magic(4) + version=3 (<I, 4) + total length (<Q, 8)  -> 16 bytes
    #   chunk header: length (<Q, 8) + type (4) + reserved encoding (<I, 4) -> 16 bytes
    # The reserved chunk-encoding field is always 0 ("no encoding"); it is a
    # placeholder for future per-chunk compression.
    total_length = 16 + 16 + json_length
    if bin_length > 0:
        total_length += 16 + bin_length

    out = bytearray()
    # GLB header
    out += b"glTF"
    out += struct.pack("<I", 3)  # version
    out += struct.pack("<Q", total_length)

    # JSON chunk
    out += struct.pack("<Q", json_length)
    out += b"JSON"
    out += struct.pack("<I", 0)  # reserved chunk-encoding
    out += json_data
    out += b" " * json_pad

    # BIN chunk
    if bin_length > 0:
        out += struct.pack("<Q", bin_length)
        out += b"BIN\x00"
        out += struct.pack("<I", 0)  # reserved chunk-encoding
        out += binary
        out += b"\x00" * bin_pad
    return bytes(out)


def write_glb(path: Path, gltf_dict: dict, binary: bytes, *, force_64bit: bool = False) -> None:
    """Write a GLB (binary glTF) file."""
    with open(path, "wb") as f:
        f.write(serialize_glb(gltf_dict, binary, force_64bit=force_64bit))


def write_gltf(path: Path, gltf_dict: dict, binary: bytes | None = None) -> None:
    """Write a .gltf JSON file with a separate .bin file."""
    json_data = _encode_json(gltf_dict, pretty=True)

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json_data.decode("utf-8"))
        f.write("\n")

    if binary and len(binary) > 0:
        bin_path = path.with_suffix(".bin")
        with open(bin_path, "wb") as f:
            f.write(binary)


def read_glb(path: Path) -> tuple[dict, bytes]:
    """Read a GLB (binary glTF) file. Returns (gltf_dict, binary_data)."""
    return parse_glb(Path(path).read_bytes())


def parse_glb(data: bytes) -> tuple[dict, bytes]:
    """Parse an in-memory GLB (binary glTF) blob. Returns (gltf_dict, binary).

    Supports both the version-2 container (32-bit lengths) and the glTF 2.1
    [DRAFT] version-3 container (64-bit lengths + reserved chunk-encoding field).
    """
    if len(data) < 12 or data[:4] != b"glTF":
        raise ValueError("Not a valid GLB blob")
    version = struct.unpack("<I", data[4:8])[0]
    if version == 2:
        return _parse_glb_v2(data)
    if version == 3:
        return _parse_glb_v3(data)
    raise ValueError(f"Unsupported GLB version {version}, expected 2 or 3")


def _parse_glb_v2(data: bytes) -> tuple[dict, bytes]:
    # header(12): magic(4) + version(4) + total length(<I,4); chunks: len(<I,4)+type(4)
    total_length = struct.unpack("<I", data[8:12])[0]
    pos = 12
    chunk_length, chunk_type = struct.unpack("<I4s", data[pos:pos + 8])
    pos += 8
    if chunk_type != b"JSON":
        raise ValueError(f"Expected JSON chunk, got {chunk_type!r}")
    gltf_dict = json.loads(data[pos:pos + chunk_length])
    pos += chunk_length

    binary = b""
    if total_length - pos > 8:
        bin_length, bin_type = struct.unpack("<I4s", data[pos:pos + 8])
        pos += 8
        if bin_type == b"BIN\x00":
            binary = data[pos:pos + bin_length]
    return gltf_dict, binary


def _parse_glb_v3(data: bytes) -> tuple[dict, bytes]:
    # header(16): magic(4) + version(4) + total length(<Q,8)
    # chunk header(16): length(<Q,8) + type(4) + reserved encoding(<I,4)
    total_length = struct.unpack("<Q", data[8:16])[0]
    pos = 16
    chunk_length, chunk_type, encoding = struct.unpack("<Q4sI", data[pos:pos + 16])
    pos += 16
    if chunk_type != b"JSON":
        raise ValueError(f"Expected JSON chunk, got {chunk_type!r}")
    if encoding != 0:
        raise ValueError(f"Unsupported GLB chunk encoding {encoding}")
    gltf_dict = json.loads(data[pos:pos + chunk_length])
    pos += chunk_length

    binary = b""
    if total_length - pos > 16:
        bin_length, bin_type, encoding = struct.unpack("<Q4sI", data[pos:pos + 16])
        pos += 16
        if bin_type == b"BIN\x00":
            if encoding != 0:
                raise ValueError(f"Unsupported GLB chunk encoding {encoding}")
            binary = data[pos:pos + bin_length]
    return gltf_dict, binary


def read_gltf(path: Path) -> tuple[dict, bytes | None]:
    """Read a .gltf JSON file. Resolves external .bin or embedded base64 buffers."""
    with open(path, "r", encoding="utf-8") as f:
        gltf_dict = json.load(f)

    binary = None
    buffers = gltf_dict.get("buffers", [])
    if buffers:
        uri = buffers[0].get("uri")
        if uri is not None:
            if uri.startswith("data:"):
                # Base64 data URI
                encoded = uri.split(",", 1)[1]
                binary = base64.b64decode(encoded)
            else:
                # External file
                bin_path = path.parent / uri
                binary = bin_path.read_bytes()

    return gltf_dict, binary


def write_gltf_embedded(path: Path, gltf_dict: dict, binary: bytes | None = None) -> None:
    """Write a single .gltf JSON file with all binary data embedded as base64 data URIs."""
    # Embed the buffer as a data URI. The exporter only ever produces a
    # single buffer; the blob must not be duplicated into multiple buffers.
    buffers = gltf_dict.get("buffers") or []
    if binary and len(binary) > 0 and buffers:
        if len(buffers) > 1:
            raise ValueError(
                f"Embedded glTF export supports a single buffer, got {len(buffers)}"
            )
        encoded = base64.b64encode(binary).decode("ascii")
        buffers[0]["uri"] = f"data:application/octet-stream;base64,{encoded}"

    json_data = _encode_json(gltf_dict, pretty=True)

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json_data.decode("utf-8"))
        f.write("\n")
