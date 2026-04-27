from __future__ import annotations

import numpy as np


def convert_location(loc: tuple[float, float, float]) -> list[float]:
    """Blender Z-up (x,y,z) -> glTF Y-up (x,z,-y)."""
    return [loc[0], loc[2], -loc[1]]


def convert_rotation(quat: tuple[float, float, float, float]) -> list[float]:
    """Blender quaternion (w,x,y,z) Z-up -> glTF (x,z,-y,w) Y-up."""
    w, x, y, z = quat
    return [x, z, -y, w]


def convert_scale(scale: tuple[float, float, float]) -> list[float]:
    """Blender scale (x,y,z) -> glTF (x,z,y)."""
    return [scale[0], scale[2], scale[1]]


def convert_positions(positions: np.ndarray) -> np.ndarray:
    """Convert position array in-place: swap Y/Z, negate new Z.
    Input shape: (N, 3) with columns [x, y, z].
    Output columns: [x, z, -y].
    """
    positions[:, [1, 2]] = positions[:, [2, 1]]
    positions[:, 2] *= -1
    return positions


def convert_normals(normals: np.ndarray) -> np.ndarray:
    """Same axis conversion as positions."""
    return convert_positions(normals)


def flip_uv_v(uvs: np.ndarray) -> np.ndarray:
    """Blender UV v -> glTF v (1 - v). Input shape: (N, 2)."""
    uvs[:, 1] = 1.0 - uvs[:, 1]
    return uvs


def convert_location_array(locations: np.ndarray) -> np.ndarray:
    """Convert (N, 3) location array: [x,y,z] -> [x,z,-y]."""
    result = locations.copy()
    result[:, [1, 2]] = result[:, [2, 1]]
    result[:, 2] *= -1
    return result


def convert_rotation_array(quats: np.ndarray) -> np.ndarray:
    """Convert (N, 4) quaternion array: Blender [w,x,y,z] -> glTF [x,z,-y,w]."""
    return np.column_stack([quats[:, 1], quats[:, 3], -quats[:, 2], quats[:, 0]])


# √2 / 2 — half-angle component of the Rx(-90°) fix-up quaternion used to align
# Blender camera/light forward (-Z local) with glTF forward after the world
# Z-up -> Y-up axis swap.
_AXIS_FIXUP_S = 0.7071067811865476


def convert_rotation_camera(quat: tuple[float, float, float, float]) -> list[float]:
    """Like convert_rotation, but post-multiplies by Rx(-90°) so the camera's
    glTF local -Z forward direction matches the Blender camera's forward."""
    w, x, y, z = quat
    rx, ry, rz, rw = x, z, -y, w  # standard conversion in (x,y,z,w)
    s = _AXIS_FIXUP_S
    return [
        s * (rx - rw),
        s * (ry - rz),
        s * (ry + rz),
        s * (rw + rx),
    ]


def convert_rotation_camera_array(quats: np.ndarray) -> np.ndarray:
    """Vectorised convert_rotation_camera over (N, 4) Blender [w,x,y,z] input."""
    converted = convert_rotation_array(quats)  # (N, 4) glTF [x,y,z,w]
    rx = converted[:, 0]
    ry = converted[:, 1]
    rz = converted[:, 2]
    rw = converted[:, 3]
    s = _AXIS_FIXUP_S
    return np.column_stack([
        s * (rx - rw),
        s * (ry - rz),
        s * (ry + rz),
        s * (rw + rx),
    ]).astype(quats.dtype, copy=False)


def convert_scale_array(scales: np.ndarray) -> np.ndarray:
    """Convert (N, 3) scale array: [x,y,z] -> [x,z,y]."""
    result = scales.copy()
    result[:, [1, 2]] = result[:, [2, 1]]
    return result


def convert_matrix(mat) -> list[float]:
    """Convert a Blender Z-up 4x4 matrix to glTF Y-up, column-major 16-float list.

    Applies M' = C @ M @ C^-1 where C maps (x,y,z) -> (x,z,-y).
    """
    # Convert to numpy for manipulation
    m = np.array([list(row) for row in mat], dtype=np.float64)

    # Swap rows 1 and 2, negate new row 2
    m[[1, 2]] = m[[2, 1]]
    m[2] *= -1

    # Swap cols 1 and 2, negate new col 2
    m[:, [1, 2]] = m[:, [2, 1]]
    m[:, 2] *= -1

    # Flatten column-major (glTF convention)
    return m.T.flatten().tolist()
