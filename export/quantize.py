"""Numpy helpers for KHR_mesh_quantization attribute encoding."""
from __future__ import annotations

import numpy as np


def position_dequant_transform(
    positions: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Per-axis (offset, scale) mapping normalized uint16 back to mesh space.
    Applied as a node translation/scale wrapping the quantized mesh."""
    pos_min = positions.min(axis=0).astype(np.float32)
    extent = (positions.max(axis=0) - pos_min).astype(np.float32)
    extent[extent == 0.0] = 1.0
    return pos_min, extent


def quantize_positions(
    positions: np.ndarray, offset: np.ndarray, scale: np.ndarray,
) -> np.ndarray:
    norm = (positions - offset) / scale
    return np.clip(np.rint(norm * 65535.0), 0, 65535).astype(np.uint16)


def quantize_normals(normals: np.ndarray) -> np.ndarray:
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    lengths[lengths == 0.0] = 1.0
    unit = normals / lengths
    return np.clip(np.rint(unit * 127.0), -127, 127).astype(np.int8)


def quantize_tangents(tangents: np.ndarray) -> np.ndarray:
    """Quantize VEC4 tangents: xyz direction plus ±1 handedness in w."""
    out = np.empty(tangents.shape, dtype=np.int8)
    out[:, :3] = quantize_normals(tangents[:, :3])
    out[:, 3] = np.where(tangents[:, 3] < 0, -127, 127)
    return out


def can_quantize_uvs(uvs: np.ndarray) -> bool:
    return bool(np.all(uvs >= 0.0) and np.all(uvs <= 1.0))


def quantize_uvs(uvs: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(uvs * 65535.0), 0, 65535).astype(np.uint16)
