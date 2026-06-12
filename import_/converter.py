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


# √2 / 2 — half-angle component of the Rx(+90°) fix-up quaternion that undoes
# the Rx(-90°) the exporter applies to cameras/lights/speakers.
_AXIS_FIXUP_S = 0.7071067811865476


def convert_rotation_camera(quat: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Inverse of export.convert_rotation_camera.

    Cameras/lights/speakers point along local -Z in both Blender and glTF, but
    the exporter post-multiplies their rotation by Rx(-90°) so that forward
    survives the Z-up -> Y-up axis swap. This undoes that fix-up (post-multiply
    by Rx(+90°)) and converts glTF (x,y,z,w) Y-up -> Blender (w,x,y,z) Z-up, so
    a round-tripped light/camera lands back at its original orientation.
    """
    gx, gy, gz, gw = quat
    s = _AXIS_FIXUP_S
    return (s * (gw - gx), s * (gw + gx), s * (gy - gz), s * (gy + gz))


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


def matrix_from_gltf(col_major_16: list[float]):
    """Unpack a glTF column-major 16-float matrix into a row-major
    mathutils.Matrix. No axis conversion is applied."""
    import mathutils

    m = col_major_16
    return mathutils.Matrix([
        [m[0], m[4], m[8], m[12]],
        [m[1], m[5], m[9], m[13]],
        [m[2], m[6], m[10], m[14]],
        [m[3], m[7], m[11], m[15]],
    ])


def load_packed_datablock(load_func, name: str, data: bytes, suffix: str):
    """Write `data` to a temp file, load it via `load_func` (e.g.
    bpy.data.images.load / bpy.data.sounds.load), pack the datablock and
    delete the temp file. Returns the datablock, or None when the load or
    decode fails — callers decide on a placeholder/fallback.
    """
    import os
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(data)
        tmp_path = f.name
    try:
        datablock = load_func(tmp_path)
        datablock.name = name
        datablock.pack()
    except RuntimeError as e:
        print(f"glTF import: cannot decode '{name}': {e}")
        datablock = None
    finally:
        os.unlink(tmp_path)
    return datablock


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
