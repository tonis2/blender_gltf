"""Baked textures kept in the .blend, so an export only re-bakes what changed.

A bake is packed into the file and tagged with what it was made from: a
fingerprint of the material's node tree (node groups and images included),
the evaluated mesh, the bake resolution and the Blender version. The next
export reuses an image whose fingerprint still matches and bakes a fresh one
otherwise. Packed images are not "modified", so saving does not ask about
them.
"""

from __future__ import annotations

import hashlib
import os

BAKE_ID_PROP = "gltf_bake_id"      # which (material, mesh, pass) an image holds
BAKE_KEY_PROP = "gltf_bake_key"    # fingerprint of what it was baked from
BAKE_TARGET_PROP = "gltf_bake_target"  # marks the exporter's own image node

# Raise when the bake itself changes, so files baked by an older exporter
# bake again.
_BAKE_VERSION = 1

# Node and struct properties that do not change what a node computes: layout,
# selection and the UI. ``frame_current`` (image users) follows the playhead.
_SKIP_PROPS = frozenset({
    "rna_type", "name", "label", "location", "location_absolute", "width",
    "width_hidden", "height", "dimensions", "select", "show_options",
    "show_preview", "show_texture", "hide", "color", "use_custom_color",
    "parent", "inputs", "outputs", "internal_links", "bl_idname", "bl_label",
    "bl_description", "bl_icon", "bl_static_type", "bl_width_default",
    "bl_width_min", "bl_width_max", "bl_height_default", "bl_height_min",
    "bl_height_max", "warning_propagation", "color_tag", "frame_current",
})
_MAX_DEPTH = 4  # deep enough for curve mapping -> curves -> points

# Mesh attribute types: (foreach field, components, numpy dtype name).
_ATTRIBUTE_FIELDS = {
    "FLOAT": ("value", 1, "float32"),
    "INT": ("value", 1, "int32"),
    "INT8": ("value", 1, "int32"),
    "BOOLEAN": ("value", 1, "bool"),
    "FLOAT_VECTOR": ("vector", 3, "float32"),
    "FLOAT2": ("vector", 2, "float32"),
    "INT32_2D": ("value", 2, "int32"),
    "INT16_2D": ("value", 2, "int32"),
    "FLOAT_COLOR": ("color", 4, "float32"),
    "BYTE_COLOR": ("color", 4, "float32"),
    "QUATERNION": ("value", 4, "float32"),
    "FLOAT4X4": ("value", 16, "float32"),
}


def _value_bytes(value) -> bytes:
    if isinstance(value, (set, frozenset)):
        # Enum flags. A set's order follows string hashing, which changes from
        # one Blender session to the next.
        value = tuple(sorted(value))
    elif hasattr(value, "__len__") and not isinstance(value, str):
        try:
            value = tuple(tuple(v) if hasattr(v, "__len__") else v for v in value)
        except TypeError:
            pass
    return repr(value).encode()


def _hash_struct(h, struct, seen, depth: int) -> None:
    import bpy

    for prop in struct.bl_rna.properties:
        ident = prop.identifier
        if ident in _SKIP_PROPS:
            continue
        try:
            value = getattr(struct, ident)
        except Exception:  # noqa: BLE001 - some properties refuse to read
            continue
        h.update(ident.encode())
        if prop.type == "POINTER":
            if value is None:
                h.update(b"-")
            elif isinstance(value, bpy.types.ID):
                _hash_id(h, value, seen)
            elif depth < _MAX_DEPTH:
                _hash_struct(h, value, seen, depth + 1)
        elif prop.type == "COLLECTION":
            if depth < _MAX_DEPTH:
                for item in value:
                    if isinstance(item, bpy.types.ID):
                        _hash_id(h, item, seen)
                    else:
                        _hash_struct(h, item, seen, depth + 1)
        else:
            h.update(_value_bytes(value))


def _hash_image(h, image) -> None:
    # The file by name, size and time rather than by path: saving the .blend
    # elsewhere rewrites relative paths without changing the file they reach.
    for attr in ("source", "alpha_mode", "generated_type",
                 "generated_width", "generated_height"):
        h.update(_value_bytes(getattr(image, attr, None)))
    h.update(_value_bytes(getattr(image, "generated_color", None)))
    h.update(_value_bytes(image.colorspace_settings.name))
    h.update(_value_bytes(os.path.basename(image.filepath.replace("\\", "/"))))
    if image.packed_file is not None:
        h.update(_value_bytes(image.packed_file.size))
    path = image.filepath_from_user() if image.filepath else ""
    if path and os.path.exists(path):
        stat = os.stat(path)
        h.update(_value_bytes((stat.st_size, stat.st_mtime_ns)))
    if image.is_dirty:
        # Painted and not saved: only the pixels say what it holds.
        import numpy as np
        pixels = np.empty(len(image.pixels), dtype=np.float32)
        image.pixels.foreach_get(pixels)
        h.update(pixels.tobytes())


def _hash_tree(h, tree, seen) -> None:
    nodes = sorted(
        (node for node in tree.nodes if not node.get(BAKE_TARGET_PROP)),
        key=lambda node: node.name,
    )
    for node in nodes:
        h.update(node.bl_idname.encode())
        h.update(node.name.encode())
        _hash_struct(h, node, seen, 0)
        for socket in node.inputs:
            h.update(socket.identifier.encode())
            if socket.is_linked:
                h.update(b"L")
            elif hasattr(socket, "default_value"):
                h.update(_value_bytes(socket.default_value))
    links = sorted(
        (link.from_node.name, link.from_socket.identifier,
         link.to_node.name, link.to_socket.identifier, link.is_muted)
        for link in tree.links
        if not link.from_node.get(BAKE_TARGET_PROP) and not link.to_node.get(BAKE_TARGET_PROP)
    )
    h.update(_value_bytes(links))


def _hash_id(h, block, seen) -> None:
    import bpy

    kind = type(block).__name__
    h.update(kind.encode())
    h.update(block.name_full.encode())
    if (kind, block.name_full) in seen:
        return
    seen.add((kind, block.name_full))
    if isinstance(block, bpy.types.NodeTree):
        _hash_tree(h, block, seen)
    elif isinstance(block, bpy.types.Image):
        _hash_image(h, block)
    elif isinstance(block, bpy.types.Object):
        h.update(_value_bytes(block.matrix_world))
    elif isinstance(block, bpy.types.Text):
        h.update(block.as_string().encode())


def _hash_mesh(h, obj) -> None:
    """The mesh as the bake sees it: modifiers applied, every named attribute
    (positions, UV maps, colours, material indices), topology and normals.
    Selection and hide state live in dot-named attributes and are left out."""
    import bpy
    import numpy as np

    owner = None
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        owner = evaluated
    except (RuntimeError, ReferenceError):
        # Not in the view layer (an instance source reached through its
        # instancer): read the mesh as it is stored.
        mesh = obj.data
    if mesh is None or not hasattr(mesh, "loops"):
        h.update(_value_bytes(getattr(mesh, "name_full", None)))
        if owner is not None:
            owner.to_mesh_clear()
        return
    try:
        for name, items, field in (
            ("corner_vert", mesh.loops, "vertex_index"),
            ("face_start", mesh.polygons, "loop_start"),
        ):
            data = np.empty(len(items), dtype=np.int32)
            items.foreach_get(field, data)
            h.update(name.encode())
            h.update(data.tobytes())
        for attribute in sorted(mesh.attributes, key=lambda a: a.name):
            if attribute.name.startswith("."):
                continue
            spec = _ATTRIBUTE_FIELDS.get(attribute.data_type)
            if spec is None:
                continue
            field, width, dtype = spec
            data = np.empty(len(attribute.data) * width, dtype=dtype)
            try:
                attribute.data.foreach_get(field, data)
            except (AttributeError, RuntimeError, TypeError):
                continue
            h.update(f"{attribute.name}:{attribute.domain}:{attribute.data_type}".encode())
            h.update(data.tobytes())
        normals = getattr(mesh, "corner_normals", None)
        if normals is not None:
            data = np.empty(len(normals) * 3, dtype=np.float32)
            normals.foreach_get("vector", data)
            h.update(data.tobytes())
    finally:
        if owner is not None:
            owner.to_mesh_clear()


def fingerprint(material, obj, resolution: int) -> str:
    """What a bake of ``material`` on ``obj`` depends on, as a short hex key."""
    import bpy

    h = hashlib.blake2b(digest_size=16)
    h.update(_value_bytes((_BAKE_VERSION, tuple(bpy.app.version), int(resolution))))
    if material.node_tree is not None:
        _hash_id(h, material.node_tree, set())
    _hash_mesh(h, obj)
    return h.hexdigest()


def find(bake_id: str, key: str):
    """The kept bake for ``bake_id`` if it was made from ``key``, else None."""
    import bpy

    for image in bpy.data.images:
        if image.get(BAKE_ID_PROP) == bake_id and image.get(BAKE_KEY_PROP) == key:
            return image
    return None


def forget(bake_id: str) -> None:
    """Remove every kept bake of ``bake_id``: they are out of date."""
    import bpy

    for image in [i for i in bpy.data.images if i.get(BAKE_ID_PROP) == bake_id]:
        bpy.data.images.remove(image)


def keep(image, bake_id: str, key: str) -> None:
    """Pack a fresh bake into the file and tag it for the next export."""
    image[BAKE_ID_PROP] = bake_id
    image[BAKE_KEY_PROP] = key
    image.pack()
    image.use_fake_user = True


def kept_images() -> list:
    import bpy

    return [image for image in bpy.data.images if image.get(BAKE_ID_PROP)]


def clear() -> int:
    """Remove every kept bake. Returns how many were removed."""
    import bpy

    images = kept_images()
    for image in images:
        bpy.data.images.remove(image)
    return len(images)
