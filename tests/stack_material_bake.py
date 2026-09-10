"""Regression check: procedural BSDF Stack inputs bake to glTF textures."""

import json
import os
import tempfile

import bpy

from blender_gltf.exporter import ExportSettings, GltfExporter


def main():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    mesh = bpy.data.meshes.new("Stack bake mesh")
    mesh.from_pydata(
        [(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)],
        [],
        [(0, 1, 2, 3)],
    )
    uv = mesh.uv_layers.new(name="UVMap")
    for loop, value in zip(uv.data, ((0, 0), (1, 0), (1, 1), (0, 1))):
        loop.uv = value
    obj = bpy.data.objects.new("Stack bake object", mesh)
    bpy.context.scene.collection.objects.link(obj)

    material = bpy.data.materials.new("Procedural stack material")
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    for node in list(nodes):
        if node.type != "OUTPUT_MATERIAL":
            nodes.remove(node)
    output = next(node for node in nodes if node.type == "OUTPUT_MATERIAL")

    stack = nodes.new("BSDFStackNodeType")
    noise = nodes.new("ShaderNodeTexNoise")
    ramp = nodes.new("ShaderNodeValToRGB")
    bump = nodes.new("ShaderNodeBump")
    links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    links.new(noise.outputs["Fac"], bump.inputs["Height"])
    links.new(ramp.outputs["Color"], stack.inputs["Color"])
    links.new(bump.outputs["Normal"], stack.inputs["Normal"])
    links.new(stack.outputs["BSDF"], output.inputs["Surface"])
    obj.data.materials.append(material)

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "stack_bake.gltf")
        settings = ExportSettings(
            filepath=path,
            export_format="GLTF_SEPARATE",
            image_format="PNG",
            bake_materials=True,
            bake_resolution="32",
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
        assert "baseColorTexture" in exported["pbrMetallicRoughness"], exported
        assert "normalTexture" in exported, exported
        names = [image.get("name", "") for image in document.get("images", [])]
        assert any("l0_color" in name for name in names), names
        assert any(name.endswith("_normal") for name in names), names

        normal_texture = document["textures"][exported["normalTexture"]["index"]]
        normal_image = document["images"][normal_texture["source"]]
        loaded = bpy.data.images.load(
            os.path.join(directory, normal_image["uri"]), check_existing=False,
        )
        try:
            pixels = loaded.pixels[:]
            assert max(pixels[2::4]) > 0.9, "exported normal texture is blank"
        finally:
            bpy.data.images.remove(loaded)


if __name__ == "__main__":
    main()
