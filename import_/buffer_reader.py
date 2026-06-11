from __future__ import annotations

import base64
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from ..gltf.constants import ComponentType, DataType

if TYPE_CHECKING:
    from ..gltf.types import Gltf


class BufferReader:
    """Read accessor data from glTF binary buffers."""

    def __init__(self, gltf: "Gltf", binary: bytes, base_dir: Path) -> None:
        self.gltf = gltf
        self._buffers: dict[int, bytes] = {0: binary} if binary else {}
        self._base_dir = base_dir

    def _resolve_buffer(self, buffer_index: int) -> bytes:
        if buffer_index in self._buffers:
            return self._buffers[buffer_index]
        buf = self.gltf.buffers[buffer_index]
        if buf.uri is None:
            raise ValueError(f"Buffer {buffer_index} has no URI and no binary chunk")
        if buf.uri.startswith("data:"):
            data = base64.b64decode(buf.uri.split(",", 1)[1])
        else:
            data = (self._base_dir / buf.uri).read_bytes()
        self._buffers[buffer_index] = data
        return data

    def read_accessor(self, accessor_index: int) -> np.ndarray:
        """Read accessor data as a numpy array shaped (count, num_components)."""
        acc = self.gltf.accessors[accessor_index]
        component_type = ComponentType(acc.component_type)
        data_type = DataType(acc.type)
        dtype = component_type.numpy_dtype
        num_components = data_type.num_components

        bv = self.gltf.buffer_views[acc.buffer_view]
        buffer_data = self._resolve_buffer(bv.buffer)

        bv_offset = bv.byte_offset or 0
        acc_offset = acc.byte_offset or 0
        start = bv_offset + acc_offset
        element_size = num_components * dtype.itemsize

        if bv.byte_stride and bv.byte_stride != element_size:
            # Strided / interleaved buffer view
            result = np.empty((acc.count, num_components), dtype=dtype)
            for i in range(acc.count):
                elem_start = start + i * bv.byte_stride
                result[i] = np.frombuffer(
                    buffer_data, dtype=dtype, count=num_components, offset=elem_start,
                )
        else:
            # Tightly packed
            total = acc.count * num_components
            data = np.frombuffer(buffer_data, dtype=dtype, count=total, offset=start)
            if num_components > 1:
                data = data.reshape(acc.count, num_components)
            result = data.copy()

        if acc.normalized:
            result = self._dequantize(result, component_type)
        return result

    @staticmethod
    def _dequantize(data: np.ndarray, component_type: ComponentType) -> np.ndarray:
        """Convert normalized integer data to float32 per the glTF spec."""
        if component_type == ComponentType.BYTE:
            return np.maximum(data.astype(np.float32) / 127.0, -1.0)
        if component_type == ComponentType.UNSIGNED_BYTE:
            return data.astype(np.float32) / 255.0
        if component_type == ComponentType.SHORT:
            return np.maximum(data.astype(np.float32) / 32767.0, -1.0)
        if component_type == ComponentType.UNSIGNED_SHORT:
            return data.astype(np.float32) / 65535.0
        return data

    def read_buffer_view_bytes(self, buffer_view_index: int) -> bytes:
        """Read raw bytes from a buffer view (for images)."""
        bv = self.gltf.buffer_views[buffer_view_index]
        buffer_data = self._resolve_buffer(bv.buffer)
        start = bv.byte_offset or 0
        return buffer_data[start : start + bv.byte_length]
