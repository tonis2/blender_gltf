from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any


def _to_camel_case(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _serialize(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    return value


@dataclass
class GltfBase:
    def to_dict(self) -> dict:
        result = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if value is None:
                continue
            if isinstance(value, list) and len(value) == 0:
                continue
            key = _to_camel_case(f.name)
            result[key] = _serialize(value)
        return result


# --- Core types ---


@dataclass
class Asset(GltfBase):
    version: str = "2.0"
    generator: str | None = None
    copyright: str | None = None
    min_version: str | None = None
    extensions: dict | None = None
    extras: Any | None = None


@dataclass
class AccessorSparse(GltfBase):
    count: int = 0
    indices: dict | None = None
    values: dict | None = None


@dataclass
class Accessor(GltfBase):
    component_type: int = 0
    count: int = 0
    type: str = ""
    buffer_view: int | None = None
    byte_offset: int | None = None
    max: list[float] | None = None
    min: list[float] | None = None
    name: str | None = None
    normalized: bool | None = None
    sparse: AccessorSparse | None = None
    extensions: dict | None = None
    extras: Any | None = None


@dataclass
class BufferView(GltfBase):
    buffer: int = 0
    byte_length: int = 0
    byte_offset: int | None = None
    byte_stride: int | None = None
    target: int | None = None
    name: str | None = None
    extensions: dict | None = None
    extras: Any | None = None


@dataclass
class Buffer(GltfBase):
    byte_length: int = 0
    uri: str | None = None
    name: str | None = None
    extensions: dict | None = None
    extras: Any | None = None


@dataclass
class MeshPrimitive(GltfBase):
    attributes: dict[str, int] = field(default_factory=dict)
    indices: int | None = None
    material: int | None = None
    mode: int | None = None
    targets: list[dict[str, int]] | None = None
    extensions: dict | None = None
    extras: Any | None = None


@dataclass
class Mesh(GltfBase):
    primitives: list[MeshPrimitive] = field(default_factory=list)
    name: str | None = None
    weights: list[float] | None = None
    extensions: dict | None = None
    extras: Any | None = None


@dataclass
class Node(GltfBase):
    name: str | None = None
    children: list[int] | None = None
    mesh: int | None = None
    camera: int | None = None
    skin: int | None = None
    matrix: list[float] | None = None
    translation: list[float] | None = None
    rotation: list[float] | None = None
    scale: list[float] | None = None
    weights: list[float] | None = None
    extensions: dict | None = None
    extras: Any | None = None


@dataclass
class Scene(GltfBase):
    name: str | None = None
    nodes: list[int] | None = None
    extensions: dict | None = None
    extras: Any | None = None


@dataclass
class TextureInfo(GltfBase):
    index: int = 0
    tex_coord: int | None = None
    extensions: dict | None = None
    extras: Any | None = None


@dataclass
class NormalTextureInfo(GltfBase):
    index: int = 0
    tex_coord: int | None = None
    scale: float | None = None
    extensions: dict | None = None
    extras: Any | None = None


@dataclass
class OcclusionTextureInfo(GltfBase):
    index: int = 0
    tex_coord: int | None = None
    strength: float | None = None
    extensions: dict | None = None
    extras: Any | None = None


@dataclass
class MaterialPBRMetallicRoughness(GltfBase):
    base_color_factor: list[float] | None = None
    base_color_texture: TextureInfo | None = None
    metallic_factor: float | None = None
    roughness_factor: float | None = None
    metallic_roughness_texture: TextureInfo | None = None
    extensions: dict | None = None
    extras: Any | None = None


@dataclass
class Material(GltfBase):
    name: str | None = None
    pbr_metallic_roughness: MaterialPBRMetallicRoughness | None = None
    normal_texture: NormalTextureInfo | None = None
    occlusion_texture: OcclusionTextureInfo | None = None
    emissive_texture: TextureInfo | None = None
    emissive_factor: list[float] | None = None
    alpha_mode: str | None = None
    alpha_cutoff: float | None = None
    double_sided: bool | None = None
    extensions: dict | None = None
    extras: Any | None = None


@dataclass
class Sampler(GltfBase):
    mag_filter: int | None = None
    min_filter: int | None = None
    wrap_s: int | None = None
    wrap_t: int | None = None
    name: str | None = None
    extensions: dict | None = None
    extras: Any | None = None


@dataclass
class Image(GltfBase):
    uri: str | None = None
    mime_type: str | None = None
    buffer_view: int | None = None
    name: str | None = None
    extensions: dict | None = None
    extras: Any | None = None


@dataclass
class Texture(GltfBase):
    sampler: int | None = None
    source: int | None = None
    name: str | None = None
    extensions: dict | None = None
    extras: Any | None = None


@dataclass
class AnimationChannelTarget(GltfBase):
    path: str = ""
    node: int | None = None
    extensions: dict | None = None
    extras: Any | None = None


@dataclass
class AnimationChannel(GltfBase):
    sampler: int = 0
    target: AnimationChannelTarget = field(default_factory=AnimationChannelTarget)


@dataclass
class AnimationSampler(GltfBase):
    input: int = 0
    output: int = 0
    interpolation: str | None = None


@dataclass
class Animation(GltfBase):
    channels: list[AnimationChannel] = field(default_factory=list)
    samplers: list[AnimationSampler] = field(default_factory=list)
    name: str | None = None
    extensions: dict | None = None
    extras: Any | None = None


@dataclass
class Gltf(GltfBase):
    asset: Asset = field(default_factory=Asset)
    scene: int | None = None
    scenes: list[Scene] | None = None
    nodes: list[Node] | None = None
    meshes: list[Mesh] | None = None
    accessors: list[Accessor] | None = None
    buffer_views: list[BufferView] | None = None
    buffers: list[Buffer] | None = None
    materials: list[Material] | None = None
    textures: list[Texture] | None = None
    images: list[Image] | None = None
    samplers: list[Sampler] | None = None
    cameras: list | None = None
    animations: list[Animation] | None = None
    skins: list | None = None
    extensions: dict | None = None
    extensions_used: list[str] | None = None
    extensions_required: list[str] | None = None
    extras: Any | None = None
