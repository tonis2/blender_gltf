"""Check placement markers ("Instance Markers").

With the option on, each top-level collection instance is also written as an
empty node named after its object, at its world transform, whose extras are
the collection's custom properties under the object's own plus "piece": the
collection's name. An instancer nested inside an instanced collection gets no
marker, the copies' instancing nodes are untouched, and with the option off
the file is what it was before markers existed.

The pieces live in an "Assets" collection excluded from the view layer, the
usual layout for a kit, so the nested instancer is in scene.objects too. A
second scene links its pieces from a kit .blend and names a placed empty
after the piece's part: the local and linked objects share a name, and each
placement must still get its marker.

Run with Blender in background after enabling blender_gltf:
  blender --background --python tests/instance_markers.py
"""

import json
import math
import os
import struct
import tempfile

import bpy
from mathutils import Euler

from blender_gltf.exporter import ExportSettings, GltfExporter

EXT = "EXT_mesh_gpu_instancing"
VISIBILITY = "KHR_node_visibility"
PIECE_PROPS = {"footprint": "8x7", "kind": "tree", "interior": True}
# Placed trees: name -> (location, rotation_euler, scale, the object's own properties)
PLACES = {
    "Tree A": ((2.0, 3.0, 0.0), (0.0, 0.0, math.radians(30.0)), (1.0, 1.0, 1.0), {}),
    "Tree B": ((-4.0, 1.0, 0.5), (0.0, 0.0, math.radians(90.0)), (2.0, 2.0, 2.0), {}),
    "Tree C": ((5.0, -6.0, 1.0), (0.0, 0.0, 0.0), (1.0, 1.0, 1.0), {"kind": "stump", "_draft": 1}),
}
HIDDEN = "Tree D"
# A kit linked from its own .blend into a second scene. Each piece's first
# empty takes the name of the piece's part, so the local empty "pine_edge"
# sits beside the linked part "pine_edge [kit.blend]":
# piece (= its part's name) -> (collection props, part props, placements)
KIT = {
    "pine_edge": ({"kind": "tree", "footprint": "2x2"}, {"walk": False}, {
        "pine_edge": (1.0, 2.0, 0.0),
        "pine_edge.001": (3.0, 2.0, 0.0),
        "pine_edge.002": (5.0, 2.0, 0.0),
    }),
    # Placed once: its part is a single-copy plain node also named "well".
    "well": ({"kind": "well", "interior": False}, {"blocks": True}, {
        "well": (9.0, 9.0, 0.0),
    }),
}


def _triangle(name, offset):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(offset, 0, 0), (offset + 1, 0, 0), (offset, 1, 0)], [], [(0, 1, 2)])
    return bpy.data.objects.new(name, mesh)


def _place(name, collection, location, rotation=(0.0, 0.0, 0.0), scale=(1.0, 1.0, 1.0)):
    empty = bpy.data.objects.new(name, None)
    empty.instance_type = "COLLECTION"
    empty.instance_collection = collection
    empty.location = location
    empty.rotation_euler = rotation
    empty.scale = scale
    return empty


def build_scene():
    """Assets (excluded) holds Tree (Trunk, Crown and a Nest instancing Bird);
    Tree A-C are placed in the scene, and Tree D is placed but hidden."""
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    scene = bpy.context.scene

    assets = bpy.data.collections.new("Assets")
    scene.collection.children.link(assets)
    bird = bpy.data.collections.new("Bird")
    bird.objects.link(_triangle("Bird body", 0.0))
    assets.children.link(bird)

    tree = bpy.data.collections.new("Tree")
    for key, value in PIECE_PROPS.items():
        tree[key] = value
    tree["_note"] = "authoring only"
    tree.objects.link(_triangle("Trunk", 0.0))
    tree.objects.link(_triangle("Crown", 2.0))
    tree.objects.link(_place("Nest", bird, (0.0, 0.0, 3.0)))
    assets.children.link(tree)
    bpy.context.view_layer.layer_collection.children["Assets"].exclude = True

    for name, (location, rotation, scale, props) in PLACES.items():
        empty = _place(name, tree, location, rotation, scale)
        for key, value in props.items():
            empty[key] = value
        scene.collection.objects.link(empty)
    hidden = _place(HIDDEN, tree, (8.0, 8.0, 0.0))
    scene.collection.objects.link(hidden)
    hidden.hide_set(True)
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
        export_physics=False,
        export_particles=False,
        export_interactivity=False,
        export_audio=False,
        export_environment_map=False,
        **overrides,
    )
    GltfExporter(bpy.context, settings).export()
    with open(path, "rb") as handle:
        raw = handle.read()
    return read_glb_json(path), raw


def _markers(document):
    return {node["name"]: node for node in document["nodes"] if "piece" in node.get("extras", {})}


def _close(a, b, tolerance=1e-5):
    return len(a) == len(b) and all(abs(x - y) < tolerance for x, y in zip(a, b))


def _check_trs(node, location, rotation, scale):
    # Blender Z-up -> glTF Y-up, identity parts omitted.
    x, y, z = location
    assert _close(node.get("translation", [0.0, 0.0, 0.0]), [x, z, -y]), (node["name"], node)
    q = Euler(rotation, "XYZ").to_quaternion()
    got = node.get("rotation", [0.0, 0.0, 0.0, 1.0])
    want = [q.x, q.z, -q.y, q.w]
    dot = sum(a * b for a, b in zip(got, want))
    assert abs(abs(dot) - 1.0) < 1e-5, (node["name"], got, want)
    sx, sy, sz = scale
    assert _close(node.get("scale", [1.0, 1.0, 1.0]), [sx, sz, sy]), (node["name"], node)


def _by_name(document):
    """Nodes keyed by name with children resolved to names, so two exports
    compare even though markers shift later node indices."""
    nodes = document["nodes"]
    out = {}
    for node in nodes:
        node = dict(node)
        if "children" in node:
            node["children"] = [nodes[i]["name"] for i in node["children"]]
        assert node["name"] not in out, node["name"]
        out[node["name"]] = node
    return out


def link_kit(directory):
    """Write the KIT pieces to kit.blend, drop the local originals, and link
    the pieces into a new scene, World, which becomes the window's scene."""
    pieces = set()
    for name, (collection_props, part_props, _places) in KIT.items():
        collection = bpy.data.collections.new(name)
        part = _triangle(name, 0.0)
        for key, value in collection_props.items():
            collection[key] = value
        for key, value in part_props.items():
            part[key] = value
        collection.objects.link(part)
        pieces.add(collection)
    path = os.path.join(directory, "kit.blend")
    bpy.data.libraries.write(path, pieces, fake_user=True)
    for collection in pieces:
        for part in list(collection.objects):
            mesh = part.data
            bpy.data.objects.remove(part)
            bpy.data.meshes.remove(mesh)
        bpy.data.collections.remove(collection)

    with bpy.data.libraries.load(path, link=True) as (_source, linked):
        linked.collections = list(KIT)
    world = bpy.data.scenes.new("World")
    bpy.context.window.scene = world
    for collection in linked.collections:
        for name, location in KIT[collection.name][2].items():
            empty = _place(name, collection, location)
            assert empty.name == name, empty.name
            world.collection.objects.link(empty)
    world.view_layers[0].update()


def check_linked_kit(directory):
    """Every placement of a linked piece has its marker, also when the empty
    has the same name as the piece's part, and the marker's uid is the
    empty's. Nodes of the same name, marker and plain mesh node, both stay."""
    link_kit(directory)
    for name in KIT:
        same = [obj for obj in bpy.data.objects if obj.name == name]
        assert sorted(obj.library is not None for obj in same) == [False, True], same

    document, _ = export(directory, "kit", export_instance_markers=True)
    nodes = document["nodes"]
    markers = [node for node in nodes if "piece" in node.get("extras", {})]
    found = sorted(node["name"] for node in markers)
    wanted = sorted(name for _c, _p, places in KIT.values() for name in places)
    assert found == wanted, (found, wanted)

    for node in markers:
        piece = node["extras"]["piece"]
        collection_props, _part_props, places = KIT[piece]
        assert node["extras"] == {**collection_props, "piece": piece}, node
        x, y, z = places[node["name"]]
        assert _close(node["translation"], [x, z, -y]), node
        assert "mesh" not in node, node
        empty = next(o for o in bpy.data.objects if o.name == node["name"] and o.library is None)
        assert node.get("uid") and node["uid"] == empty.get("gltf_uid"), (node, empty.get("gltf_uid"))
    for obj in bpy.data.objects:
        assert obj.library is None or "gltf_uid" not in obj, obj.name_full

    # The copies keep their part's extras beside the markers.
    pines = [node for node in nodes if node["name"] == "pine_edge_instances"]
    assert len(pines) == 1 and EXT in pines[0]["extensions"], pines
    assert pines[0]["extras"] == KIT["pine_edge"][1], pines
    wells = [node for node in nodes if node["name"] == "well"]
    assert len(wells) == 2, wells
    plain = [node for node in wells if "mesh" in node]
    assert len(plain) == 1 and plain[0]["extras"] == KIT["well"][1], wells
    x, y, z = KIT["well"][2]["well"]
    assert _close(plain[0]["translation"], [x, z, -y]), plain


def main():
    build_scene()

    with tempfile.TemporaryDirectory() as directory:
        default_doc, default_raw = export(directory, "default")
        off_doc, off_raw = export(directory, "off", export_instance_markers=False)
        on_doc, _ = export(directory, "on", export_instance_markers=True)
        visible_doc, _ = export(
            directory, "visible", export_instance_markers=True, export_only_visible=True,
        )
        bare_doc, _ = export(
            directory, "bare", export_instance_markers=True, export_extras=False,
        )
        no_instancing_doc, _ = export(
            directory, "no_instancing", export_instance_markers=True, export_gpu_instancing=False,
        )

    # Off is the default, and writes no markers: the placed empties are not
    # nodes at all, as before markers existed.
    assert off_raw == default_raw, "export_instance_markers=False differs from the default"
    assert not _markers(off_doc), _markers(off_doc)
    off_names = {node["name"] for node in off_doc["nodes"]}
    assert not off_names & set(PLACES), off_names

    # On: one marker per top-level instance, the hidden one included; the
    # Nest inside Tree is part of the piece, not a placement.
    markers = _markers(on_doc)
    assert set(markers) == set(PLACES) | {HIDDEN}, sorted(markers)
    roots = on_doc["scenes"][on_doc.get("scene", 0)]["nodes"]
    for name, node in markers.items():
        assert on_doc["nodes"].index(node) in roots, name
        assert "mesh" not in node and "children" not in node, node
        assert EXT not in node.get("extensions", {}), node

    for name, (location, rotation, scale, props) in PLACES.items():
        node = markers[name]
        _check_trs(node, location, rotation, scale)
        wanted = dict(PIECE_PROPS)
        wanted.update({k: v for k, v in props.items() if not k.startswith("_")})
        wanted["piece"] = "Tree"
        assert node["extras"] == wanted, (name, node["extras"], wanted)
        assert VISIBILITY not in node.get("extensions", {}), node
    assert markers["Tree C"]["extras"]["kind"] == "stump"
    assert markers["Tree A"]["extras"]["interior"] is True

    # A hidden placement follows the existing rule: written hidden, or left
    # out under "Only Visible".
    hidden = markers[HIDDEN]
    assert hidden["extensions"][VISIBILITY] == {"visible": False}, hidden
    _check_trs(hidden, (8.0, 8.0, 0.0), (0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
    assert set(_markers(visible_doc)) == set(PLACES), sorted(_markers(visible_doc))

    # Custom Properties off: a marker still says what it is, nothing more.
    for name, node in _markers(bare_doc).items():
        assert node["extras"] == {"piece": "Tree"}, (name, node["extras"])

    # Markers ride on GPU instancing.
    assert not _markers(no_instancing_doc), _markers(no_instancing_doc)

    # The copies are untouched: every other node, accessor and mesh is the
    # same as with the option off, and the markers are extra roots. Tree D,
    # hidden, makes no copies, so it was a plain hidden empty already; its
    # marker is that node plus the marker's extras.
    off_nodes = _by_name(off_doc)
    on_nodes = _by_name(on_doc)
    assert set(on_nodes) - set(off_nodes) == set(PLACES), sorted(on_nodes)
    assert set(off_nodes) & set(markers) == {HIDDEN}, sorted(off_nodes)
    for name in PLACES:
        del on_nodes[name]
    on_nodes[HIDDEN] = {k: v for k, v in on_nodes[HIDDEN].items() if k != "extras"}
    assert on_nodes == off_nodes, (on_nodes, off_nodes)
    instancing = [node for node in on_nodes.values() if EXT in node.get("extensions", {})]
    assert {node["name"] for node in instancing} == {"Trunk", "Crown", "Bird body_instances"}, instancing
    for key in ("accessors", "bufferViews", "buffers", "meshes", "extensionsUsed"):
        assert on_doc.get(key) == off_doc.get(key), key

    def root_names(document):
        scene = document["scenes"][document.get("scene", 0)]
        return {document["nodes"][i]["name"] for i in scene["nodes"]}

    assert root_names(on_doc) == root_names(off_doc) | set(markers), (
        root_names(on_doc), root_names(off_doc))

    with tempfile.TemporaryDirectory() as directory:
        check_linked_kit(directory)

    print("instance_markers: OK")


if __name__ == "__main__":
    main()
