"""Regression check: meshes that share every instance transform are each instanced.

Two meshes in one collection, instanced together, land on the same transforms and
are written as one group. EXT_mesh_gpu_instancing instances only its node's own
mesh, so the extension must sit on each mesh node, not on a parent without one,
where readers ignore it and draw the meshes once.

Run with Blender in background after enabling blender_gltf:
  blender --background --python tests/grouped_instancing.py
"""

import json
import os
import struct
import tempfile

import bpy

from blender_gltf.export.converter import convert_location
from blender_gltf.exporter import ExportSettings, GltfExporter

EXT = "EXT_mesh_gpu_instancing"
PLACES = ((2.0, 3.0, 0.0), (-4.0, 1.0, 0.0), (5.0, -6.0, 1.0))


def _triangle(name, offset):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(offset, 0, 0), (offset + 1, 0, 0), (offset, 1, 0)], [], [(0, 1, 2)])
    return bpy.data.objects.new(name, mesh)


def main():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    part = bpy.data.collections.new("Roof and ridge")
    part.objects.link(_triangle("Roof", 0.0))
    part.objects.link(_triangle("Ridge", 2.0))
    for index, location in enumerate(PLACES):
        empty = bpy.data.objects.new(f"House {index}", None)
        empty.instance_type = "COLLECTION"
        empty.instance_collection = part
        empty.location = location
        bpy.context.scene.collection.objects.link(empty)
    bpy.context.view_layer.update()

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "grouped.gltf")
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
        )
        GltfExporter(bpy.context, settings).export()
        with open(path, "r", encoding="utf8") as handle:
            document = json.load(handle)
        with open(os.path.join(directory, document["buffers"][0]["uri"]), "rb") as handle:
            binary = handle.read()

    nodes = document["nodes"]
    instanced = [node for node in nodes if EXT in node.get("extensions", {})]

    # The extension never sits on a node without a mesh.
    assert all("mesh" in node for node in instanced), instanced

    # Each mesh is instanced, both from the same accessors.
    names = {document["meshes"][node["mesh"]]["name"] for node in instanced}
    assert names == {"Roof", "Ridge"}, names
    attributes = [node["extensions"][EXT]["attributes"] for node in instanced]
    assert attributes[0] == attributes[1], attributes

    # Every copy is where its house is (no node transforms above the mesh nodes).
    def has_transform(node):
        return any(key in node for key in ("translation", "rotation", "scale", "matrix"))
    assert not any(has_transform(node) for node in nodes), nodes
    accessor = document["accessors"][attributes[0]["TRANSLATION"]]
    view = document["bufferViews"][accessor["bufferView"]]
    start = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    stride = view.get("byteStride", 12)
    found = sorted(struct.unpack_from("<3f", binary, start + i * stride) for i in range(accessor["count"]))
    wanted = sorted(tuple(convert_location(place)) for place in PLACES)
    assert len(found) == len(wanted), found
    for got, want in zip(found, wanted):
        assert max(abs(a - b) for a, b in zip(got, want)) < 1e-5, (got, want)


if __name__ == "__main__":
    main()
