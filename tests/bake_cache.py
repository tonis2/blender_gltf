"""Baked textures are kept in the .blend and reused until what they were baked
from changes; the export's own temporary images are gone before its KTX2
encodes finish.

Run: blender --background --factory-startup --python-expr "import sys;
sys.path.insert(0, '<addons dir>'); import blender_gltf; blender_gltf.register()"
--python tests/bake_cache.py
"""

import json
import os
import struct
import tempfile

import bpy
import numpy as np

from blender_gltf.export import bake_cache, texture as texture_module
from blender_gltf.export.material import MaterialExporter
from blender_gltf.export.texture import TEMP_IMAGE_PROP
from blender_gltf.exporter import ExportSettings, GltfExporter

BAKES = []
_run_bake = MaterialExporter._run_bake


def _counting_bake(bake_type):
    BAKES.append(bake_type)
    _run_bake(bake_type)


MaterialExporter._run_bake = staticmethod(_counting_bake)


def settings(path, **overrides):
    values = dict(
        filepath=path,
        export_format="GLB",
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
    values.update(overrides)
    return ExportSettings(**values)


def glb_document(path):
    with open(path, "rb") as handle:
        data = handle.read()
    length = struct.unpack_from("<I", data, 12)[0]
    return json.loads(data[20:20 + length])


def export(path, **overrides):
    """Export and return (glTF JSON, textures baked, bakes reused)."""
    BAKES.clear()
    exporter = GltfExporter(bpy.context, settings(path, **overrides))
    exporter.export()
    materials = exporter.material_exporter
    assert materials.bakes_made == len(BAKES), (materials.bakes_made, BAKES)
    return glb_document(path), materials.bakes_made, materials.bakes_reused


def reuse_in_new_session(blend, path):
    """(baked, reused) for the same export run by a fresh Blender."""
    import subprocess

    addons = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    script = (
        "import sys; sys.path.insert(0, %r); import blender_gltf; blender_gltf.register()\n"
        "from blender_gltf.exporter import ExportSettings, GltfExporter\n"
        "e = GltfExporter(bpy.context, ExportSettings(filepath=%r, export_format='GLB', "
        "image_format='PNG', bake_materials=True, bake_resolution='64', "
        "export_animations=False, export_physics=False, export_particles=False, "
        "export_interactivity=False, export_audio=False, export_environment_map=False))\n"
        "e.export()\n"
        "print('SESSION', e.material_exporter.bakes_made, e.material_exporter.bakes_reused)\n"
    ) % (addons, path)
    env = dict(os.environ, PYTHONHASHSEED="12345")
    result = subprocess.run(
        [bpy.app.binary_path, "--background", "--factory-startup", blend,
         "--python-expr", "import bpy\n" + script],
        capture_output=True, text=True, env=env, timeout=300,
    )
    for line in result.stdout.splitlines():
        if line.startswith("SESSION"):
            return tuple(int(v) for v in line.split()[1:])
    raise AssertionError(result.stdout[-2000:] + result.stderr[-2000:])


def build_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    mesh = bpy.data.meshes.new("Bake cache mesh")
    mesh.from_pydata([(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)], [], [(0, 1, 2, 3)])
    uv = mesh.uv_layers.new(name="UVMap")
    for loop, value in zip(uv.data, ((0, 0), (1, 0), (1, 1), (0, 1))):
        loop.uv = value
    obj = bpy.data.objects.new("Bake cache object", mesh)
    bpy.context.scene.collection.objects.link(obj)

    material = bpy.data.materials.new("Procedural cache material")
    material.use_nodes = True
    tree = material.node_tree
    principled = next(node for node in tree.nodes if node.type == "BSDF_PRINCIPLED")
    noise = tree.nodes.new("ShaderNodeTexNoise")
    noise.name = "Noise"
    ramp = tree.nodes.new("ShaderNodeValToRGB")
    tree.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    tree.links.new(ramp.outputs["Color"], principled.inputs["Base Color"])
    tree.links.new(noise.outputs["Fac"], principled.inputs["Roughness"])
    obj.data.materials.append(material)
    return obj, material


def build_packed_material():
    """A second object whose separate roughness / metallic images pack into a
    temporary ORM image."""
    mesh = bpy.data.meshes.new("Pack mesh")
    mesh.from_pydata([(2, -1, 0), (4, -1, 0), (4, 1, 0), (2, 1, 0)], [], [(0, 1, 2, 3)])
    mesh.uv_layers.new(name="UVMap")
    obj = bpy.data.objects.new("Pack object", mesh)
    bpy.context.scene.collection.objects.link(obj)
    material = bpy.data.materials.new("Pack material")
    material.use_nodes = True
    tree = material.node_tree
    principled = next(node for node in tree.nodes if node.type == "BSDF_PRINCIPLED")
    for socket, value in (("Roughness", 0.25), ("Metallic", 0.75)):
        image = bpy.data.images.new(f"Pack {socket}", 4, 4, alpha=False)
        image.colorspace_settings.name = "Non-Color"
        image.generated_color = (value, value, value, 1.0)
        image.pack()
        node = tree.nodes.new("ShaderNodeTexImage")
        node.image = image
        tree.links.new(node.outputs["Color"], principled.inputs[socket])
    obj.data.materials.append(material)


def check_no_leftovers(before):
    new = [image.name for image in bpy.data.images
           if image.name_full not in before and not image.get(bake_cache.BAKE_ID_PROP)]
    assert not new, f"export left images behind: {new}"
    dirty = [image.name for image in bpy.data.images if image.is_dirty]
    assert not dirty, f"saving would ask about modified images: {dirty}"
    assert not [m.name for m in bpy.data.materials if m.name.startswith("__gltf_baked__")]


def check_kept():
    kept = bake_cache.kept_images()
    assert kept, "nothing was kept"
    ids = [image[bake_cache.BAKE_ID_PROP] for image in kept]
    assert len(ids) == len(set(ids)), f"a bake is kept twice: {ids}"
    for image in kept:
        assert image.packed_file is not None and image.use_fake_user, image.name
        assert not image.is_dirty, image.name
    return kept


def main():
    obj, material = build_scene()
    build_packed_material()
    before = {image.name_full for image in bpy.data.images}

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "cache.glb")

        document, made, reused = export(path)
        assert made == 2 and reused == 0, (made, reused)  # base colour + roughness
        assert "baseColorTexture" in document["materials"][0]["pbrMetallicRoughness"]
        kept = check_kept()
        assert len(kept) == 2, [image.name for image in kept]
        check_no_leftovers(before)

        # Unchanged: nothing bakes again, and the file still carries the bakes.
        document, made, reused = export(path)
        assert made == 0 and reused == 2, (made, reused)
        names = [image.get("name", "") for image in document["images"]]
        assert any(name.endswith("_basecolor") for name in names), names
        check_no_leftovers(before)

        # Layout, selection and the playhead do not count as changes.
        noise = material.node_tree.nodes["Noise"]
        noise.location = (noise.location.x + 300, noise.location.y)
        noise.select = not noise.select
        obj.select_set(True)
        bpy.context.scene.frame_set(7)
        _, made, _ = export(path)
        assert made == 0, "moving a node or the playhead re-baked"

        # A changed node value bakes again and replaces the old images.
        noise.inputs["Scale"].default_value += 1.0
        _, made, reused = export(path)
        assert made == 2 and reused == 0, (made, reused)
        assert len(check_kept()) == 2

        # So does a changed mesh.
        obj.data.vertices[0].co.z += 0.5
        _, made, _ = export(path)
        assert made == 2, made

        # And a changed bake resolution.
        _, made, _ = export(path, bake_resolution="64")
        assert made == 2, made
        assert all(tuple(image.size) == (64, 64) for image in check_kept())

        # Kept bakes survive saving and reopening the file, in this session
        # and in a new one (where Python hashes strings differently).
        blend = os.path.join(directory, "cache.blend")
        bpy.ops.wm.save_as_mainfile(filepath=blend)
        assert reuse_in_new_session(blend, path) == (0, 2)
        bpy.ops.wm.open_mainfile(filepath=blend)
        before = {image.name_full for image in bpy.data.images}
        _, made, reused = export(path, bake_resolution="64")
        assert made == 0 and reused == 2, (made, reused)
        check_no_leftovers(before)

        # The temporary ORM image is gone as soon as gathering ends, before
        # any KTX2 encode could finish.
        exporter = GltfExporter(bpy.context, settings(path))
        exporter.build_begin()
        temps = [image.name for image in bpy.data.images if image.get(TEMP_IMAGE_PROP)]
        assert not temps, f"temporary images outlive gathering: {temps}"
        exporter.write_output(*exporter.build_finish())
        packed = [i for i in glb_document(path)["images"] if i.get("name", "").endswith("_ORM")]
        assert packed, "the packed ORM texture is missing"

        # A failed KTX2 encode of a temporary image falls back to the pixels
        # read before the image was removed.
        def failing_encode(*args, **kwargs):
            raise RuntimeError("encoder unavailable")

        real_encode = texture_module.ktx_lib.encode_rgba
        texture_module.ktx_lib.encode_rgba = failing_encode
        try:
            document, _, _ = export(path, image_format="KTX2")
        finally:
            texture_module.ktx_lib.encode_rgba = real_encode
        orm = [i for i in document["images"] if i.get("name", "").endswith("_ORM")]
        assert orm and orm[0].get("mimeType") == "image/png", orm

        assert bpy.ops.gltf_custom.clear_bakes() == {"FINISHED"}
        assert not bake_cache.kept_images()
        _, made, _ = export(path)
        assert made == 2, made

    # The PNG writer behind that fallback round-trips through Blender.
    with tempfile.TemporaryDirectory() as directory:
        rgba = np.arange(3 * 2 * 4, dtype=np.uint8) * 10
        png = os.path.join(directory, "fallback.png")
        with open(png, "wb") as handle:
            handle.write(texture_module.png_from_rgba(rgba.tobytes(), 3, 2))
        image = bpy.data.images.load(png)
        pixels = np.round(np.array(image.pixels[:]) * 255).astype(np.uint8)
        # Blender's rows run bottom-up, PNG's top-down.
        assert (pixels.reshape(2, 12)[::-1].ravel() == rgba).all()

    print("bake_cache: OK")


if __name__ == "__main__":
    main()
