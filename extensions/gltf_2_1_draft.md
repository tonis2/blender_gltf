# glTF 2.1 [DRAFT] features

This addon implements four features from the in-development **glTF 2.1** ("complex
scenes") update: 64-bit binary files, expanded accessor component types, unique IDs, and
external assets with packaging.

> ⚠️ **Draft notice.** The glTF 2.1 wire formats for several of these features are still
> being developed openly by Khronos
> ([2.1 tracking issue](https://github.com/KhronosGroup/glTF/issues/2532),
> [UID discussion](https://github.com/KhronosGroup/glTF/issues/2337)). Where the official
> schema is not yet finalized, this addon uses a clean **best-effort convention** marked
> `[DRAFT]` below. These conventions round-trip perfectly through this addon and are aimed
> at the author's own engine; they are **not guaranteed** to interoperate with other 2.1
> tools until the spec lands, and the on-disk shapes may change to match it. The
> well-defined GLB version-3 container is implemented to the draft layout.

---

## 1. 64-bit binary files (GLB version 3)

The classic GLB container (version 2) stores chunk lengths in 32-bit fields, capping a file
at **4 GiB**. glTF 2.1 introduces a **version-3** container with 64-bit length fields.

**What this addon does**

- Exports a version-2 GLB by default. Version-2 output is **byte-identical** to before.
- Automatically upgrades to version 3 when a GLB would exceed 4 GiB (e.g. a large packaged
  scene — see §4).
- A **"Force 64-bit GLB"** export toggle (Binary panel) always writes version 3, useful for
  testing the path without a 4 GiB asset.
- Imports **both** version 2 and version 3 transparently (the container version is detected
  from the header).

**Layout (version 3) [DRAFT]**

| Region | Field | Bytes |
|--------|-------|-------|
| Header | `glTF` magic | 4 |
|        | version = `3` (`uint32` LE) | 4 |
|        | total length (`uint64` LE) | 8 |
| Chunk header | chunk length (`uint64` LE) | 8 |
|        | chunk type (`JSON` / `BIN\0`) | 4 |
|        | reserved chunk-encoding = `0` (`uint32` LE) | 4 |

The reserved chunk-encoding field is always `0` ("no encoding"); it is a placeholder for
future per-chunk compression. A non-zero value is rejected on import.

---

## 2. Accessor component-type definitions

glTF 2.0 accessors support `BYTE`, `UNSIGNED_BYTE`, `SHORT`, `UNSIGNED_SHORT`,
`UNSIGNED_INT`, and `FLOAT`. glTF 2.1 adds core definitions for more types so that
extensions can reference a common, consistent set.

**What this addon does**

- Defines the new component types so the **importer can decode** accessors that use them:

  | Type | `componentType` | numpy dtype | Notes |
  |------|-----------------|-------------|-------|
  | `SIGNED_INT` | `5124` | `int32` | GL_INT |
  | `DOUBLE` | `5130` | `float64` | GL_DOUBLE |
  | `HALF_FLOAT` | `5131` | `float16` | GL_HALF_FLOAT |
  | `SIGNED_INT64` | `5134` | `int64` | **[DRAFT sentinel]** — no GL token |
  | `UNSIGNED_INT64` | `5135` | `uint64` | **[DRAFT sentinel]** |

- Core mesh export is **unchanged** — positions, normals, UVs, and indices still use only
  the glTF 2.0 component types. Defining the new types in core does not change which types
  mesh attributes accept; it just lets the importer (and future extensions) handle them.

The `5134`/`5135` values are this addon's draft sentinels and may change when Khronos
assigns official values.

---

## 3. Unique IDs (`uid`)

glTF object `name`s are not guaranteed unique, which makes them unreliable as engine
handles. glTF 2.1 adds a per-file **`uid`** string on objects. A `uid` must not collide with
any other `uid` **or** any `name` in the same file.

**What this addon does** — node-level `uid`, **round-trip + auto-fill**:

- On export (**"Unique IDs"** toggle, on by default), every object node gets a `uid`:
  - reuses the object's `gltf_uid` custom property if present and unique;
  - otherwise generates a stable one (`<objectname>-<8 hex>`) and **writes it back** onto the
    object, so re-exports reproduce the same id;
  - regenerates (with a warning) if a stored id collides with another id or any node name.
- On import, each node's `uid` is restored onto the object as a `gltf_uid` custom property
  (**"Unique IDs"** import toggle).

**JSON [DRAFT]**

```json
{ "nodes": [ { "name": "Cube", "uid": "Cube-9f3a2c14", "translation": [0,0,0] } ] }
```

To pin an id, set a `gltf_uid` custom property on the object yourself before exporting.

---

## 4. External assets + packaging

glTF 2.1 lets one file **reference other glTF/GLB files** that are instantiated into the
scene at load, and lets those files be **packaged** (embedded) so a single file is fully
self-contained.

**Blender mapping** — this addon maps Blender **collection instances** to external assets: an
Empty whose *Instancing* is set to **Collection** becomes an external-asset reference, and
each unique instanced collection is exported as its own sub-asset.

**What this addon does** (export toggle **"External Assets"**, off by default):

- Detects collection-instance Empties and writes them as nodes that reference a new top-level
  **`files`** array. The Empty's transform stays on the node.
- Exports each unique instanced collection **once** as a standalone sub-asset (deduped — N
  Empties of the same collection produce N nodes but one file).
- **External Mode**:
  - **Packaged** (default) — embeds each sub-asset GLB as a `bufferView` of the host file, so
    one GLB is self-contained. The host `files` array acts as a virtual filesystem.
  - **References** — writes each sub-asset as a sibling `.glb` and references it by `uri`.
- Nested collection instances recurse into nested sub-assets. Reference cycles and excessive
  nesting (depth > 8) are detected and skipped with a warning.
- On import (**"External Assets"** toggle), each referenced/packaged asset is imported once
  into its own collection and reconstructed as a Blender **collection instance** — a full
  round trip.

**JSON [DRAFT]**

```json
{
  "nodes": [
    { "name": "Oak.001", "translation": [5,0,0], "file": 0 },
    { "name": "Oak.002", "translation": [-5,0,2], "file": 0 }
  ],
  "files": [
    { "name": "Oak", "bufferView": 7, "mimeType": "model/gltf-binary" }
  ]
}
```

A referenced (non-packaged) entry instead looks like `{ "name": "Oak", "uri": "Oak.glb" }`.
The `file` node field and the `files` array are this addon's draft convention; if Khronos
finalizes a different shape (e.g. an extension), the on-disk layout will be updated to match.

**Tips**

- Keep a source collection that you intend to instance **out of the active scene's view
  layer** (the normal collection-instance workflow). A collection that is *also* linked
  directly into the exported scene will have its geometry exported both inline and as a
  sub-asset.
- `GPU Instancing` and `External Assets` both consume collection instances. When `External
  Assets` is on, collection-instance Empties are routed to the `files` array instead of being
  flattened into `EXT_mesh_gpu_instancing`.
