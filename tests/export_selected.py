"""Check "Only Selected": strict selection, re-rooting, and instancing.

Run with Blender in background after enabling blender_gltf:
  blender --background --python tests/export_selected.py
"""

import json
import os
import tempfile

import bpy

from blender_gltf.export.converter import convert_location
from blender_gltf.exporter import ExportSettings, GltfExporter


def _export(path, **overrides):
    settings = ExportSettings(
        filepath=path,
        export_format="GLTF_SEPARATE",
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
    with open(path, "r", encoding="utf8") as handle:
        return json.load(handle)


def _node_names(document):
    return [node.get("name") for node in document.get("nodes", [])]


def _node(document, name):
    for node in document.get("nodes", []):
        if node.get("name") == name:
            return node
    raise AssertionError(f"node '{name}' not in {_node_names(document)}")


def _assert_translation(node, blender_position):
    expected = convert_location(tuple(blender_position))
    actual = node.get("translation", [0.0, 0.0, 0.0])
    assert all(abs(a - b) < 1e-5 for a, b in zip(actual, expected)), (actual, expected)


def _select(*objects):
    wanted = {obj.name for obj in objects}
    for obj in bpy.context.scene.objects:
        obj.select_set(obj.name in wanted)


def _mesh(name):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    return mesh


def main():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    rig = bpy.data.objects.new("Rig", None)
    rig.location = (10.0, 0.0, 0.0)
    bpy.context.scene.collection.objects.link(rig)

    child_a = bpy.data.objects.new("Child A", _mesh("Child A"))
    child_a.parent = rig
    child_a.location = (0.0, 0.0, 3.0)
    bpy.context.scene.collection.objects.link(child_a)

    child_b = bpy.data.objects.new("Child B", _mesh("Child B"))
    child_b.parent = rig
    child_b.location = (0.0, 2.0, 0.0)
    bpy.context.scene.collection.objects.link(child_b)

    lone = bpy.data.objects.new("Lone", _mesh("Lone"))
    lone.location = (0.0, 5.0, 0.0)
    bpy.context.scene.collection.objects.link(lone)

    source = bpy.data.collections.new("Instance source")
    instanced = bpy.data.objects.new("Instance mesh", _mesh("Instance mesh"))
    source.objects.link(instanced)

    instances = []
    for index, x in enumerate((-4.0, 4.0)):
        empty = bpy.data.objects.new(f"Instance {index}", None)
        empty.instance_type = "COLLECTION"
        empty.instance_collection = source
        empty.location = (x, 0.0, 0.0)
        bpy.context.scene.collection.objects.link(empty)
        instances.append(empty)

    bpy.context.view_layer.update()

    with tempfile.TemporaryDirectory() as directory:
        # Only Child A selected: the unselected parent is dropped and the child
        # is re-rooted with its world transform; Child B is not exported.
        _select(child_a)
        document = _export(os.path.join(directory, "child.gltf"), export_selected=True)
        assert _node_names(document) == ["Child A"], _node_names(document)
        node = _node(document, "Child A")
        assert "mesh" in node
        _assert_translation(node, child_a.matrix_world.translation)

        # Rig and Child A selected: hierarchy is kept, Child B still filtered.
        _select(rig, child_a)
        document = _export(os.path.join(directory, "subtree.gltf"), export_selected=True)
        assert set(_node_names(document)) == {"Rig", "Child A"}, _node_names(document)
        child_node = _node(document, "Child A")
        rig_node = _node(document, "Rig")
        child_index = document["nodes"].index(child_node)
        assert rig_node.get("children") == [child_index], rig_node.get("children")
        _assert_translation(child_node, (0.0, 0.0, 3.0))

        # A selected collection-instance empty keeps its instances; the other
        # (unselected) instance is dropped.
        _select(instances[0])
        document = _export(os.path.join(directory, "instance.gltf"), export_selected=True)
        assert _node_names(document) == ["Instance mesh"], _node_names(document)
        _assert_translation(
            _node(document, "Instance mesh"), instances[0].matrix_world.translation,
        )

        # Nothing selected: empty export, no nodes and no meshes.
        _select()
        document = _export(os.path.join(directory, "empty.gltf"), export_selected=True)
        assert not document.get("nodes"), document.get("nodes")
        assert not document.get("meshes"), document.get("meshes")

        # Off (default): every object is exported regardless of selection.
        document = _export(os.path.join(directory, "all.gltf"))
        names = set(_node_names(document))
        assert {"Rig", "Child A", "Child B", "Lone"} <= names, names


if __name__ == "__main__":
    main()
