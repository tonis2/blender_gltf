"""Regression check: a zero-strength Blender Normal Map is not exported."""

import json
import os
import tempfile

import bpy

from blender_gltf.exporter import ExportSettings, GltfExporter


def main():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    mesh = bpy.data.meshes.new("Disabled normal mesh")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    uv = mesh.uv_layers.new(name="UVMap")
    for loop, value in zip(uv.data, ((0, 0), (1, 0), (0, 1))):
        loop.uv = value
    obj = bpy.data.objects.new("Disabled normal object", mesh)
    bpy.context.scene.collection.objects.link(obj)

    material = bpy.data.materials.new("Disabled normal material")
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    principled = next(node for node in nodes if node.type == "BSDF_PRINCIPLED")
    normal = nodes.new("ShaderNodeNormalMap")
    normal.inputs["Strength"].default_value = 0.0
    links.new(normal.outputs["Normal"], principled.inputs["Normal"])
    obj.data.materials.append(material)

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "disabled_normal.gltf")
        settings = ExportSettings(
            filepath=path,
            export_format="GLTF_SEPARATE",
            image_format="PNG",
            bake_materials=True,
            export_animations=False,
            export_physics=False,
            export_particles=False,
            export_interactivity=False,
            export_audio=False,
            export_environment_map=False,
        )
        GltfExporter(bpy.context, settings).export()
        with open(path, "r", encoding="utf8") as handle:
            document = json.load(handle)
        exported = next(m for m in document["materials"] if m["name"] == material.name)
        assert "normalTexture" not in exported, exported
        assert not any("normal" in image.get("name", "").lower()
                       for image in document.get("images", [])), document.get("images")


if __name__ == "__main__":
    main()
