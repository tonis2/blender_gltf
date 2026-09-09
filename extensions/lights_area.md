# CUSTOM_lights_area

`CUSTOM_lights_area` stores finite area-light emitters in glTF. It supports
Blender's square, rectangle, disk, and ellipse area-light shapes without
replacing them with punctual-light approximations.

This is a private extension, not a Khronos-ratified glTF extension. Files list
it in `extensionsUsed`, but not `extensionsRequired`, so readers that do not
recognize it can still load the rest of the scene. Such readers see the light's
transform node but no emitter.

## Structure

The root extension contains a library of area-light definitions. A node selects
one definition by its zero-based array index, following the same library and
node-reference pattern as `KHR_lights_punctual`.

```json
{
  "extensionsUsed": ["CUSTOM_lights_area"],
  "extensions": {
    "CUSTOM_lights_area": {
      "lights": [
        {
          "name": "Window softbox",
          "shape": "rectangle",
          "width": 4.0,
          "height": 1.5,
          "color": [1.0, 0.72, 0.48],
          "powerWatts": 800.0,
          "spread": 2.094395,
          "range": 24.0
        }
      ]
    }
  },
  "nodes": [
    {
      "name": "Window softbox",
      "translation": [2.0, 3.0, -1.0],
      "extensions": {
        "CUSTOM_lights_area": { "light": 0 }
      }
    }
  ]
}
```

## Root extension object

The root `extensions.CUSTOM_lights_area` object has one property:

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `lights` | array of [area-light definitions](#area-light-definition) | Yes | The area-light library. It must contain at least one entry when the extension is used |

## Node extension object

The node `extensions.CUSTOM_lights_area` object has one property:

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `light` | integer | Yes | Zero-based index into the root `lights` array |

`light` must be non-negative and smaller than the root array length. Several
nodes may reference the same definition. Each node supplies its own transform,
so linked Blender light data can be placed more than once without duplicating
the definition.

An area-light node should not also contain `KHR_lights_punctual`. This addon
exports Blender area lights only through `CUSTOM_lights_area`.

## Area-light definition

| Property | Type | Required | Default when reading | Description |
|----------|------|----------|----------------------|-------------|
| `name` | string | No | Generated name | Human-readable light-data name |
| `shape` | string | Yes | `"rectangle"` | Emitting surface shape: `"square"`, `"rectangle"`, `"disk"`, or `"ellipse"` |
| `width` | number | Yes | `1.0` | Local X extent in metres, before the node transform |
| `height` | number | Yes | `width` | Local Y extent in metres, before the node transform |
| `color` | array of 3 numbers | No | `[1.0, 1.0, 1.0]` | Linear RGB emission color |
| `powerWatts` | number | Yes | `10.0` | Radiant power in watts, copied from Blender's area-light energy |
| `spread` | number | No | π | Emission spread angle in radians, from `0` through π |
| `range` | number | No | Renderer-defined | Cutoff distance in metres, copied from Blender when **Custom Distance** is enabled |

The addon writes every property in the table, including values equal to the
reader defaults. `name` is the only exception when a source has no useful name.
The defaults describe the importer's tolerant behavior; producers should write
`shape`, `width`, `height`, and `powerWatts` explicitly.

Dimensions and power should be finite and non-negative. A zero dimension has no
emitting area and a zero power emits no light. When present, `range` must be
finite and positive. Unknown `shape` strings are invalid rather than aliases for
a rectangle.

For `square` and `disk`, canonical writers set `height` equal to `width` and
readers use `width` for both axes. `rectangle` and `ellipse` use the two extents
independently.

## Coordinate system and transform

The definition describes a surface in the node's local XY plane, centered at
the node origin:

- local +X spans `width`;
- local +Y spans `height`;
- light is emitted from the local −Z face.

The node's ordinary glTF transform places, rotates, and scales that surface.
Consequently, the effective world-space width is `width` multiplied by the
length of the transformed local X basis; height is treated the same way with
the local Y basis. Translation and rotation do not change the definition.

Transform the local −Z emission direction independently from the two footprint
axes. In particular, mirroring local X or Y changes the surface parameterization
but must not accidentally make the light emit from its other side. This is also
why deriving the emitting direction solely from a cross product of transformed
X and Y axes is unsafe when a node has a negative scale.

The transform uses normal glTF conventions: right-handed, Y up, metres, and
column-vector node transforms. Blender's Z-up conversion is handled by the
exporter and importer rather than encoded in this extension.

## Shape interpretation

| Shape | Surface domain in local XY |
|-------|----------------------------|
| `square` | Rectangle with X and Y extents both equal to `width` |
| `rectangle` | Rectangle with X extent `width` and Y extent `height` |
| `disk` | Ellipse with X and Y diameters both equal to `width` |
| `ellipse` | Ellipse with X diameter `width` and Y diameter `height` |

`width` and `height` are full extents or diameters, not half-extents or radii.

## Energy and spread

`powerWatts` is copied without a punctual-light conversion from Blender
`AreaLight.energy`. It is radiant power over the whole emitting surface, not
candela, lumens, illuminance, or a unitless intensity. A physically based
renderer should distribute this power over the transformed surface area and
its emission lobe. Engines with artistic lighting units may apply their own
calibration, but should keep it consistent across all four shapes.

`color` is a linear RGB multiplier on that power. It is not sRGB-encoded.

`spread` controls how widely emission fans out around local −Z, like Blender's
gridded-softbox control. π represents the unrestricted hemisphere; smaller
values narrow the emission. Readers that do not implement spread should treat
the value as π rather than changing the surface size or total power.

`range` is an authored performance cutoff, not a change to the light's inverse-
square falloff. Readers use it to stop evaluating the emitter beyond that
distance. When it is absent, each renderer retains its normal area-light range
policy; absence does not imply a new fixed or scene-derived default.

The extension records the light source, not a particular sampling algorithm.
A renderer may integrate analytically, use linearly transformed cosines, sample
the surface, or use another approximation. Surface shape, orientation, size,
color, and power should remain observably consistent.

## Blender mapping

| Blender area-light data | Extension data |
|-------------------------|----------------|
| `shape` | Lowercase `shape` |
| `size` | `width` |
| `size_y` | `height` for rectangles and ellipses |
| `color` | `color` |
| `energy` | `powerWatts` |
| `spread` | `spread` |
| `cutoff_distance`, when `use_custom_distance` is enabled | `range` |
| Object location, rotation, and scale | Core glTF node transform |

On import, the mapping is reversed. A square or disk uses `width` as Blender
`size`; a rectangle or ellipse additionally restores `height` to `size_y`. A
present `range` enables Blender's **Custom Distance** and restores its cutoff;
an absent `range` leaves **Custom Distance** disabled.

## Runtime loading

A minimal reader performs these steps:

1. Read `extensions.CUSTOM_lights_area.lights` from the glTF root.
2. For each node with `extensions.CUSTOM_lights_area`, validate its `light`
   index and obtain the referenced definition.
3. Build the finite surface in the node's local XY plane.
4. Apply the node's world transform to its center and footprint axes, and
   transform local −Z for its emitting direction.
5. Evaluate the light using its shape, linear color, radiant power, and spread.

Invalid indices should disable only that emitter, not abort loading unrelated
scene content.

## Round-trip behavior

The addon exports and imports all four shapes, non-uniform dimensions, color,
power, spread, optional range, and node transforms. Linked light definitions
remain shareable. Negative footprint scaling is preserved without reversing the
emitting face.

The following Blender properties are not represented:

- shadow enablement, shadow-map resolution, bias, filtering, and contact-shadow
  controls;
- light-node shader graphs, gobos, and IES profiles;
- blackbody temperature as a separate parameter;
- Blender renderer settings that alter normalization or sampling.

Those are renderer or pipeline policies rather than part of the portable light
definition. If a project needs them, it should add a separate extension instead
of changing the meaning of the fields above.

## Relationship to core glTF and KHR_lights_punctual

Core glTF has no light object. `KHR_lights_punctual` adds directional, point,
and spot emitters, but it has no finite emitting surface and its intensity uses
lux or candela depending on type. Mapping an area light into that extension
would lose its dimensions, shape, watt power, and characteristic soft-light
behavior.

`CUSTOM_lights_area` therefore complements `KHR_lights_punctual`; it does not
alter or extend a punctual-light entry. A scene can use both extensions on
different nodes.
