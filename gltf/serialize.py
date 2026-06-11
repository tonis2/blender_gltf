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


def write_glb(path: Path, gltf_dict: dict, binary: bytes) -> None:
    """Write a GLB (binary glTF) file."""
    json_data = _encode_json(gltf_dict)

    # Pad JSON to 4-byte alignment with spaces
    json_pad = (4 - (len(json_data) % 4)) % 4
    json_length = len(json_data) + json_pad

    # Pad binary to 4-byte alignment with zeros
    bin_pad = (4 - (len(binary) % 4)) % 4
    bin_length = len(binary) + bin_pad

    # Total file length: header(12) + JSON chunk header(8) + JSON + BIN chunk header(8) + BIN
    total_length = 12 + 8 + json_length
    if bin_length > 0:
        total_length += 8 + bin_length

    with open(path, "wb") as f:
        # GLB header
        f.write(b"glTF")
        f.write(struct.pack("<I", 2))  # version
        f.write(struct.pack("<I", total_length))

        # JSON chunk
        f.write(struct.pack("<I", json_length))
        f.write(b"JSON")
        f.write(json_data)
        f.write(b" " * json_pad)

        # BIN chunk
        if bin_length > 0:
            f.write(struct.pack("<I", bin_length))
            f.write(b"BIN\x00")
            f.write(binary)
            f.write(b"\x00" * bin_pad)


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
    with open(path, "rb") as f:
        # GLB header: magic(4) + version(4) + length(4)
        header = f.read(12)
        if len(header) < 12 or header[:4] != b"glTF":
            raise ValueError(f"Not a valid GLB file: {path}")
        version, total_length = struct.unpack("<II", header[4:12])
        if version != 2:
            raise ValueError(f"Unsupported GLB version {version}, expected 2")

        # Read JSON chunk
        chunk_header = f.read(8)
        chunk_length, chunk_type = struct.unpack("<I4s", chunk_header)
        if chunk_type != b"JSON":
            raise ValueError(f"Expected JSON chunk, got {chunk_type!r}")
        json_data = f.read(chunk_length)
        gltf_dict = json.loads(json_data)

        # Read BIN chunk (optional)
        binary = b""
        remaining = total_length - 12 - 8 - chunk_length
        if remaining > 8:
            chunk_header = f.read(8)
            chunk_length, chunk_type = struct.unpack("<I4s", chunk_header)
            if chunk_type == b"BIN\x00":
                binary = f.read(chunk_length)

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
