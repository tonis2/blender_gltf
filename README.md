# Blender glTF 2.0 Exporter/Importer

A custom Blender addon for exporting and importing scenes in glTF 2.0 format.

## Features

- **Meshes** — geometry with normals, UVs, and vertex colors
- **PBR Materials & Textures**
- **Animations** — keyframe animations
- **Shape Keys** — morph targets
- **Armatures & Skinning** — skeletal animation with bone weights
- **Physics** — rigid bodies and collision shapes (KHR extensions)
- **GPU Instancing** — collection instances via `EXT_mesh_gpu_instancing`
- **Export formats** — GLB (binary), glTF + .bin (separate), glTF embedded (base64)
- **glTF 2.1 [DRAFT] features** — 64-bit GLB, expanded accessor component types, unique IDs,
  and external assets + packaging (see [`extensions/gltf_2_1_draft.md`](extensions/gltf_2_1_draft.md))

## Supported glTF Extensions

| Extension | Description |
|-----------|-------------|
| `KHR_lights_punctual` | Point, directional, and spot lights |
| `KHR_node_visibility` | Per-node visibility |
| `KHR_animation_pointer` | Material property animations |
| `KHR_physics_rigid_bodies` | Rigid bodies, colliders, and joints |
| `KHR_implicit_shapes` | Collision shapes for physics |
| `EXT_mesh_gpu_instancing` | Efficient GPU instancing for repeated objects |

## Requirements

- Blender 4.2.0 or newer

## Installation

1. Download the latest `.zip` release from the [Releases](../../releases) page
2. Open Blender
3. Go to **Edit > Preferences > Add-ons**
4. Click **Install from Disk...** (dropdown arrow in the top-right)
5. Select the downloaded `.zip` file
6. Enable the addon by checking the checkbox next to **"glTF 2.0 Exporter (Custom)"**

## Usage

- **Export:** File > Export > glTF 2.0
- **Import:** File > Import > glTF 2.0

## License

Apache 2.0
