"""Blender integration check for CUSTOM_lights_area.

Run with Blender in background after enabling blender_gltf:
  blender --background --python tests/area_light_roundtrip.py
"""

import json
import math
import os
import tempfile

import bpy
from mathutils import Euler, Vector


def close(a, b, epsilon=1e-5):
    return abs(a - b) <= epsilon


def main():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    data = bpy.data.lights.new("Scaled ellipse", "AREA")
    data.shape = "ELLIPSE"
    data.size = 4.2
    data.size_y = 1.75
    data.energy = 1000.0
    data.color = (0.8, 0.5, 0.25)
    data.spread = 1.2
    source = bpy.data.objects.new("Scaled ellipse", data)
    bpy.context.scene.collection.objects.link(source)
    source.location = (2.0, -3.0, 5.0)
    source.rotation_euler = Euler((0.3, -0.7, 1.1), "XYZ")
    # Mirroring one footprint axis must not reverse the light's local -Z emission.
    source.scale = (-2.0, 0.5, 1.0)
    bpy.context.view_layer.objects.active = source
    source.select_set(True)
    rectangle_data = bpy.data.lights.new("Scaled rectangle", "AREA")
    rectangle_data.shape = "RECTANGLE"
    rectangle_data.size = 6.0
    rectangle_data.size_y = 2.0
    rectangle_data.energy = 500.0
    rectangle = bpy.data.objects.new("Scaled rectangle", rectangle_data)
    bpy.context.scene.collection.objects.link(rectangle)
    rectangle.location = (-1.0, 4.0, 2.0)
    rectangle.rotation_euler = Euler((-0.2, 0.4, -0.8), "XYZ")
    rectangle.scale = (0.75, 1.5, 1.0)
    bpy.context.view_layer.update()
    before = {source.name: source.matrix_world.copy(), rectangle.name: rectangle.matrix_world.copy()}

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "area.gltf")
        bpy.ops.export_scene.gltf_custom(filepath=path, export_format="GLTF_SEPARATE")
        with open(path, "r", encoding="utf8") as handle:
            document = json.load(handle)

        assert "CUSTOM_lights_area" in document["extensionsUsed"]
        areas = document["extensions"]["CUSTOM_lights_area"]["lights"]
        area = next(item for item in areas if item["name"] == "Scaled ellipse")
        assert area["shape"] == "ellipse"
        assert close(area["width"], 4.2) and close(area["height"], 1.75)
        assert close(area["powerWatts"], 1000.0)
        assert all(close(a, b) for a, b in zip(area["color"], (0.8, 0.5, 0.25)))
        assert close(area["spread"], 1.2)

        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.delete(use_global=False)
        bpy.ops.import_scene.gltf_custom(filepath=path)
        restored_lights = {obj.name: obj for obj in bpy.context.scene.objects if obj.type == "LIGHT"}
        restored = restored_lights["Scaled ellipse"]
        assert restored.data.type == "AREA" and restored.data.shape == "ELLIPSE"
        assert close(restored.data.size, 4.2) and close(restored.data.size_y, 1.75)
        assert close(restored.data.energy, 1000.0) and close(restored.data.spread, 1.2)
        for column in range(4):
            delta = (restored.matrix_world.col[column] - before["Scaled ellipse"].col[column]).length
            assert delta < 1e-4, (column, tuple(restored.matrix_world.col[column]),
                                  tuple(before["Scaled ellipse"].col[column]), delta)
        restored_rectangle = restored_lights["Scaled rectangle"]
        assert restored_rectangle.data.shape == "RECTANGLE"
        assert close(restored_rectangle.data.size, 6.0) and close(restored_rectangle.data.size_y, 2.0)
        for column in range(4):
            assert (restored_rectangle.matrix_world.col[column] - before["Scaled rectangle"].col[column]).length < 1e-4


if __name__ == "__main__":
    main()
