# CUSTOM_particle_emitter

A custom glTF extension that exports particle system parameters from Blender. The extension describes **how** to spawn particles at runtime — the game engine is responsible for simulating and rendering them.

## Extension placement

The extension is a **node-level** extension on the emitter object. A single node can have multiple particle emitters (e.g., fire + sparks on the same torch).

```json
{
  "nodes": [
    {
      "name": "Torch_01",
      "mesh": 2,
      "extensions": {
        "CUSTOM_particle_emitter": {
          "emitters": [ ... ]
        }
      }
    }
  ],
  "extensionsUsed": ["CUSTOM_particle_emitter"]
}
```

## Schema

### Emitter object

Each entry in the `emitters` array describes one particle system.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `name` | string | No | Identifier for this emitter |
| `emission` | object | Yes | How particles are spawned |
| `lifetime` | object | Yes | How long particles live |
| `velocity` | object | No | Initial particle velocity |
| `size` | object | Yes | Particle size |
| `physics` | object | No | Mass, drag, gravity |
| `rotation` | object | No | Particle spin |
| `render` | object | No | How particles are drawn |

### emission

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `count` | integer | 1000 | Total number of particles to emit |
| `rate` | number | - | Particles emitted per second |
| `duration` | number | 0 | Emission duration in seconds. `0` means continuous |
| `emitFrom` | string | `"face"` | Emission source: `"vert"`, `"face"`, or `"volume"` |

### lifetime

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `value` | number | 2.0 | Particle lifetime in seconds |
| `random` | number | 0.0 | Randomness factor (0-1). Actual lifetime is `value * (1 - random * rand())` |

### velocity

All values are in Blender units per second. Omit the entire object if all values are zero.

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `normalFactor` | number | 0.0 | Speed along the emitter surface normal |
| `tangentFactor` | number | 0.0 | Speed along the emitter surface tangent |
| `randomFactor` | number | 0.0 | Random velocity component added to each particle |
| `objectFactor` | number | 0.0 | Velocity inherited from emitter object motion |

### size

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `value` | number | 0.05 | Base particle radius |
| `random` | number | 0.0 | Randomness factor (0-1). Actual size is `value * (1 - random * rand())` |

### physics

Omitted when all values are at defaults.

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `mass` | number | 1.0 | Particle mass (affects force interactions) |
| `damping` | number | 0.0 | Velocity damping per frame (0 = no damping, 1 = full stop) |
| `gravityFactor` | number | 1.0 | Multiplier on world gravity. `0` = no gravity, negative = reverse gravity |

### rotation

Omitted when particles don't rotate.

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `angularVelocity` | number | 0.0 | Rotation speed in radians per second |
| `mode` | string | `"none"` | Rotation axis: `"none"`, `"velocity"`, `"horizontal"`, `"vertical"`, `"global_x"`, `"global_y"`, `"global_z"`, `"rand"` |
| `randomFactor` | number | 0.0 | Random initial rotation (0-1) |

### render

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `type` | string | `"billboard"` | `"billboard"` (camera-facing quad) or `"mesh"` (3D mesh per particle) |
| `material` | integer | - | Index into the glTF `materials` array. For billboards, use the base color texture as the particle sprite |
| `instanceMesh` | integer | - | Index into the glTF `meshes` array. Only present when `type` is `"mesh"` |
| `stretchWithVelocity` | boolean | false | Stretch billboard along velocity direction (trail effect) |

## Full example

A fire emitter on a torch:

```json
{
  "name": "Torch_01",
  "mesh": 0,
  "extensions": {
    "CUSTOM_particle_emitter": {
      "emitters": [
        {
          "name": "Fire",
          "emission": {
            "count": 300,
            "rate": 36.1446,
            "duration": 8.2917,
            "emitFrom": "face"
          },
          "lifetime": {
            "value": 1.25,
            "random": 0.3
          },
          "velocity": {
            "normalFactor": 2.0,
            "randomFactor": 0.5
          },
          "size": {
            "value": 0.08,
            "random": 0.3
          },
          "physics": {
            "mass": 0.5,
            "damping": 0.04,
            "gravityFactor": -0.3
          },
          "rotation": {
            "angularVelocity": 1.0,
            "mode": "rand",
            "randomFactor": 1.0
          },
          "render": {
            "type": "billboard",
            "material": 0
          }
        }
      ]
    }
  },
  "extras": {
    "vfx_type": "fire",
    "blend_mode": "additive"
  }
}
```

## Game engine implementation guide

### Minimal implementation

1. **Parse** the `CUSTOM_particle_emitter` extension from each node
2. **Create a particle emitter** at the node's world transform
3. **Spawn particles** at `emission.rate` particles/sec for `emission.duration` seconds (or indefinitely if 0)
4. **Per particle**, on spawn:
   - Set lifetime from `lifetime.value` with `lifetime.random` variation
   - Set initial velocity along emitter mesh normal * `velocity.normalFactor`, plus random spread from `velocity.randomFactor`
   - Set size from `size.value` with `size.random` variation
5. **Per frame**, for each living particle:
   - Apply gravity: `velocity.y += world_gravity * physics.gravityFactor * dt`
   - Apply damping: `velocity *= (1 - physics.damping)`
   - Update position: `position += velocity * dt`
   - Kill particle when age exceeds lifetime
6. **Render** each particle as a camera-facing quad (billboard) using the material's base color texture

### Velocity model

Particles are emitted from the surface of the emitter mesh. The initial velocity for each particle is:

```
v_initial = surface_normal * normalFactor
          + surface_tangent * tangentFactor
          + random_unit_vector * randomFactor
```

If the emitter mesh is not available or `emitFrom` is `"vert"`, use vertex normals. For `"volume"`, spawn at random points inside the mesh bounding box.

### Render types

**Billboard** (`type: "billboard"`):
- Render each particle as a camera-facing quad
- Sample the sprite texture from the referenced `material` (use `pbrMetallicRoughness.baseColorTexture`)
- If the material has `KHR_materials_unlit`, skip lighting calculations
- If `stretchWithVelocity` is true, stretch the quad along the particle's velocity vector (useful for rain, sparks)

**Mesh** (`type: "mesh"`):
- Render the mesh at `instanceMesh` at each particle's position/rotation/scale
- Use GPU instancing for performance
- Useful for debris, falling objects, scattered items

### Blend mode

The glTF spec doesn't have a particle blend mode. Check `node.extras` for a `blend_mode` hint:
- `"additive"` — additive blending (fire, sparks, magic)
- `"alpha"` — alpha blending (smoke, dust, fog)
- If absent, default to alpha blending

### Interaction with other extensions

| Extension | How it interacts |
|-----------|-----------------|
| `KHR_materials_unlit` | Particle material marked as unlit — skip lighting, render flat. Most particle sprites should use this |
| `node.extras` | Custom properties on the emitter node. Use for engine-specific hints like `blend_mode`, `vfx_priority`, `sort_order` |
| `KHR_physics_rigid_bodies` | The emitter object may also have a collider. Particles and physics are independent — the collider defines the object's physics shape, not the particle emission shape |
| `EXT_mesh_gpu_instancing` | Separate system. GPU instancing is for static placement (rocks, trees). Particles are for dynamic runtime effects. They do not overlap |

### Performance considerations

- `emission.count` is the total particle budget — use it to scale quality
- `emission.rate` tells you the steady-state particle count: `rate * lifetime.value` particles alive at once
- For `mesh` render type, use instanced rendering
- For `billboard` render type, batch all particles of the same material into one draw call
- Consider LOD: reduce `emission.rate` for distant emitters
