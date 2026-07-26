# KHR_environment_map

Exports the scene world's environment as a KTX2 cubemap plus l=2 spherical
harmonics for diffuse irradiance.

## Status of the upstream spec

`KHR_environment_map` is **not a released glTF extension**. It lives in
[KhronosGroup/glTF#1956](https://github.com/KhronosGroup/glTF/pull/1956), open
since March 2021 and still unmerged. Two things to know before relying on it:

**The PR contradicts itself.** The prose and example JSON call the extension
`KHR_environment_map`, with `cubemaps` / `environment_maps` arrays at the root
and an `environment_map` index on the scene. The schema files in the same PR
call it `KHR_lights_environment`, with `lights` and `light`. The PR directory
is named after the latter. This exporter follows the **prose**, since that is
what the extension is named and what any reader of the spec will implement
against. The README's own example JSON does not parse (missing comma after
`"source": 0`, trailing comma after `"cubemap": 0`), and neither do three of
the four schema files.

**The permitted format list is impractical.** The spec allows only
`R8G8B8_SRGB`, `R8G8B8_UNORM`, `E5B9G9R9_UFLOAT_PACK32`,
`B10G11R11_UFLOAT_PACK32`, `R16G16B16_SFLOAT` and `R16G16B16_UNORM` — no
block-compressed formats, and nothing with four channels. `VK_FORMAT_R8G8B8_SRGB`
is not a sampled-image format on most desktop drivers, so writing it would
produce files real engines cannot bind. This exporter emits RGBA8 / BC7 / UASTC
/ ETC1S instead, selected by **Cubemap Codec**.

## What gets exported

The world's Background node is traced back from the World Output, through any
Mapping or Texture Coordinate chain, to an **Environment Texture** node with an
`EQUIRECTANGULAR` projection. Nothing is emitted when the scene has no world,
the world has no such node, or the projection is something else.

```json
"extensions": {
  "KHR_environment_map": {
    "cubemaps": [ { "source": 0, "layer": 0 } ],
    "environment_maps": [ {
      "name": "Sky_72",
      "cubemap": 0,
      "irradianceCoefficients": [ [r,g,b], ... 9 entries ]
    } ]
  }
},
"scenes": [ { "extensions": { "KHR_environment_map": { "environment_map": 0 } } } ]
```

The Background node's **Strength** becomes `cubemaps[].intensity`, and is
omitted when it is 1.

## Cubemap

One KTX2 file with `faceCount: 6` and a full mip chain, referenced as an image
with mimeType `image/ktx2`. Faces are in KTX order — **+X, −X, +Y, −Y, +Z, −Z**
— in glTF's Y-up space, so the cubemap lines up with the exported geometry.

Face resolution comes from **Cubemap Size**; `AUTO` uses a quarter of the
equirect width rounded down to a power of two, which keeps texel density
roughly matched to the source.

The equirect is resampled per face with bilinear filtering and a wrapping
horizontal axis. Blender hands byte images back in their stored encoding, so
the face texels are the sRGB values the source image had and the KTX2 format is
an `-srgb` one.

## Irradiance coefficients

`irradianceCoefficients` is the 9×3 **radiance** projection L<sub>lm</sub>, not
pre-convolved irradiance. Clients apply the Lambertian convolution themselves:

```glsl
const float A0 = 3.141593, A1 = 2.094395, A2 = 0.785398;
// n is the world-space normal, L[] the 9 exported coefficients
vec3 irradianceOverPi(vec3 n) {
    return (A0 * 0.282095 * L[0]
          + A1 * 0.488603 * (n.y * L[1] + n.z * L[2] + n.x * L[3])
          + A2 * (1.092548 * n.x * n.y * L[4]
                + 1.092548 * n.y * n.z * L[5]
                + 0.315392 * (3.0 * n.z * n.z - 1.0) * L[6]
                + 1.092548 * n.x * n.z * L[7]
                + 0.546274 * (n.x * n.x - n.y * n.y) * L[8])) / PI;
}
```

The result multiplies albedo directly — it is the diffuse ambient term, already
divided by π. Coefficients are integrated from the **linear** equirect: an sRGB
source image is decoded first, without which they come out roughly twice too
large.

Accuracy is the usual l=2 truncation: reconstruction lands within about 1% of a
brute-force cosine-weighted integral for most normals, rising to ~4–5% straight
up and straight down on a sky with a strong vertical gradient.

## Requirements

Cubemap encoding needs `ktx_encode_cube` in the native KTX library
([tonis2/ktx.c3](https://github.com/tonis2/ktx.c3)). Libraries built before it
was added still load, and the exporter skips the extension with a console
message rather than failing the export.

## Not implemented

* **Import.** The importer does not read `KHR_environment_map` back into a
  Blender world; a round-trip loses the environment.
* `boundingBoxMin` / `boundingBoxMax` (localized cubemaps) and `ior` are never
  written, so reflections are treated as infinitely distant.
* `irradianceFactor` is not written; the coefficients are absolute.
* Pre-filtered roughness mips are not generated. The spec puts that on the
  client, and the mip chain in the file is a plain box-filtered one.
