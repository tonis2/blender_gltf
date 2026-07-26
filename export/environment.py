"""KHR_environment_map — the scene's world environment as a KTX2 cubemap.

Blender holds the environment as an equirectangular image on the world's
Background shader; glTF wants a cubemap. This module resamples the equirect
into six faces, projects the l=2 spherical harmonics for diffuse irradiance,
and emits both through the extension.

Spec: KhronosGroup/glTF PR #1956 (Draft). Two cautions about that document:

* It is self-inconsistent. The prose and example JSON name the extension
  ``KHR_environment_map`` with ``cubemaps`` / ``environment_maps`` arrays and a
  scene-level ``environment_map`` index, while the schema files in the same PR
  say ``lights`` / ``light``. This follows the prose, which is what the
  extension is actually called.
* Its permitted format list (R8G8B8_SRGB, E5B9G9R9, B10G11R11, R16G16B16_*)
  omits every block-compressed format and every 4-channel one. We write what
  the KTX pipeline and real GPUs handle instead — RGBA8/BC7/UASTC — since
  VK_FORMAT_R8G8B8_SRGB is not a sampled-image format on most desktop drivers.

Cube faces are emitted in KTX order (+X, -X, +Y, -Y, +Z, -Z) in glTF's
Y-up space, so the exported cubemap lines up with the exported geometry.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import bpy
    from .texture import TextureExporter
    from ..exporter import ExportSettings

EXT_ENVIRONMENT_MAP = "KHR_environment_map"

# Irradiance is reconstructed by the client as
#     E(n)/PI = sum_l sum_m  A_l * L_lm * Y_lm(n)
# where L_lm are the coefficients written to irradianceCoefficients and A_l are
# the Lambertian convolution constants below (Ramamoorthi & Hanrahan 2001).
# Storing the unconvolved radiance projection is what the Khronos IBL tooling
# does; see extensions/environment_map.md for the shader-side evaluation.
SH_A = (3.141593, 2.094395, 0.785398)  # A_0, A_1, A_2


class EnvironmentExporter:
    """Emits the KHR_environment_map root + scene extensions."""

    def __init__(self, texture_exporter: "TextureExporter",
                 settings: "ExportSettings") -> None:
        self.texture_exporter = texture_exporter
        self.settings = settings
        self.extensions_used: set[str] = set()
        self.cubemaps: list[dict] = []
        self.environment_maps: list[dict] = []
        # Blender world name -> environment_maps index, so several scenes
        # sharing a world share one cubemap instead of re-encoding it.
        self._cache: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Gathering
    # ------------------------------------------------------------------
    def gather_scene(self, scene: "bpy.types.Scene") -> dict | None:
        """Build the scene extension for `scene`, or None if it has no usable
        world environment (no world, no equirect image, or export disabled)."""
        if not self.settings.export_environment_map:
            return None
        world = getattr(scene, "world", None)
        if world is None or not world.use_nodes or world.node_tree is None:
            return None

        if world.name in self._cache:
            index = self._cache[world.name]
        else:
            index = self._build(world)
            if index is None:
                return None
            self._cache[world.name] = index

        self.extensions_used.add(EXT_ENVIRONMENT_MAP)
        return {EXT_ENVIRONMENT_MAP: {"environment_map": index}}

    def get_root_extension(self) -> dict | None:
        if not self.environment_maps:
            return None
        return {
            EXT_ENVIRONMENT_MAP: {
                "cubemaps": self.cubemaps,
                "environment_maps": self.environment_maps,
            }
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _build(self, world: "bpy.types.World") -> int | None:
        found = _find_environment(world)
        if found is None:
            return None
        image, strength = found

        try:
            equirect, linear = _read_equirect(image)
        except Exception as e:
            print(f"[glTF export] world '{world.name}': cannot read "
                  f"environment image '{image.name}': {e}")
            return None

        size = _face_size(equirect.shape[1], self.settings.environment_map_size)
        faces = _equirect_to_cube(equirect, size)
        coefficients = _irradiance_sh(linear)

        blob = self._encode(faces, size, image)
        if blob is None:
            return None

        source = self.texture_exporter.add_ktx_blob(
            blob, f"{world.name}_cubemap")

        cubemap = {"source": source, "layer": 0}
        # Background Strength is a plain multiplier on the environment, which
        # is exactly what the cubemap `intensity` factor means.
        if abs(strength - 1.0) > 1e-6:
            cubemap["intensity"] = round(float(strength), 6)
        self.cubemaps.append(cubemap)

        self.environment_maps.append({
            "name": world.name,
            "cubemap": len(self.cubemaps) - 1,
            "irradianceCoefficients": coefficients,
        })
        return len(self.environment_maps) - 1

    def _encode(self, faces: "list[bytes]", size: int,
                image: "bpy.types.Image") -> bytes | None:
        from .. import ktx_lib

        if not ktx_lib.is_cube_available():
            print("[glTF export] KHR_environment_map skipped: the installed "
                  "ktx library cannot encode cubemaps "
                  f"({ktx_lib.load_error() or 'no ktx_encode_cube export'})")
            return None
        cs = getattr(image.colorspace_settings, "name", "sRGB")
        codec = self.settings.environment_map_codec or "rgba8"
        fmt = codec if cs == "sRGB" else codec + "-linear"
        if codec in ("rgba8", "bc7") and cs == "sRGB":
            fmt = codec + "-srgb"
        try:
            return ktx_lib.encode_cube(
                faces, size, fmt, mipmaps=True,
                quality=self.settings.ktx_quality,
                effort=self.settings.ktx_effort)
        except Exception as e:
            print(f"[glTF export] environment cubemap encode failed: {e}")
            return None


# ----------------------------------------------------------------------
# World inspection
# ----------------------------------------------------------------------
def _find_environment(world: "bpy.types.World"):
    """(image, strength) from the world's Background, or None.

    Walks back from the World Output so a Mapping/Texture Coordinate chain in
    front of the environment texture doesn't hide it.
    """
    nt = world.node_tree
    output = next((n for n in nt.nodes
                   if n.type == "OUTPUT_WORLD" and n.is_active_output), None)
    if output is None:
        output = next((n for n in nt.nodes if n.type == "OUTPUT_WORLD"), None)
    if output is None:
        return None

    background = _upstream(output.inputs.get("Surface"), {"BACKGROUND"})
    if background is None:
        return None
    strength_input = background.inputs.get("Strength")
    strength = (1.0 if strength_input is None or strength_input.is_linked
                else float(strength_input.default_value))

    env = _upstream(background.inputs.get("Color"), {"TEX_ENVIRONMENT"})
    if env is None or env.image is None:
        return None
    if getattr(env, "projection", "EQUIRECTANGULAR") != "EQUIRECTANGULAR":
        print(f"[glTF export] world '{world.name}': environment texture uses "
              f"{env.projection} projection; only EQUIRECTANGULAR is supported")
        return None
    return env.image, strength


def _upstream(socket, types: set, depth: int = 0):
    """First node of one of `types` reachable backwards from `socket`."""
    if socket is None or not socket.is_linked or depth > 8:
        return None
    node = socket.links[0].from_node
    if node.type in types:
        return node
    for inp in node.inputs:
        found = _upstream(inp, types, depth + 1)
        if found is not None:
            return found
    return None


def _read_equirect(image: "bpy.types.Image"):
    """(rgba8 top-down HxWx4 uint8, linear float HxWx3) for an equirect image.

    Blender hands back byte images in their stored encoding, so an sRGB image
    needs decoding before it can be integrated as radiance — skipping that
    inflates the SH coefficients by roughly a factor of two.
    """
    import numpy as np

    w, h = image.size
    if not w or not h:
        raise ValueError("image has no pixel data")
    buf = np.empty(w * h * 4, dtype=np.float32)
    image.pixels.foreach_get(buf)
    px = buf.reshape(h, w, 4)[::-1]          # Blender rows are bottom-up

    rgba = np.clip(px * 255.0 + 0.5, 0.0, 255.0).astype(np.uint8)
    rgb = px[..., :3]
    if getattr(image.colorspace_settings, "name", "sRGB") == "sRGB":
        linear = np.where(rgb <= 0.04045, rgb / 12.92,
                          np.power((np.maximum(rgb, 0.0) + 0.055) / 1.055, 2.4))
    else:
        linear = np.maximum(rgb, 0.0)
    return rgba, linear.astype(np.float32)


def _face_size(equirect_width: int, requested: str) -> int:
    """Face resolution: the requested override, else equirect_width/4 rounded
    down to a power of two (that keeps texel density roughly matched)."""
    if requested and requested != "AUTO":
        return int(requested)
    natural = max(equirect_width // 4, 16)
    size = 1
    while size * 2 <= natural:
        size *= 2
    return min(size, 2048)


# ----------------------------------------------------------------------
# Equirect -> cube
# ----------------------------------------------------------------------
def _equirect_to_cube(equirect, size: int) -> "list[bytes]":
    """Resample a top-down equirect into six top-down RGBA8 faces.

    Directions use glTF's Y-up space and the standard cubemap face
    parameterisation, so face f's texel (s,t) looks along the same ray a GPU
    would sample. Bilinear, with the horizontal axis wrapping.
    """
    import numpy as np

    h, w = equirect.shape[:2]
    a = (np.arange(size) + 0.5) / size * 2.0 - 1.0      # -1..1 across the face
    uc, vc = np.meshgrid(a, a)
    ones = np.ones_like(uc)
    # (+X, -X, +Y, -Y, +Z, -Z) — inverse of the GL lookup this module documents.
    dirs = [
        (ones, -vc, -uc),
        (-ones, -vc, uc),
        (uc, ones, vc),
        (uc, -ones, -vc),
        (uc, -vc, ones),
        (-uc, -vc, -ones),
    ]

    faces = []
    for dx, dy, dz in dirs:
        norm = np.sqrt(dx * dx + dy * dy + dz * dz)
        x, y, z = dx / norm, dy / norm, dz / norm
        # Match _read_equirect's top-down rows: row 0 is +Y. Blender's
        # equirect lookup is u -> atan2(-z, -x), so both negations are needed;
        # dropping the one on z mirrors the world east-to-west.
        u = (np.arctan2(-z, -x) / (2 * np.pi) + 0.5) * w - 0.5
        v = (np.arccos(np.clip(y, -1.0, 1.0)) / np.pi) * h - 0.5

        x0 = np.floor(u).astype(np.int64)
        y0 = np.clip(np.floor(v).astype(np.int64), 0, h - 1)
        fx = (u - x0)[..., None].astype(np.float32)
        fy = (v - y0)[..., None].astype(np.float32)
        x1, y1 = (x0 + 1) % w, np.clip(y0 + 1, 0, h - 1)
        x0 %= w

        src = equirect.astype(np.float32)
        top = src[y0, x0] * (1 - fx) + src[y0, x1] * fx
        bot = src[y1, x0] * (1 - fx) + src[y1, x1] * fx
        faces.append(np.clip(top * (1 - fy) + bot * fy + 0.5, 0, 255)
                     .astype(np.uint8).tobytes())
    return faces


# ----------------------------------------------------------------------
# Spherical harmonics
# ----------------------------------------------------------------------
def _irradiance_sh(linear) -> "list[list[float]]":
    """Project a linear top-down equirect onto the l=2 SH basis (9x3).

    Returns the radiance projection L_lm; clients convolve with SH_A to get
    irradiance. Row 0 of `linear` is +Y, matching _read_equirect.
    """
    import numpy as np

    h, w = linear.shape[:2]
    theta = (np.arange(h) + 0.5) / h * np.pi          # 0 at +Y
    phi = (np.arange(w) + 0.5) / w * 2 * np.pi - np.pi
    st, ct = np.sin(theta), np.cos(theta)
    # Inverse of the mapping in _equirect_to_cube: u -> atan2(-z, -x). This
    # must stay in step with it, or the l=1 band points the light the wrong way.
    x = -st[:, None] * np.cos(phi)[None, :]
    z = -st[:, None] * np.sin(phi)[None, :]
    y = np.broadcast_to(ct[:, None], (h, w))
    dw = (st * (np.pi / h) * (2 * np.pi / w))[:, None]

    c0 = 0.282095
    c1 = 0.488603
    c2, c3, c4 = 1.092548, 0.315392, 0.546274
    basis = [
        np.full((h, w), c0),
        c1 * y, c1 * z, c1 * x,
        c2 * x * y, c2 * y * z,
        c3 * (3.0 * z * z - 1.0),
        c2 * x * z,
        c4 * (x * x - y * y),
    ]
    return [[round(float((linear[..., c] * (b * dw)).sum()), 6)
             for c in range(3)] for b in basis]
