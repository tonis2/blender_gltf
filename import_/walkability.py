from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import bpy
    from ..gltf.types import Gltf
    from .texture import TextureImporter
    from ..importer import ImportSettings


EXT_WALKABILITY = "CUSTOM_walkability_mask"

# glTF channel index (0=R 1=G 2=B 3=A) -> channel enum identifier
_CHANNEL_ENUM = {0: "R", 1: "G", 2: "B", 3: "A"}


class WalkabilityImporter:
    """Reconstructs ``obj.gltf_walkability`` from the CUSTOM_walkability_mask
    node extension. Run as a post-pass after the scene hierarchy is built, since
    it needs the node-index -> Blender object mapping."""

    def __init__(
        self,
        gltf: "Gltf",
        texture_importer: "TextureImporter",
        settings: "ImportSettings",
    ) -> None:
        self.gltf = gltf
        self.texture_importer = texture_importer
        self.settings = settings

    def apply(self, node_to_blender: dict[int, "bpy.types.Object"]) -> None:
        if self.gltf.nodes is None:
            return

        for node_index, obj in node_to_blender.items():
            if obj is None or node_index >= len(self.gltf.nodes):
                continue
            node = self.gltf.nodes[node_index]
            ext = node.extensions.get(EXT_WALKABILITY) if node.extensions else None
            if not ext:
                continue

            image_index = ext.get("image")
            if image_index is None:
                continue

            # Images are normally loaded alongside materials; if material import
            # was disabled they won't exist yet, so load them on demand.
            if not self.texture_importer.blender_images and self.gltf.images:
                self.texture_importer.import_all()
            img = self.texture_importer.get_blender_image(image_index)

            props = getattr(obj, "gltf_walkability", None)
            if props is None:
                continue
            props.enabled = True
            props.mask = img
            props.threshold = float(ext.get("threshold", 0.5))
            props.channel = _CHANNEL_ENUM.get(int(ext.get("channel", 0)), "R")
            props.walkable_above = bool(ext.get("walkableAbove", True))
