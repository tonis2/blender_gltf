"""Regression check: sheared collection instances keep shared mesh data."""

import json
import os
import tempfile

import bpy
from mathutils import Euler, Matrix, Quaternion, Vector

from blender_gltf.export.converter import convert_matrix
from blender_gltf.exporter import ExportSettings, GltfExporter


def _node_matrix(node):
    translation = Vector(node.get("translation", (0.0, 0.0, 0.0)))
    rotation = node.get("rotation", (0.0, 0.0, 0.0, 1.0))
    quaternion = Quaternion((rotation[3], rotation[0], rotation[1], rotation[2]))
    scale = Vector(node.get("scale", (1.0, 1.0, 1.0)))
    return Matrix.LocRotScale(translation, quaternion, scale)


def _converted_matrix(matrix):
    values = convert_matrix(matrix)
    return Matrix([
        [values[0], values[4], values[8], values[12]],
        [values[1], values[5], values[9], values[13]],
        [values[2], values[6], values[10], values[14]],
        [values[3], values[7], values[11], values[15]],
    ])


def main():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    source_collection = bpy.data.collections.new("Shear source collection")
    mesh = bpy.data.meshes.new("Shared triangle")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    source = bpy.data.objects.new("Shared triangle", mesh)
    source.rotation_euler = Euler((0.3, 0.5, 0.7), "XYZ")
    source_collection.objects.link(source)

    for index, location in enumerate(((2, 3, 4), (-3, 1, 2))):
        instance = bpy.data.objects.new(f"Sheared instance {index}", None)
        instance.instance_type = "COLLECTION"
        instance.instance_collection = source_collection
        instance.location = location
        instance.scale = (2.0, 1.0, 0.5)
        bpy.context.scene.collection.objects.link(instance)

    bpy.context.view_layer.update()
    expected = [
        _converted_matrix(instance.matrix_world.copy())
        for instance in bpy.context.evaluated_depsgraph_get().object_instances
        if instance.is_instance and instance.object.original is source
    ]
    assert len(expected) == 2

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "sheared_instances.gltf")
        settings = ExportSettings(
            filepath=path,
            export_format="GLTF_SEPARATE",
            export_animations=False,
            export_materials=False,
            export_physics=False,
            export_particles=False,
            export_interactivity=False,
            export_audio=False,
            export_walkability=False,
            export_environment_map=False,
        )
        GltfExporter(bpy.context, settings).export()
        with open(path, "r", encoding="utf8") as handle:
            document = json.load(handle)

    assert len(document["meshes"]) == 1, document["meshes"]
    assert all("matrix" not in node for node in document["nodes"])

    actual = []
    nodes = document["nodes"]

    def visit(index, parent):
        node = nodes[index]
        world = parent @ _node_matrix(node)
        if "mesh" in node:
            actual.append(world)
        for child in node.get("children", []):
            visit(child, world)

    for root in document["scenes"][document.get("scene", 0)]["nodes"]:
        visit(root, Matrix.Identity(4))

    assert len(actual) == len(expected)
    unmatched = actual[:]
    for wanted in expected:
        best = min(unmatched, key=lambda value: max(
            abs(value[row][column] - wanted[row][column])
            for row in range(4) for column in range(4)
        ))
        error = max(
            abs(best[row][column] - wanted[row][column])
            for row in range(4) for column in range(4)
        )
        assert error < 1e-5, error
        unmatched.remove(best)


if __name__ == "__main__":
    main()
