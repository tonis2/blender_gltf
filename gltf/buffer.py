from __future__ import annotations

import numpy as np

from .constants import ComponentType, DataType, BufferViewTarget
from .types import Accessor, BufferView, Buffer


class BufferBuilder:
    """Accumulates binary data and produces glTF Accessors and BufferViews."""

    def __init__(self) -> None:
        self._data = bytearray()
        self._accessors: list[Accessor] = []
        self._buffer_views: list[BufferView] = []

    def _pad_to_alignment(self, alignment: int = 4) -> None:
        remainder = len(self._data) % alignment
        if remainder:
            self._data.extend(b"\x00" * (alignment - remainder))

    def add_accessor(
        self,
        data: np.ndarray,
        component_type: ComponentType,
        data_type: DataType,
        target: BufferViewTarget | None = None,
        include_bounds: bool = False,
    ) -> int:
        """Append data as a new BufferView + Accessor. Returns the accessor index."""
        data = data.astype(component_type.numpy_dtype)
        raw = data.tobytes()

        self._pad_to_alignment()
        byte_offset = len(self._data)
        self._data.extend(raw)

        # Create BufferView
        bv_index = len(self._buffer_views)
        self._buffer_views.append(
            BufferView(
                buffer=0,
                byte_length=len(raw),
                byte_offset=byte_offset,
                target=target.value if target else None,
            )
        )

        # Compute min/max if requested
        accessor_min = None
        accessor_max = None
        if include_bounds:
            num_components = data_type.num_components
            reshaped = data.reshape(-1, num_components)
            accessor_min = reshaped.min(axis=0).tolist()
            accessor_max = reshaped.max(axis=0).tolist()

        # Create Accessor
        num_elements = len(data.flat) // data_type.num_components
        acc_index = len(self._accessors)
        self._accessors.append(
            Accessor(
                buffer_view=bv_index,
                component_type=component_type.value,
                count=num_elements,
                type=data_type.value,
                min=accessor_min,
                max=accessor_max,
            )
        )
        return acc_index

    def add_image_data(self, data: bytes) -> int:
        """Append raw image bytes as a BufferView (no accessor). Returns bufferView index."""
        self._pad_to_alignment()
        byte_offset = len(self._data)
        self._data.extend(data)

        bv_index = len(self._buffer_views)
        self._buffer_views.append(
            BufferView(
                buffer=0,
                byte_length=len(data),
                byte_offset=byte_offset,
            )
        )
        return bv_index

    def finalize(self) -> tuple[list[Accessor], list[BufferView], Buffer | None, bytes]:
        """Return all accessors, buffer views, the buffer descriptor, and the raw binary blob."""
        if not self._data:
            return self._accessors, self._buffer_views, None, b""

        self._pad_to_alignment()
        binary = bytes(self._data)
        buffer = Buffer(byte_length=len(binary))
        return self._accessors, self._buffer_views, buffer, binary
