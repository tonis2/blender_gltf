# CUSTOM_materials_layers

A custom glTF extension that adds an ordered stack of additional material layers blended on top of the base material. Each layer carries its own PBR textures and is masked by either a texture channel or a vertex color attribute.

The base material is unchanged — viewers that don't understand the extension fall back to rendering the base material correctly. The extension carries the *extra* layers; the base layer is whatever sits in `pbrMetallicRoughness` / `normalTexture` on the material itself.

## Use cases

- Terrain blending: grass base + gravel/dirt/snow layered on top via splat maps or vertex paint
- Surface weathering: clean wall + dirt/rust layered on top
- Decals baked into a single material instead of overlapping geometry

## Extension placement

The extension is a **material-level** extension.

```json
{
  "materials": [
    {
      "name": "Ground",
      "pbrMetallicRoughness": {
        "baseColorTexture": { "index": 0 }
      },
      "normalTexture": { "index": 1 },
      "extensions": {
        "CUSTOM_materials_layers": {
          "layers": [ ... ]
        }
      }
    }
  ],
  "extensionsUsed": ["CUSTOM_materials_layers"]
}
```

## Schema

### Extension object

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `layers` | array of layer | No* | Ordered list of layers, applied bottom-to-top over the base material |
| `base` | object | No* | Extra base-material data that has no core glTF slot — currently `heightTexture` and `bump` (see [base object](#base-object)) |

\* At least one of `layers` or `base` must be present. A material may carry only `base` (e.g. a plain material whose normal comes from a Blender Bump node with a displacement map, and no extra layers) — it is then not a "layered" material, just one whose base height/bump is preserved.

The base material is layer 0 conceptually — the first entry in `layers` is layer 1, blended over it, the next is layer 2 over that, etc.

### Layer object

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `name` | string | No | Identifier for tooling |
| `pbrMetallicRoughness` | object | No | PBR inputs for this layer (same shape as glTF's `pbrMetallicRoughness`). Per-layer alpha lives in `baseColorFactor[3]` |
| `normalTexture` | normalTextureInfo | No | Per-layer tangent-space normal map |
| `heightTexture` | textureInfo | No | Per-layer height / displacement (bump) map. Grayscale; drives a bump perturbation combined with `normalTexture`. See [height & bump](#height--bump) |
| `bump` | object | No | Bump parameters for `heightTexture` — `{ "strength": number (default 1), "distance": number (default 1) }` |
| `emissiveFactor` | array of 3 numbers | No | Per-layer emissive color × strength (default `[0,0,0]`) |
| `emissiveTexture` | textureInfo | No | Per-layer emissive map |
| `subsurface` | object | No | Per-layer subsurface — `{ "weight": number (default 0), "radius": [r,g,b] (default [1,0.2,0.1]) }` |
| `clearcoat` | object | No | Per-layer clearcoat, same shape as [`KHR_materials_clearcoat`](#clearcoat--sheen) |
| `sheen` | object | No | Per-layer sheen, same shape as [`KHR_materials_sheen`](#clearcoat--sheen) |
| `mask` | object | No | Where this layer is visible. If omitted, the layer is fully visible (mask = `1.0` everywhere) |
| `blendMode` | string | No | How to blend with the layer below. Default `"MIX"` |
| `opacity` | number | No | Scalar layer opacity in `[0,1]`, multiplied into the mask (default `1.0`) |
| `enabled` | boolean | No | If `false`, the layer is skipped entirely (default `true`) |

A layer with no `pbrMetallicRoughness`, `normalTexture`, `emissiveFactor`, `clearcoat` or `sheen` and a full mask is a no-op.

#### pbrMetallicRoughness

Same shape as the [glTF 2.0 pbrMetallicRoughness](https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html#reference-material-pbrmetallicroughness) object. All fields optional.

| Property | Type | Description |
|----------|------|-------------|
| `baseColorFactor` | array of 4 numbers | RGBA tint (default `[1,1,1,1]`) |
| `baseColorTexture` | textureInfo | Albedo texture |
| `metallicFactor` | number | (default `1.0`) |
| `roughnessFactor` | number | (default `1.0`) |
| `metallicRoughnessTexture` | textureInfo | Combined MR texture (G=roughness, B=metallic, per glTF convention) |

**A default is not a statement.** There is no way to spell "this layer leaves the
surface alone", so a layer whose `metallicFactor` and `roughnessFactor` are both
absent (or both `1.0`) and which carries no `metallicRoughnessTexture` MUST be
read as saying nothing about metal or roughness — the base material's values pass
through it unchanged. A layer that genuinely means fully rough and fully metallic
states it with a map, the same way a layer that means white paint does.

Without that rule a colour-only layer is unexpressible: every layer would carry a
metal and a roughness whether it meant to or not, and a stack of tints would flatten
the base material's roughness map everywhere its masks reached.

**The pair travels together.** Writing one factor leaves the other at its `1.0`
default, so a dirt layer stating `roughnessFactor` alone arrives *fully metallic*.
Writers MUST emit both whenever they emit either.

`KHR_texture_transform` is supported on each `textureInfo` and is the recommended way to encode per-layer UV tiling (e.g., gravel tiles 4× while grass tiles 1×).

### clearcoat & sheen

A layer's `clearcoat` and `sheen` objects take the field names and meanings of
[`KHR_materials_clearcoat`](https://github.com/KhronosGroup/glTF/tree/main/extensions/2.0/Khronos/KHR_materials_clearcoat)
and [`KHR_materials_sheen`](https://github.com/KhronosGroup/glTF/tree/main/extensions/2.0/Khronos/KHR_materials_sheen)
unchanged — a layer's coat is the material's coat with a mask on it, so it is
described the same way.

| `clearcoat` property | Type | Description |
|----------------------|------|-------------|
| `clearcoatFactor` | number | Coat strength |
| `clearcoatTexture` | textureInfo | Coat strength map (R) |
| `clearcoatRoughnessFactor` | number | Coat roughness |
| `clearcoatRoughnessTexture` | textureInfo | Coat roughness map (G) |
| `clearcoatNormalTexture` | normalTextureInfo | Coat normal map |

| `sheen` property | Type | Description |
|------------------|------|-------------|
| `sheenColorFactor` | array of 3 numbers | Sheen color |
| `sheenColorTexture` | textureInfo | Sheen color map |
| `sheenRoughnessFactor` | number | Sheen roughness |
| `sheenRoughnessTexture` | textureInfo | Sheen roughness map (A) |

**The base material's coat and sheen are not here.** They live in the material's
own `KHR_materials_clearcoat` / `KHR_materials_sheen`, like every other layer-0
value that core glTF or a Khronos extension can already hold — so a viewer that
ignores this extension still renders the base coat correctly. `layers[i]`
carries only the coat of layer *i+1*.

**A default is not a statement, here too.** Absent fields are unstated: whatever
the layers below arrived with passes through. Present fields blend by the mask
exactly as the pbr fields do. The consequence worth stating: a layer that means
*remove* the coat — mud over a lacquered surface — MUST write
`"clearcoatFactor": 0`, not omit it. Omitting it says "I have no opinion about
the coat" and lets the coat underneath through untouched, which is the opposite.
(In Blender, wire a Value node of `0.0` into the layer's Coat Weight: a socket
left at its `0.0` default reads as untouched, a linked one always reads as
stated. Same for Sheen Weight.)

### height & bump

glTF core has no height/displacement texture slot, so layers carry an optional `heightTexture` plus a `bump` object. This is a grayscale bump map: it perturbs the surface normal (the same role as Blender's Bump node), and combines with `normalTexture` when both are present — the normal map is the bump node's "Normal" input, the height map its "Height" input.

| `bump` property | Type | Default | Description |
|-----------------|------|---------|-------------|
| `strength` | number | `1.0` | Bump intensity (0 disables) |
| `distance` | number | `1.0` | Height-to-displacement scale (maps to Blender Bump `Distance`) |

Viewers that don't implement height/parallax can ignore `heightTexture`/`bump` and use `normalTexture` alone; the result is still correct, just without the extra bump detail.

### base object

`extensions.CUSTOM_materials_layers.base` carries layer-0 data that the core material can't hold. The base material's color/MR/normal/emission live in the standard `pbrMetallicRoughness` / `normalTexture` / `emissiveFactor` fields as usual; only the extras go here.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `name` | string | No | Authoring label for layer 0 (the base), matching a layer's `name` |
| `heightTexture` | textureInfo | No | Base-material height / displacement (bump) map (same meaning as a layer's `heightTexture`) |
| `bump` | object | No | Base-material bump parameters — `{ "strength", "distance" }` |

### mask object

Defines the per-pixel weight `m ∈ [0,1]` used to blend this layer over what's below.

The `mask` object itself is optional on a layer — omit it for a layer that is visible everywhere (the common "full-coverage paint pass" case). When present:

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `source` | string | Yes | `"TEXTURE"` or `"VERTEX_COLOR"` |
| `channel` | string | No | One of `"R"`, `"G"`, `"B"`, `"A"`. Default `"R"` |
| `texture` | textureInfo | If `source = "TEXTURE"` | Mask texture |
| `attribute` | string | No | Which glTF vertex color attribute to read when `source = "VERTEX_COLOR"` — `"COLOR_0"`, `"COLOR_1"`, … Default `"COLOR_0"` |
| `invert` | boolean | No | If true, use `1 - m`. Default `false` |

**Channel packing.** A single 4-channel mask texture can drive up to four layers — layer A reads `R`, layer B reads `G`, etc. This is the standard splat-map technique.

**A `VERTEX_COLOR` mask on a mesh that does not carry the named attribute reads `m = 0`, not `1`** — the layer is off there rather than covering the whole surface. This is what Blender's Color Attribute node answers for an attribute that is not on the mesh, and it is what lets one material be shared between a painted mesh and an unpainted one, which is the normal case: the material is authored once and assigned to everything made of that stuff, and only some of it gets painted. It is deliberately the opposite of core glTF's rule for a *missing* `COLOR_0`, which is white, because that rule is about a tint and this is a weight.

**A masked material's `COLOR_0` is not a tint.** Core glTF says `COLOR_0` multiplies the base color; when a material carries this extension, the attribute its layers read is a set of per-layer weights and multiplying it into the base color draws the mask instead of the texture. A consumer must skip that multiply for any mesh whose material carries `CUSTOM_materials_layers` — including a stack whose `layers` array is empty, since the paint is on the mesh either way.

### blendMode

How the masked layer is composited over the layer below. `m` is the mask value, `c_below` is the surface color from layers beneath, `c_layer` is this layer's color.

`m` already includes the layer's `opacity` (`m = mask × opacity`). Let `f(a, b)` be the per-mode blend of the below color `a` and layer color `b`; the result is always `lerp(c_below, f(c_below, c_layer), m)`.

| Value | `f(a, b)` | Notes |
|-------|-----------|-------|
| `"MIX"` | `b` | Default. For normals, use reoriented-normal blending — see implementation notes |
| `"MULTIPLY"` | `a * b` | Darkening passes (dirt, AO decals) |
| `"ADD"` | `a + b` | Emissive accents, light-leak decals |
| `"SUBTRACT"` | `a - b` | |
| `"SCREEN"` | `1 - (1 - a)(1 - b)` | Lightening |
| `"OVERLAY"` | `a < 0.5 ? 2ab : 1 - 2(1 - a)(1 - b)` | Contrast |
| `"SOFT_LIGHT"` | soft-light combine of `a`, `b` | Gentle contrast |
| `"DIFFERENCE"` | `abs(a - b)` | |
| `"DARKEN"` | `min(a, b)` | |
| `"LIGHTEN"` | `max(a, b)` | |

These match Blender's `ShaderNodeMix` `blend_type` values 1:1. Implementations MAY support a subset; if a `blendMode` is unrecognized, fall back to `"MIX"`.

## Full example

Grass base, gravel layer masked by a splat texture's R channel, with the gravel tiled 4×:

```json
{
  "materials": [
    {
      "name": "Ground",
      "pbrMetallicRoughness": {
        "baseColorTexture": { "index": 0 },
        "metallicRoughnessTexture": { "index": 1 },
        "roughnessFactor": 0.9
      },
      "normalTexture": { "index": 2 },
      "extensions": {
        "CUSTOM_materials_layers": {
          "base": {
            "heightTexture": { "index": 7 },
            "bump": { "strength": 1.0, "distance": 0.05 }
          },
          "layers": [
            {
              "name": "Gravel",
              "pbrMetallicRoughness": {
                "baseColorTexture": {
                  "index": 3,
                  "extensions": {
                    "KHR_texture_transform": { "scale": [4.0, 4.0] }
                  }
                },
                "metallicRoughnessTexture": {
                  "index": 4,
                  "extensions": {
                    "KHR_texture_transform": { "scale": [4.0, 4.0] }
                  }
                }
              },
              "normalTexture": {
                "index": 5,
                "extensions": {
                  "KHR_texture_transform": { "scale": [4.0, 4.0] }
                }
              },
              "heightTexture": {
                "index": 8,
                "extensions": {
                  "KHR_texture_transform": { "scale": [4.0, 4.0] }
                }
              },
              "bump": { "strength": 1.0, "distance": 0.1 },
              "mask": {
                "source": "TEXTURE",
                "texture": { "index": 6 },
                "channel": "R"
              },
              "blendMode": "MIX"
            }
          ]
        }
      }
    }
  ],
  "extensionsUsed": ["CUSTOM_materials_layers"]
}
```

## Engine implementation guide

### Minimal shader

```glsl
// Sample base
vec4 base_color  = texture(u_baseColor, uv) * u_baseColorFactor;
float metallic   = texture(u_metallicRough, uv).b * u_metallicFactor;
float roughness  = texture(u_metallicRough, uv).g * u_roughnessFactor;
vec3  normal     = sampleNormalMap(u_normal, uv);

vec3  emissive   = u_emissiveFactor;

// Apply each layer in order
for (int i = 0; i < u_layerCount; ++i) {
    Layer L = u_layers[i];
    if (!L.enabled) continue;

    float m = sampleMask(L);                 // 0..1 (1.0 if no mask)
    if (L.invert) m = 1.0 - m;
    m *= L.opacity;                          // scalar layer opacity

    vec4 lc = texture(L.baseColor, L.uv) * L.baseColorFactor;
    float lm = texture(L.metallicRough, L.uv).b * L.metallicFactor;
    float lr = texture(L.metallicRough, L.uv).g * L.roughnessFactor;
    vec3  ln = sampleNormalMap(L.normal, L.uv);
    vec3  le = texture(L.emissive, L.uv).rgb * L.emissiveFactor;

    // f() is the per-mode blend from the blendMode table; MIX is f(a,b)=b.
    base_color.rgb = mix(base_color.rgb, f(base_color.rgb, lc.rgb, L.blendMode), m);
    base_color.a   = mix(base_color.a, lc.a, m);   // alpha = baseColorFactor[3]
    metallic       = mix(metallic, lm, m);
    roughness      = mix(roughness, lr, m);
    normal         = blendNormalsRNM(normal, ln, m);
    emissive       = mix(emissive, le, m);
    // subsurface, clearcoat and sheen blend the same way when supported.
    // Only fields the layer actually carries blend; absent ones pass through.
}
```

### Mask sampling

```glsl
float sampleMask(Layer L) {
    if (L.source == TEXTURE)      return texture(L.maskTex, L.maskUV)[L.channel];
    // 0 where the mesh carries no such attribute — see the mask object above.
    if (L.source == VERTEX_COLOR) return hasAttribute(L.attribute) ? v_color[L.channel] : 0.0;
    return 0.0;
}
```

The vertex color attribute is whatever the engine binds to `v_color` for the named `attribute` (default `COLOR_0`, glTF's standard vertex color). An engine that fills `v_color` with white when the mesh has no such attribute — the usual arrangement, because white is the identity for a tint — must not let that white reach this function: a missing attribute is a weight of 0.

### Normal blending

Linear-mixing tangent-space normals produces wrong results — the magnitude shrinks. Use Reoriented Normal Mapping (RNM) or Whiteout blending. RNM:

```glsl
vec3 blendNormalsRNM(vec3 n1, vec3 n2, float t) {
    vec3 n2_blended = mix(vec3(0,0,1), n2, t);
    vec3 t_n = n1 * vec3(2,2,2) + vec3(-1,-1,0);
    vec3 u_n = n2_blended * vec3(-2,-2,2) + vec3(1,1,-1);
    return normalize(t_n * dot(t_n, u_n) - u_n * t_n.z);
}
```

For `t = 0` you get `n1`; for `t = 1` you get `n2`. Cheap fallback if you don't care about correctness: `normalize(mix(n1, n2, t))`.

### Performance

- Layer count is part of the shader permutation. Generate variants for 0, 1, 2, … layers, or use a uniform loop with branching.
- Texture sampling cost dominates: a 3-layer material with full PBR per layer is **12+ texture fetches per pixel**. Consider:
  - Sharing UV transforms across a layer's textures
  - Using vertex color masks instead of texture masks where possible (free vs. one fetch)
  - Skipping layers when `m < epsilon` for the whole triangle (compute on CPU per-mesh, not per-pixel)
- Channel-pack masks: one RGBA splat texture drives up to 4 layers.

### Interaction with other extensions

| Extension | How it interacts |
|-----------|-----------------|
| `KHR_texture_transform` | Supported per-textureInfo inside layer textures and inside `mask.texture`. Use it for per-layer tiling |
| `KHR_materials_unlit` | If the base material is unlit, layers are blended into the unlit color. No lighting either way |
| `KHR_materials_emissive_strength` | Per-layer emission is in scope via `emissiveFactor` (color × strength is pre-multiplied into the factor, so a layer factor MAY exceed 1). The base material's emission still uses the standard material fields, including this extension |
| `KHR_materials_clearcoat` | Carries the *base* material's coat. Layers 1..N carry theirs in their own `clearcoat` object |
| `KHR_materials_sheen` | Carries the *base* material's sheen. Layers 1..N carry theirs in their own `sheen` object |

### Fallback behavior

A viewer that does not implement this extension will render the base material correctly because the extension data lives entirely in `extensions`. The glTF validator accepts unknown extensions when listed in `extensionsUsed` (not `extensionsRequired`); this extension SHOULD be listed in `extensionsUsed` only.

## Authoring (Blender)

This addon ships a single custom shader node called **`BSDF Stack`** (`Add → Custom → BSDF Stack` in the Shader Editor). One node holds the whole layer stack: each layer exposes a Principled-BSDF-style set of inputs (Color, Mask, Normal, Roughness, Metallic, Alpha, Emission, Subsurface, Coat, Sheen) and they are blended internally into one Principled BSDF, so **the blend is visible live in Blender's viewport**.

Coat IOR and Coat Tint have no sockets: glTF's clearcoat has no slot for either, so a socket for them would promise what no exported file can carry.

To author a layered material:

1. Add a `BSDF Stack` node and connect its `BSDF` output to **Material Output → Surface**.
2. **Layer 0 (the bottom layer) is the base material.** Set its Color/Roughness/Metallic/Normal/Emission inputs — either as default values or by wiring Image Texture nodes into the per-layer sockets.
3. Press **Add Layer** to stack more layers on top. Each layer's panel on the node exposes its own inputs; reorder or remove layers with the up/down/✕ buttons in the layer header.
4. For each upper layer set its **Mask** input (an Image Texture, or a Color Attribute for a vertex-color mask). Leave Mask unconnected (default 1.0) for a full-coverage layer.
5. Pick each layer's **blend mode** and **opacity** in the node UI, and toggle its **enabled** checkbox.

On export, the exporter finds the `BSDF Stack` node feeding Material Output:

- **Layer 0** becomes the material's own `pbrMetallicRoughness` / `normalTexture` / emissive fields (so viewers without the extension render it correctly). Layer 0's mask/opacity/blend mode are ignored.
- **Layers 1..N** become entries in the `layers` array, each carrying its PBR/normal/emission/subsurface/coat/sheen inputs, mask, `blendMode`, `opacity`, and `enabled` flag.

Every channel follows the same rule on export: a layer states a channel by wiring something into it or by moving it off its default, and an untouched channel is written nowhere. That is what makes a colour-only layer expressible — without it, adding a layer to tint some dirt would also drag the roughness under it to 0.5, force its metalness to 0 and black out its emission, all at full mask.

A **Bump node** feeding a layer's Normal socket is fully supported: its `Height` input (a depth/displacement map) is exported as `heightTexture` with the Bump's `strength`/`distance` as `bump`, and its `Normal` input becomes the layer's `normalTexture`. Layer 0's bump/height goes into the extension's `base` object since core glTF has no height slot. On export the Normal socket is walked through any Normal Map and/or Bump node to find the underlying images.

On import the node is rebuilt: layer 0 from the base material (plus `base` extras), layers 1..N from the extension. A layer with `heightTexture`/`bump` rebuilds a Bump node (Height ← depth map, Normal ← normal map); a layer with only `normalTexture` wires a Normal Map node directly.

`blendMode` accepts any of the ten Blender mix modes (`MIX`, `MULTIPLY`, `ADD`, `SUBTRACT`, `SCREEN`, `OVERLAY`, `SOFT_LIGHT`, `DIFFERENCE`, `DARKEN`, `LIGHTEN`); per-layer alpha is carried in `baseColorFactor[3]`.
