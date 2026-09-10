"""Regression check for automatic ORM packing and import round-trip."""

import json
import os
import tempfile

import bpy

from blender_gltf.exporter import ExportSettings, GltfExporter
from blender_gltf.importer import ImportSettings, GltfImporter
from blender_gltf import ktx_lib


def make_image(name, width, height, values):
    image = bpy.data.images.new(name, width, height, alpha=True)
    image.colorspace_settings.name = "Non-Color"
    pixels = []
    for value in values:
        pixels.extend((value, value, value, 1.0))
    image.pixels.foreach_set(pixels)
    image.update()
    return image


def export_settings(path, image_format="PNG"):
    return ExportSettings(
        filepath=path,
        export_format="GLTF_SEPARATE",
        image_format=image_format,
        pack_pbr_textures=True,
        export_animations=False,
        export_physics=False,
        export_particles=False,
        export_interactivity=False,
        export_audio=False,
        export_environment_map=False,
    )


def load_document(path):
    with open(path, "r", encoding="utf8") as handle:
        return json.load(handle)


def assert_packed(document, directory, expected_name):
    material = next(item for item in document["materials"] if item["name"] == expected_name)
    pbr = material["pbrMetallicRoughness"]
    mr_index = pbr["metallicRoughnessTexture"]["index"]
    assert "metallicFactor" not in pbr, pbr
    assert "roughnessFactor" not in pbr, pbr
    assert material["occlusionTexture"]["index"] == mr_index, material
    assert len(document["images"]) == 1, document["images"]

    texture = document["textures"][mr_index]
    image = document["images"][texture["source"]]
    assert "_ORM" in image["name"], image
    loaded = bpy.data.images.load(
        os.path.join(directory, image["uri"]), check_existing=False,
    )
    try:
        assert tuple(loaded.size) == (2, 2), loaded.size[:]
        pixels = loaded.pixels[:]
        red = pixels[0::4]
        green = pixels[1::4]
        blue = pixels[2::4]
        assert max(abs(a - b) for a, b in zip(red, (0.9, 0.5, 0.9, 0.5))) < 0.01, red
        assert max(abs(a - b) for a, b in zip(green, (0.1, 0.2, 0.3, 0.4))) < 0.01, green
        assert max(abs(value - 0.75) for value in blue) < 0.01, blue
    finally:
        bpy.data.images.remove(loaded)


def main():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    mesh = bpy.data.meshes.new("PBR pack mesh")
    mesh.from_pydata(
        [(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)],
        [],
        [(0, 1, 2, 3)],
    )
    uv = mesh.uv_layers.new(name="UVMap")
    for loop, value in zip(uv.data, ((0, 0), (1, 0), (1, 1), (0, 1))):
        loop.uv = value
    obj = bpy.data.objects.new("PBR pack object", mesh)
    bpy.context.scene.collection.objects.link(obj)

    material = bpy.data.materials.new("PBR pack material")
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    principled = next(node for node in nodes if node.type == "BSDF_PRINCIPLED")

    roughness = make_image("Separate roughness", 2, 2, (0.1, 0.2, 0.3, 0.4))
    metallic = make_image("Separate metallic", 1, 1, (0.75,))
    occlusion = make_image("Separate occlusion", 2, 1, (0.9, 0.5))
    rough_node = nodes.new("ShaderNodeTexImage")
    rough_node.image = roughness
    metal_node = nodes.new("ShaderNodeTexImage")
    metal_node.image = metallic
    links.new(rough_node.outputs["Color"], principled.inputs["Roughness"])
    links.new(metal_node.outputs["Color"], principled.inputs["Metallic"])
    material.gltf_props.occlusion_image = occlusion
    material.gltf_props.occlusion_channel = "R"
    obj.data.materials.append(material)

    with tempfile.TemporaryDirectory() as directory:
        first_path = os.path.join(directory, "packed.gltf")
        GltfExporter(bpy.context, export_settings(first_path)).export()
        first_document = load_document(first_path)
        assert_packed(first_document, directory, material.name)

        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.delete(use_global=False)
        bpy.data.materials.remove(material)

        GltfImporter(
            bpy.context,
            ImportSettings(
                filepath=first_path,
                import_animations=False,
                import_physics=False,
                import_particles=False,
                import_interactivity=False,
                import_audio=False,
            ),
        ).import_file()

        imported = bpy.data.materials.get("PBR pack material")
        assert imported is not None
        assert imported.gltf_props.packed_r_channel == "OCCLUSION"
        assert imported.gltf_props.occlusion_image is not None
        imported_principled = next(
            node for node in imported.node_tree.nodes if node.type == "BSDF_PRINCIPLED"
        )
        separate = next(
            node for node in imported.node_tree.nodes if node.type == "SEPARATE_COLOR"
        )
        rough_link = imported_principled.inputs["Roughness"].links[0]
        metal_link = imported_principled.inputs["Metallic"].links[0]
        assert rough_link.from_node == separate and rough_link.from_socket.name == "Green"
        assert metal_link.from_node == separate and metal_link.from_socket.name == "Blue"

        second_path = os.path.join(directory, "repacked.gltf")
        GltfExporter(bpy.context, export_settings(second_path)).export()
        assert_packed(load_document(second_path), directory, imported.name)

        if ktx_lib.is_available():
            ktx_path = os.path.join(directory, "packed_ktx.gltf")
            GltfExporter(
                bpy.context, export_settings(ktx_path, image_format="KTX2"),
            ).export()
            ktx_document = load_document(ktx_path)
            ktx_pbr = ktx_document["materials"][0]["pbrMetallicRoughness"]
            ktx_texture = ktx_document["textures"][
                ktx_pbr["metallicRoughnessTexture"]["index"]
            ]
            assert "KHR_texture_basisu" in ktx_texture["extensions"], ktx_texture
            assert len(ktx_document["images"]) == 1, ktx_document["images"]

            bpy.ops.object.select_all(action="SELECT")
            bpy.ops.object.delete(use_global=False)
            bpy.data.materials.remove(imported)
            GltfImporter(
                bpy.context,
                ImportSettings(
                    filepath=ktx_path,
                    import_animations=False,
                    import_physics=False,
                    import_particles=False,
                    import_interactivity=False,
                    import_audio=False,
                ),
            ).import_file()
            ktx_material = bpy.data.materials.get("PBR pack material")
            assert ktx_material is not None
            assert ktx_material.gltf_props.packed_r_channel == "OCCLUSION"
            assert tuple(ktx_material.gltf_props.occlusion_image.size) == (2, 2)

            bpy.ops.object.select_all(action="SELECT")
            bpy.ops.object.delete(use_global=False)
            bpy.data.materials.remove(ktx_material)
        else:
            bpy.ops.object.select_all(action="SELECT")
            bpy.ops.object.delete(use_global=False)
            bpy.data.materials.remove(imported)

        stack_document = load_document(first_path)
        stack_material = stack_document["materials"][0]
        stack_material.setdefault("extensions", {})["CUSTOM_materials_layers"] = {
            "layers": [{"name": "Overlay"}],
        }
        used = stack_document.setdefault("extensionsUsed", [])
        if "CUSTOM_materials_layers" not in used:
            used.append("CUSTOM_materials_layers")
        stack_path = os.path.join(directory, "packed_stack.gltf")
        with open(stack_path, "w", encoding="utf8") as handle:
            json.dump(stack_document, handle)

        GltfImporter(
            bpy.context,
            ImportSettings(
                filepath=stack_path,
                import_animations=False,
                import_physics=False,
                import_particles=False,
                import_interactivity=False,
                import_audio=False,
            ),
        ).import_file()
        layered = bpy.data.materials.get("PBR pack material")
        assert layered is not None
        assert layered.gltf_props.packed_r_channel == "OCCLUSION"
        assert layered.gltf_props.occlusion_image is not None
        assert any(
            node.bl_idname == "BSDFStackNodeType" for node in layered.node_tree.nodes
        )

        layered_path = os.path.join(directory, "repacked_stack.gltf")
        GltfExporter(bpy.context, export_settings(layered_path)).export()
        assert_packed(load_document(layered_path), directory, layered.name)


if __name__ == "__main__":
    main()
