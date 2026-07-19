from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import bpy
    from .texture import TextureExporter
    from ..exporter import ExportSettings


EXT_WALKABILITY = "CUSTOM_walkability_mask"

# Channel enum identifier -> glTF channel index (0=R 1=G 2=B 3=A)
_CHANNEL_INDEX = {"R": 0, "G": 1, "B": 2, "A": 3}


class WalkabilityExporter:
    """Emits the CUSTOM_walkability_mask node extension.

    A mesh object can carry an ``obj.gltf_walkability`` property group pointing at
    a mask image (white = walkable, black = blocked). The image is packed into the
    glTF like any other texture and referenced by image index, alongside the
    sampling parameters the engine needs (channel, threshold, sense, UV set).
    """

    def __init__(self, texture_exporter: "TextureExporter", settings: "ExportSettings") -> None:
        self.texture_exporter = texture_exporter
        self.settings = settings
        self.extensions_used: set[str] = set()

    def gather_node(self, obj: "bpy.types.Object") -> dict | None:
        """Build the walkability extension for a node. Returns a dict to merge
        into ``node.extensions`` or None when the object has no mask."""
        if not self.settings.export_walkability:
            return None

        props = getattr(obj, "gltf_walkability", None)
        if props is None or not props.enabled or props.mask is None:
            return None

        # Pack the mask image into images/buffer (deduped by image name, with the
        # Blender colorspace stamped into extras for a faithful round-trip).
        image_index = self.texture_exporter._gather_image(props.mask)

        self.extensions_used.add(EXT_WALKABILITY)
        return {
            EXT_WALKABILITY: {
                "image": image_index,
                "channel": _CHANNEL_INDEX.get(props.channel, 0),
                "threshold": round(float(props.threshold), 6),
                "walkableAbove": bool(props.walkable_above),
                "texCoord": 0,
            }
        }
