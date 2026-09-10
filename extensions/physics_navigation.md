# CUSTOM_physics_navigation

An optional root extension containing versioned, precomputed navigation profiles.
It is derived exclusively from passive, non-trigger `KHR_physics_rigid_bodies`
collider sources and does not modify that extension.

```json
{"extensions":{"CUSTOM_physics_navigation":{"version":1,"profiles":[{
  "name":"player", "cellSize":0.42, "agentRadius":0.30,
  "agentHeight":1.66, "stepHeight":0.42, "slopeLimit":54,
  "origin":[-10,-2,-10], "dimensions":[48,16,48], "cells":7,
  "components":1, "largestComponent":2048, "solidCells":900,
  "floorCells":2400, "sourceHash":"sha256 hex"
}]}}}
```

`cells` references a core unsigned-int `SCALAR` accessor of ascending linear
voxel indices. Indexing is `x + nx * (y + ny * z)` in glTF world coordinates
(Y up). `origin` is the minimum corner of voxel `(0,0,0)`; its center is
`origin + 0.5 * cellSize`. The serialized grid includes two empty boundary cells
on every side, so neighbour walks can inspect a full 3x3x3 stencil safely. Dimensions and counts
are non-negative integers. Distances use glTF metres and `slopeLimit` uses degrees.

Consumers connect cells whose X/Z coordinates differ by at most one and whose
floor-height difference is at most `stepHeight`. A diagonal requires compatible
cells on both intervening cardinal sides; this prevents paths cutting through a
blocked corner. Triangles count as floor only when their winding produces an
upward normal within `slopeLimit`; downward-facing undersides are not floors.

The exporter rebuilds `BOX`, `SPHERE`, `CAPSULE`, `CYLINDER`, and `CONE` from
the same local dimensions written to `KHR_implicit_shapes`, using the runtime's
16 radial segments, eight sphere rows, and four capsule rings. `CONVEX_HULL`
uses Blender's convex-hull operation; planar or otherwise degenerate hulls fall
back to authored mesh triangles, matching the runtime's usable fallback.

`sourceHash` is lowercase SHA-256 over the profile, padded bounds and dimensions,
collider names and shape types, transforms, and canonical world-space triangles.
