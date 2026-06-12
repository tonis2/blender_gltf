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
        """Read accessor data as a numpy array shaped (count, num_components)
        (scalar accessors are returned 1-D, shaped (count,))."""
        acc = self.gltf.accessors[accessor_index]
        component_type = ComponentType(acc.component_type)
        data_type = DataType(acc.type)
        dtype = component_type.numpy_dtype
        num_components = data_type.num_components

        if acc.buffer_view is None:
            # Legal in glTF: an accessor without a bufferView is zero-filled
            # (usually the base of a sparse accessor).
            result = np.zeros((acc.count, num_components), dtype=dtype)
        else:
            result = self._read_buffer_view_elements(
                acc.buffer_view, acc.byte_offset or 0,
                acc.count, num_components, dtype,
            )

        if acc.sparse is not None:
            result = self._apply_sparse(result, acc.sparse, num_components)

        if acc.normalized:
            result = self._dequantize(result, component_type)
        if num_components == 1:
            result = result.reshape(-1)
        return result

    def _read_buffer_view_elements(
        self,
        buffer_view_index: int,
        acc_offset: int,
        count: int,
        num_components: int,
        dtype: np.dtype,
    ) -> np.ndarray:
        """Read `count` elements from a buffer view as a writable (count,
        num_components) array, handling interleaved (strided) layouts."""
        bv = self.gltf.buffer_views[buffer_view_index]
        buffer_data = self._resolve_buffer(bv.buffer)
        start = (bv.byte_offset or 0) + acc_offset
        element_size = num_components * dtype.itemsize

        if bv.byte_stride and bv.byte_stride != element_size:
            # Strided / interleaved buffer view: read the whole span once,
            # then slice each element out of its stride window.
            stride = bv.byte_stride
            span = stride * (count - 1) + element_size
            raw = np.frombuffer(buffer_data, dtype=np.uint8, count=span, offset=start)
            # Pad to a full (count, stride) grid; the tail past the last
            # element is never part of any element.
            padded = np.zeros(count * stride, dtype=np.uint8)
            padded[:span] = raw
            elems = padded.reshape(count, stride)[:, :element_size]
            return np.ascontiguousarray(elems).view(dtype).reshape(count, num_components)

        # Tightly packed
        total = count * num_components
        data = np.frombuffer(buffer_data, dtype=dtype, count=total, offset=start)
        return data.reshape(count, num_components).copy()

    def _apply_sparse(
        self, base: np.ndarray, sparse, num_components: int,
    ) -> np.ndarray:
        """Scatter sparse accessor substitutions into the base data."""
        idx_info = sparse.indices or {}
        val_info = sparse.values or {}
        count = sparse.count or 0
        if count <= 0 or "bufferView" not in idx_info or "bufferView" not in val_info:
            return base

        idx_dtype = ComponentType(idx_info["componentType"]).numpy_dtype
        indices = self._read_buffer_view_elements(
            idx_info["bufferView"], idx_info.get("byteOffset") or 0,
            count, 1, idx_dtype,
        ).reshape(-1).astype(np.int64)

        values = self._read_buffer_view_elements(
            val_info["bufferView"], val_info.get("byteOffset") or 0,
            count, num_components, base.dtype,
        )

        # `base` is always a private writable copy (zeros or a fresh read).
        base[indices] = values
        return base

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
        if component_type == ComponentType.UNSIGNED_INT:
            return data.astype(np.float32) / 4294967295.0
        if component_type == ComponentType.SIGNED_INT:
            return np.maximum(data.astype(np.float32) / 2147483647.0, -1.0)
        # DOUBLE/HALF_FLOAT are already floating point; INT64/UINT64 have no
        # defined normalized form here — pass them through unchanged.
        return data

    def read_buffer_view_bytes(self, buffer_view_index: int) -> bytes:
        """Read raw bytes from a buffer view (for images)."""
        bv = self.gltf.buffer_views[buffer_view_index]
        buffer_data = self._resolve_buffer(bv.buffer)
        start = bv.byte_offset or 0
        return buffer_data[start : start + bv.byte_length]
