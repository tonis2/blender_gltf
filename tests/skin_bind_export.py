"""Check that an exported skin reproduces Blender's deformation wherever the
armature and its mesh stand.

Run with Blender:
  blender --background --factory-startup --python tests/skin_bind_export.py
"""

import json
import math
import os
import struct
import sys
import tempfile

import bpy
import numpy as np
from mathutils import Euler, Matrix, Quaternion

try:
    import blender_gltf
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    import blender_gltf
if not hasattr(bpy.types.Scene, "gltf_cutscenes"):
    blender_gltf.register()

from blender_gltf.exporter import ExportSettings, GltfExporter

COMPONENTS = {5121: "B", 5123: "H", 5125: "I", 5126: "f"}
WIDTHS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def export(path):
    settings = ExportSettings(
        filepath=path,
        export_format="GLB",
        export_materials=False,
        export_physics=False,
        export_particles=False,
        export_interactivity=False,
        export_audio=False,
        export_environment_map=False,
    )
    GltfExporter(bpy.context, settings).export()
    with open(path, "rb") as handle:
        data = handle.read()
    json_length = struct.unpack_from("<I", data, 12)[0]
    document = json.loads(data[20:20 + json_length])
    return document, data[20 + json_length + 8:]


def accessor(document, binary, index):
    acc = document["accessors"][index]
    view = document["bufferViews"][acc["bufferView"]]
    width = WIDTHS[acc["type"]]
    code = COMPONENTS[acc["componentType"]]
    size = struct.calcsize(code) * width
    stride = view.get("byteStride", size)
    start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    rows = [
        struct.unpack_from(f"<{width}{code}", binary, start + i * stride)
        for i in range(acc["count"])
    ]
    return np.array(rows, dtype=np.float64)


def node_matrix(node):
    scale = Matrix.Diagonal((*node.get("scale", (1.0, 1.0, 1.0)), 1.0))
    x, y, z, w = node.get("rotation", (0.0, 0.0, 0.0, 1.0))
    rotation = Quaternion((w, x, y, z)).to_matrix().to_4x4()
    translation = Matrix.Translation(node.get("translation", (0.0, 0.0, 0.0)))
    return np.array(translation @ rotation @ scale)


def world_matrices(document):
    nodes = document["nodes"]
    world = [None] * len(nodes)

    def walk(index, parent):
        world[index] = parent @ node_matrix(nodes[index])
        for child in nodes[index].get("children", []):
            walk(child, world[index])

    for root in document["scenes"][0]["nodes"]:
        walk(root, np.eye(4))
    return world


def skinned_positions(document, binary, mesh_name):
    """Where glTF skinning puts the mesh's vertices, in Blender's axes."""
    node = next(n for n in document["nodes"] if n.get("name") == mesh_name)
    primitive = document["meshes"][node["mesh"]]["primitives"][0]
    attributes = primitive["attributes"]
    positions = accessor(document, binary, attributes["POSITION"])
    joints = accessor(document, binary, attributes["JOINTS_0"]).astype(int)
    weights = accessor(document, binary, attributes["WEIGHTS_0"])
    skin = document["skins"][node["skin"]]
    inverse_bind = accessor(document, binary, skin["inverseBindMatrices"]).reshape(-1, 4, 4)
    world = world_matrices(document)
    palette = [
        world[joint] @ inverse_bind[i].T  # the accessor is column-major
        for i, joint in enumerate(skin["joints"])
    ]
    out = np.zeros((len(positions), 3))
    for v, position in enumerate(positions):
        point = np.array([*position, 1.0])
        total = np.zeros(4)
        for joint, weight in zip(joints[v], weights[v]):
            total += weight * (palette[joint] @ point)
        out[v] = total[:3]
    # glTF (x, y, z) is Blender (x, -z, y)
    return np.stack([out[:, 0], -out[:, 2], out[:, 1]], axis=1)


def blender_positions(obj):
    deps = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(deps)
    mesh = evaluated.to_mesh()
    points = np.array([tuple(evaluated.matrix_world @ v.co) for v in mesh.vertices])
    evaluated.to_mesh_clear()
    return points


def assert_same_points(actual, expected, what):
    """Each expected point has an exported twin; the export may split vertices."""
    assert len(actual) >= len(expected), what
    for point in expected:
        nearest = np.min(np.linalg.norm(actual - point, axis=1))
        assert nearest < 1e-4, f"{what}: {point} is {nearest:.5f} from any exported vertex"
    for point in actual:
        nearest = np.min(np.linalg.norm(expected - point, axis=1))
        assert nearest < 1e-4, f"{what}: exported {point} is {nearest:.5f} from the mesh"


def make_rig(name):
    data = bpy.data.armatures.new(name)
    armature = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(armature)
    bpy.context.view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode="EDIT")
    lower = data.edit_bones.new("lower")
    lower.head, lower.tail = (0, 0, 0), (0, 0, 1)
    upper = data.edit_bones.new("upper")
    upper.head, upper.tail = (0, 0, 1), (0.2, 0, 2)
    upper.parent = lower
    bpy.ops.object.mode_set(mode="OBJECT")
    return armature


def make_skin(name, armature):
    mesh = bpy.data.meshes.new(name)
    vertices = [(x, y, z) for z in (0.0, 1.0, 2.0) for x, y in ((-0.2, -0.2), (0.2, -0.2), (0.2, 0.2), (-0.2, 0.2))]
    faces = [(i, i + 1, i + 5, i + 4) for i in (0, 1, 2, 4, 5, 6)]
    mesh.from_pydata(vertices, [], faces)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    lower = obj.vertex_groups.new(name="lower")
    upper = obj.vertex_groups.new(name="upper")
    for index, vertex in enumerate(vertices):
        blend = vertex[2] / 2.0
        lower.add([index], 1.0 - blend, "REPLACE")
        upper.add([index], blend, "REPLACE")
    obj.modifiers.new("Armature", "ARMATURE").object = armature
    return obj


def pose(armature):
    armature.pose.bones["lower"].rotation_mode = "XYZ"
    armature.pose.bones["lower"].rotation_euler = (0.3, 0.1, -0.4)
    armature.pose.bones["upper"].rotation_mode = "XYZ"
    armature.pose.bones["upper"].rotation_euler = (-0.5, 0.2, 0.6)
    armature.pose.bones["upper"].location = (0.0, 0.1, 0.0)


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)

    # A character standing somewhere in a level: moved, turned and scaled,
    # with its mesh parented straight to the armature.
    placed = make_rig("placed")
    placed.location = (-6.6, -4.6, -0.2)
    placed.rotation_euler = Euler((math.radians(-8), math.radians(-10), math.radians(134)))
    placed.scale = (1.9, 1.9, 1.9)
    child = make_skin("placed_skin", placed)
    child.parent = placed

    # A mesh that is not the armature's child and sits offset against it.
    loose = make_rig("loose")
    loose.location = (3.0, 1.0, 0.5)
    loose.rotation_euler = Euler((0.0, 0.0, math.radians(40)))
    apart = make_skin("loose_skin", loose)
    apart.location = (3.3, 0.8, 0.5)
    apart.rotation_euler = Euler((0.0, 0.0, math.radians(15)))
    apart.scale = (1.2, 1.2, 1.2)

    # The plain kit character: everything at the origin.
    plain = make_rig("plain")
    kit = make_skin("plain_skin", plain)
    kit.parent = plain

    with tempfile.TemporaryDirectory() as folder:
        for label in ("rest", "posed"):
            if label == "posed":
                for armature in (placed, loose, plain):
                    pose(armature)
            bpy.context.view_layer.update()
            expected = {obj.name: blender_positions(obj) for obj in (child, apart, kit)}

            # The file holds the rest pose; put the pose on its joint nodes.
            document, binary = export(os.path.join(folder, f"{label}.glb"))
            if label == "posed":
                from blender_gltf.export.converter import trs_to_node_fields
                for armature in (placed, loose, plain):
                    for bone in armature.pose.bones:
                        local = bone.matrix
                        if bone.parent is not None:
                            local = bone.parent.matrix.inverted() @ bone.matrix
                        translation, rotation, scale = trs_to_node_fields(local)
                        node = next(
                            n for n in document["nodes"]
                            if n.get("name") == bone.name and _under(document, armature.name, n)
                        )
                        node.pop("translation", None), node.pop("rotation", None), node.pop("scale", None)
                        if translation is not None:
                            node["translation"] = translation
                        if rotation is not None:
                            node["rotation"] = rotation
                        if scale is not None:
                            node["scale"] = scale
            for obj in (child, apart, kit):
                actual = skinned_positions(document, binary, obj.name)
                assert_same_points(actual, expected[obj.name], f"{obj.name} {label}")

    print("skin_bind_export: OK")


def _under(document, root_name, node):
    """Whether `node` is in the subtree of the node named root_name."""
    nodes = document["nodes"]
    root = next(n for n in nodes if n.get("name") == root_name)
    stack = list(root.get("children", []))
    while stack:
        index = stack.pop()
        if nodes[index] is node:
            return True
        stack.extend(nodes[index].get("children", []))
    return False


if __name__ == "__main__":
    main()
