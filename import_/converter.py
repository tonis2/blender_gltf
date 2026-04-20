from __future__ import annotations

import numpy as np


def convert_location(loc: tuple[float, float, float]) -> tuple[float, float, float]:
    """glTF Y-up (x,y,z) -> Blender Z-up (x,-z,y)."""
    return (loc[0], -loc[2], loc[1])


def convert_rotation(quat: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """glTF quaternion (x,y,z,w) Y-up -> Blender (w,x,y,z) Z-up."""
    gx, gy, gz, gw = quat
    return (gw, gx, -gz, gy)


def convert_scale(scale: tuple[float, float, float]) -> tuple[float, float, float]:
    """glTF (x,y,z) -> Blender (x,z,y). Self-inverse."""
    return (scale[0], scale[2], scale[1])


def convert_positions(positions: np.ndarray) -> np.ndarray:
    """Convert (N,3) positions: glTF [x,y,z] -> Blender [x,-z,y]."""
    result = positions.copy()
    y = result[:, 1].copy()
    result[:, 1] = -result[:, 2]
    result[:, 2] = y
    return result


def convert_normals(normals: np.ndarray) -> np.ndarray:
    """Same axis conversion as positions."""
    return convert_positions(normals)


def flip_uv_v(uvs: np.ndarray) -> np.ndarray:
    """glTF UV v -> Blender v (1-v). Self-inverse."""
    result = uvs.copy()
    result[:, 1] = 1.0 - result[:, 1]
    return result


def convert_location_array(locations: np.ndarray) -> np.ndarray:
    """Convert (N,3) location array: [x,y,z] -> [x,-z,y]."""
    return convert_positions(locations)


def convert_rotation_array(quats: np.ndarray) -> np.ndarray:
    """Convert (N,4) glTF [x,y,z,w] -> Blender [w,x,-z,y]."""
    return np.column_stack([quats[:, 3], quats[:, 0], -quats[:, 2], quats[:, 1]])


def convert_scale_array(scales: np.ndarray) -> np.ndarray:
    """Convert (N,3) scale: [x,y,z] -> [x,z,y]."""
    result = scales.copy()
    result[:, [1, 2]] = result[:, [2, 1]]
    return result


def convert_matrix(col_major_16: list[float]):
    """Convert glTF Y-up column-major 16-float matrix to Blender Z-up Matrix.

    Applies C^-1 @ M @ C where C maps (x,y,z) -> (x,z,-y).
    C^-1 maps (x,y,z) -> (x,-z,y).
    """
    import mathutils

    # Unpack column-major to row-major 4x4
    m = np.array(col_major_16, dtype=np.float64).reshape(4, 4).T

    # Apply C^-1 @ M @ C (inverse of the export conversion)
    # Swap rows 1 and 2, negate new row 1
    m[[1, 2]] = m[[2, 1]]
    m[1] *= -1

    # Swap cols 1 and 2, negate new col 1
    m[:, [1, 2]] = m[:, [2, 1]]
    m[:, 1] *= -1

    return mathutils.Matrix([list(m[i]) for i in range(4)])
