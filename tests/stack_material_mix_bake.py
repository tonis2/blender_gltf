"""Regression check: a mixed BSDF Stack color is baked, not flattened."""

import json
import os
import tempfile

import bpy

from blender_gltf.exporter import ExportSettings, GltfExporter


def main():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    mesh = bpy.data.meshes.new("Stack mix bake mesh")
    mesh.from_pydata(
        [(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)],
        [],
        [(0, 1, 2, 3)],
    )
    uv = mesh.uv_layers.new(name="UVMap")
    for loop, value in zip(uv.data, ((0, 0), (1, 0), (1, 1), (0, 1))):
        loop.uv = value
    obj = bpy.data.objects.new("Stack mix bake object", mesh)
    bpy.context.scene.collection.objects.link(obj)

    image = bpy.data.images.new("Stack mix overlay", 2, 2, alpha=True)
    image.pixels = [
        0.05, 0.4, 0.1, 0.0,
        0.05, 0.4, 0.1, 1.0,
        0.05, 0.4, 0.1, 0.0,
        0.05, 0.4, 0.1, 1.0,
    ]

    material = bpy.data.materials.new("Mixed stack material")
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
    texture = nodes.new("ShaderNodeTexImage")
    texture.image = image
    mix = nodes.new("ShaderNodeMixRGB")
    links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    links.new(ramp.outputs["Color"], mix.inputs[1])
    links.new(texture.outputs["Color"], mix.inputs[2])
    links.new(texture.outputs["Alpha"], mix.inputs["Fac"])
    links.new(mix.outputs["Color"], stack.inputs["Color"])
    links.new(stack.outputs["BSDF"], output.inputs["Surface"])
    obj.data.materials.append(material)

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "stack_mix_bake.gltf")
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
        texture_info = exported["pbrMetallicRoughness"]["baseColorTexture"]
        exported_texture = document["textures"][texture_info["index"]]
        exported_image = document["images"][exported_texture["source"]]
        assert "l0_color" in exported_image.get("name", ""), exported_image

        loaded = bpy.data.images.load(
            os.path.join(directory, exported_image["uri"]), check_existing=False,
        )
        try:
            pixels = loaded.pixels[:]
            red = pixels[0::4]
            assert max(red) - min(red) > 0.05, "exported color texture is flat"
        finally:
            bpy.data.images.remove(loaded)


if __name__ == "__main__":
    main()
