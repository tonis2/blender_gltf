from enum import IntEnum, Enum
import numpy as np


class ComponentType(IntEnum):
    BYTE = 5120
    UNSIGNED_BYTE = 5121
    SHORT = 5122
    UNSIGNED_SHORT = 5123
    UNSIGNED_INT = 5125
    FLOAT = 5126

    @property
    def byte_size(self) -> int:
        return {
            ComponentType.BYTE: 1,
            ComponentType.UNSIGNED_BYTE: 1,
            ComponentType.SHORT: 2,
            ComponentType.UNSIGNED_SHORT: 2,
            ComponentType.UNSIGNED_INT: 4,
            ComponentType.FLOAT: 4,
        }[self]

    @property
    def numpy_dtype(self) -> np.dtype:
        return {
            ComponentType.BYTE: np.dtype(np.int8),
            ComponentType.UNSIGNED_BYTE: np.dtype(np.uint8),
            ComponentType.SHORT: np.dtype(np.int16),
            ComponentType.UNSIGNED_SHORT: np.dtype(np.uint16),
            ComponentType.UNSIGNED_INT: np.dtype(np.uint32),
            ComponentType.FLOAT: np.dtype(np.float32),
        }[self]


class DataType(str, Enum):
    SCALAR = "SCALAR"
    VEC2 = "VEC2"
    VEC3 = "VEC3"
    VEC4 = "VEC4"
    MAT2 = "MAT2"
    MAT3 = "MAT3"
    MAT4 = "MAT4"

    @property
    def num_components(self) -> int:
        return {
            DataType.SCALAR: 1,
            DataType.VEC2: 2,
            DataType.VEC3: 3,
            DataType.VEC4: 4,
            DataType.MAT2: 4,
            DataType.MAT3: 9,
            DataType.MAT4: 16,
        }[self]


class BufferViewTarget(IntEnum):
    ARRAY_BUFFER = 34962
    ELEMENT_ARRAY_BUFFER = 34963


class TextureFilter(IntEnum):
    NEAREST = 9728
    LINEAR = 9729
    NEAREST_MIPMAP_NEAREST = 9984
    LINEAR_MIPMAP_NEAREST = 9985
    NEAREST_MIPMAP_LINEAR = 9986
    LINEAR_MIPMAP_LINEAR = 9987


class TextureWrap(IntEnum):
    CLAMP_TO_EDGE = 33071
    MIRRORED_REPEAT = 33648
    REPEAT = 10497
