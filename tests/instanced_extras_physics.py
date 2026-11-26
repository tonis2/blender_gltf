"""Check that instanced copies keep their part's extras and collider.

A part inside an instanced collection is written through the instancing path
(EXT_mesh_gpu_instancing nodes, single-copy plain nodes, grouped mesh children
and sheared copies), never as an object node. Each of those nodes must carry
the part's custom properties as extras and its rigid-body collider, exactly
as a plain object with the same part gets them, so a reader can hand both to
every copy. Group and shear wrapper nodes carry neither, and no copy gets a
uid.

Run with Blender in background after enabling blender_gltf:
  blender --background --python tests/instanced_extras_physics.py
"""

import json
import math
import os
import struct
import tempfile

import bpy

from blender_gltf.exporter import ExportSettings, GltfExporter

EXT = "EXT_mesh_gpu_instancing"
RIGID = "KHR_physics_rigid_bodies"
SHAPES = "KHR_implicit_shapes"

# A box 1 x 1 x 2 standing on its origin: as a cylinder, height 2 and radius
# the corner distance.
CYLINDER = {"height": 2.0, "radiusTop": math.sqrt(0.5), "radiusBottom": math.sqrt(0.5)}

# piece -> (instancer locations, [(part, collision shape, walk, x offset)]).
# Every instancer sits somewhere else: equal transforms would group pieces.
# Grouped parts share the piece's transforms, so the second part's box is
# offset in its mesh; implicit shapes are measured about the part's origin,
# so the offset parts are the MESH ones.
PIECES = {
    "Slab": ([(10, 0, 0), (12, 0, 0)], [("Slab", "MESH", True, 0.0)]),
    "Step": ([(20, 0, 0)], [("Step", "MESH", True, 0.0)]),
    "Pillar": ([(30, 0, 0), (32, 0, 0)], [("Pillar", "CYLINDER", False, 0.0)]),
    "Stump": ([(40, 0, 0)], [("Stump", "CYLINDER", True, 0.0)]),
    "Gate": ([(50, 0, 0), (52, 0, 0)], [("Gate post", "CYLINDER", False, 0.0),
                                        ("Gate bar", "MESH", True, 3.0)]),
    "Arch": ([(60, 0, 0)], [("Arch left", "CYLINDER", False, 0.0),
                            ("Arch right", "MESH", True, 3.0)]),
}


def _box(name, x=0.0):
    mesh = bpy.data.meshes.new(name)
    verts = [(x + dx, dy, dz) for dz in (0.0, 2.0) for dy in (-0.5, 0.5) for dx in (-0.5, 0.5)]
    faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    mesh.from_pydata(verts, [], faces)
    return bpy.data.objects.new(name, mesh)


def _passive(obj, shape, collection):
    """A PASSIVE rigid body needs the object in the view layer to add; the
    part then lives only in its collection (the rigid body stays)."""
    scene = bpy.context.scene
    scene.collection.objects.link(obj)
    for other in bpy.context.selected_objects:
        other.select_set(False)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.rigidbody.object_add(type="PASSIVE")
    obj.rigid_body.collision_shape = shape
    if collection is not None:
        collection.objects.link(obj)
        scene.collection.objects.unlink(obj)


def _part(name, shape, walk, x, collection):
    obj = _box(name, x)
    obj["walk"] = walk
    obj["_internal"] = 1
    _passive(obj, shape, collection)
    return obj


def _place(name, collection, location, scale=(1.0, 1.0, 1.0)):
    empty = bpy.data.objects.new(name, None)
    empty.instance_type = "COLLECTION"
    empty.instance_collection = collection
    empty.location = location
    empty.scale = scale
    bpy.context.scene.collection.objects.link(empty)
    return empty


def build_scene():
    """The pieces above, a sheared Ramp copy, and one plain object per shape
    to compare the copies' colliders with."""
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    for piece, (places, parts) in PIECES.items():
        collection = bpy.data.collections.new(piece)
        for name, shape, walk, x in parts:
            _part(name, shape, walk, x, collection)
        for index, location in enumerate(places):
            _place(f"{piece} {index}", collection, location)

    # A turned part under a non-uniform scale is sheared: two TRS nodes.
    ramp = bpy.data.collections.new("Ramp")
    _part("Ramp", "MESH", True, 0.0, ramp).rotation_euler = (0.0, 0.0, math.radians(45.0))
    _place("Ramp 0", ramp, (70, 0, 0), scale=(2.0, 1.0, 1.0))

    for shape in ("MESH", "CYLINDER"):
        reference = _part(f"Reference {shape}", shape, True, 0.0, None)
        reference.location = (100, 0, 0) if shape == "MESH" else (110, 0, 0)
    bpy.context.view_layer.update()


def read_glb_json(path):
    with open(path, "rb") as handle:
        data = handle.read()
    magic, _version, _length = struct.unpack_from("<4sII", data, 0)
    assert magic == b"glTF", magic
    chunk_length, chunk_type = struct.unpack_from("<I4s", data, 12)
    assert chunk_type == b"JSON", chunk_type
    return json.loads(data[20:20 + chunk_length])


def export(directory, name, **overrides):
    path = os.path.join(directory, name + ".glb")
    settings = ExportSettings(
        filepath=path,
        export_format="GLB",
        export_animations=False,
        export_materials=False,
        export_particles=False,
        export_interactivity=False,
        export_audio=False,
        export_environment_map=False,
        **overrides,
    )
    GltfExporter(bpy.context, settings).export()
    return read_glb_json(path)


def main():
    build_scene()
    with tempfile.TemporaryDirectory() as directory:
        document = export(directory, "copies")
        no_extras = export(directory, "no_extras", export_extras=False, export_physics=False)

    nodes = document["nodes"]
    by_name = {}
    for node in nodes:
        assert node["name"] not in by_name, node["name"]
        by_name[node["name"]] = node
    shapes = document["extensions"][SHAPES]["shapes"]
    assert SHAPES in document["extensionsUsed"] and RIGID in document["extensionsUsed"]

    def collider(node):
        return node["extensions"][RIGID]

    references = {shape: collider(by_name[f"Reference {shape}"]) for shape in ("MESH", "CYLINDER")}
    assert by_name["Reference MESH"]["extras"] == {"walk": True}

    def check_copy(node, shape, walk, instanced):
        name = node["name"]
        assert "mesh" in node, name
        assert node.get("extras") == {"walk": walk}, (name, node.get("extras"))
        assert (EXT in node["extensions"]) == instanced, (name, node["extensions"])
        assert "uid" not in node, name
        body = collider(node)
        assert "motion" not in body, (name, body)  # PASSIVE: a static collider
        geometry = body["collider"]["geometry"]
        if shape == "MESH":
            assert geometry == {"mesh": node["mesh"]}, (name, geometry)
        else:
            implicit = shapes[geometry["shape"]]
            assert implicit["type"] == "cylinder", (name, implicit)
            for key, value in CYLINDER.items():
                assert abs(implicit["cylinder"][key] - value) < 1e-5, (name, implicit)
        # The same collider a plain object with this part gets.
        wanted = json.loads(json.dumps(references[shape]))
        if shape == "MESH":
            wanted["collider"]["geometry"]["mesh"] = node["mesh"]
        assert body == wanted, (name, body, wanted)

    def check_wrapper(node):
        assert "mesh" not in node and "extras" not in node, node
        assert RIGID not in node.get("extensions", {}), node

    # Two copies of one part: the <part>_instances node.
    for piece in ("Slab", "Pillar"):
        (_name, shape, walk, _x), = PIECES[piece][1]
        check_copy(by_name[f"{piece}_instances"], shape, walk, instanced=True)
        assert piece not in by_name, piece

    # One copy of one part: a plain node at the copy's place.
    for piece in ("Step", "Stump"):
        (name, shape, walk, _x), = PIECES[piece][1]
        node = by_name[name]
        check_copy(node, shape, walk, instanced=False)
        assert node["translation"][0] == PIECES[piece][0][0][0], node

    # Grouped parts: each mesh child carries its own part's data; the group
    # node carries none. Two copies instance the children, one copy doesn't.
    for piece, instanced in (("Gate", True), ("Arch", False)):
        parts = PIECES[piece][1]
        children = {by_name[n]["name"] for n, *_ in parts}
        groups = [
            node for node in nodes
            if {nodes[i]["name"] for i in node.get("children", [])} == children
        ]
        assert len(groups) == 1, (piece, groups)
        check_wrapper(groups[0])
        for name, shape, walk, _x in parts:
            check_copy(by_name[name], shape, walk, instanced=instanced)

    # A sheared copy: the inner mesh node is the copy, the outer one a wrapper.
    check_wrapper(by_name["Ramp_shear"])
    check_copy(by_name["Ramp"], "MESH", True, instanced=False)

    # Off switches still hold: no extras without Custom Properties, no
    # collider without Physics.
    for node in no_extras["nodes"]:
        assert "extras" not in node, node
        assert RIGID not in node.get("extensions", {}), node

    print("instanced_extras_physics: OK")


if __name__ == "__main__":
    main()
