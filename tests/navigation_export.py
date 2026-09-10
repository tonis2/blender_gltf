"""Background integration check for CUSTOM_physics_navigation.

Run with Blender after enabling blender_gltf:
  blender --background --python tests/navigation_export.py
"""

import json
import os
import struct
import tempfile
from contextlib import ExitStack

import bpy
import blender_gltf
from blender_gltf.export.navigation import NavigationExporter, bake, _linked
from blender_gltf.exporter import ExportSettings
from blender_gltf.gltf.buffer import BufferBuilder


def main():
    assert blender_gltf.navigation.NavigationExporter.__module__.endswith("export.navigation")
    dims = (5, 6, 5)
    index = lambda x, y, z: (z * dims[1] + y) * dims[0] + x
    source, target = index(1, 2, 1), index(2, 3, 2)
    incompatible = {source, target, index(2, 1, 1), index(1, 4, 2)}
    assert not _linked(incompatible, dims, 1.0, 1.0, source, 1, 1, 1)
    stepped = {source, target, index(2, 2, 1)}
    assert _linked(stepped, dims, 1.0, 1.0, source, 1, 1, 1)
    try:
        bake(bpy.context, [], profile_name="player", cell_size=float("nan"),
             agent_radius=0.3, agent_height=1.66, step_height=0.5, slope_limit=54.0)
    except ValueError:
        pass
    else:
        raise AssertionError("non-finite navigation setting was accepted")
    try:
        NavigationExporter(BufferBuilder(), ExportSettings(
            export_navigation=True,
        )).gather(bpy.context.scene)
    except ValueError:
        pass
    else:
        raise AssertionError("export accepted a scene with no navigation bake")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    bpy.ops.mesh.primitive_plane_add(size=8.0, location=(0, 0, 0))
    floor = bpy.context.object
    floor.name = "Passive floor"
    bpy.ops.rigidbody.object_add(type="PASSIVE")
    floor.rigid_body.collision_shape = "MESH"

    # A long diagonal wall exercises exact triangle/cell overlap: filling its
    # AABB would erase almost the whole floor instead of only the wall.
    wall_mesh = bpy.data.meshes.new("Diagonal wall mesh")
    wall_mesh.from_pydata(
        [(-1.5, -1.5, 0), (1.5, 1.5, 0), (1.5, 1.5, 2.5), (-1.5, -1.5, 2.5)],
        [], [(0, 1, 2), (0, 2, 3)],
    )
    wall = bpy.data.objects.new("Diagonal wall", wall_mesh)
    bpy.context.scene.collection.objects.link(wall)
    bpy.context.view_layer.objects.active = wall
    wall.select_set(True)
    bpy.ops.rigidbody.object_add(type="PASSIVE")
    wall.rigid_body.collision_shape = "MESH"

    bpy.ops.mesh.primitive_uv_sphere_add(segments=12, ring_count=6, radius=0.6,
                                         location=(-2.5, 1.5, 0.6))
    sphere = bpy.context.object
    sphere.name = "Sphere proxy source"
    bpy.ops.rigidbody.object_add(type="PASSIVE")
    sphere.rigid_body.collision_shape = "SPHERE"

    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(2.5, 1.5, 0.8))
    capsule = bpy.context.object
    capsule.name = "Capsule proxy source"
    capsule.scale.z = 1.6
    bpy.ops.rigidbody.object_add(type="PASSIVE")
    capsule.rigid_body.collision_shape = "CAPSULE"

    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=1, radius=0.7,
                                          location=(2.5, -1.5, 0.7))
    hull = bpy.context.object
    hull.name = "Hull proxy source"
    hull.scale = (1.0, 0.7, 1.3)
    bpy.ops.rigidbody.object_add(type="PASSIVE")
    hull.rigid_body.collision_shape = "CONVEX_HULL"

    bpy.ops.mesh.primitive_cube_add(size=0.5, location=(-2.5, -1.5, 0.25))
    disabled = bpy.context.object
    disabled.name = "Disabled passive collider"
    bpy.ops.rigidbody.object_add(type="PASSIVE")
    disabled.rigid_body.collision_shape = "BOX"
    disabled.rigid_body.enabled = False

    bpy.ops.mesh.primitive_cube_add(size=0.25, location=(0, 3, 0.25))
    compound = bpy.context.object
    compound.name = "Passive compound root"
    bpy.ops.rigidbody.object_add(type="PASSIVE")
    compound.rigid_body.collision_shape = "COMPOUND"
    bpy.ops.mesh.primitive_cube_add(size=0.8, location=(0, 3, 0.4))
    compound_child = bpy.context.object
    compound_child.name = "Active compound child"
    compound_child.parent = compound
    compound_child.matrix_parent_inverse = compound.matrix_world.inverted()
    bpy.ops.rigidbody.object_add(type="ACTIVE")
    compound_child.rigid_body.collision_shape = "BOX"

    bounds = bpy.data.objects.new("Navigation Bounds", None)
    bpy.context.scene.collection.objects.link(bounds)
    bounds.name = "Navigation Bounds"
    bounds.empty_display_type = "CUBE"
    bounds.empty_display_size = 4.0
    bounds.location = (0, 0, 2.0)
    bounds.scale = (1.0, 1.0, 0.5)
    bpy.context.view_layer.update()

    # The bake is its own step now: select the ground meshes, then press the button.
    bpy.ops.object.select_all(action="DESELECT")
    sources = (floor, wall, sphere, capsule, hull, disabled, compound_child)
    for obj in sources:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = floor
    result = bpy.ops.gltf_custom.bake_navigation(
        profile_name="player",
        cell_size=0.5,
        agent_radius=0.3,
        agent_height=1.66,
        step_height=0.5,
        slope_limit=54.0,
        bounds_object="Navigation Bounds",
    )
    assert result == {"FINISHED"}, result
    cache = bpy.context.scene.gltf_navigation
    assert cache.baked and cache.cell_count > 0
    assert list(cache.dimensions) == [23, 19, 23], list(cache.dimensions)
    assert len(cache.sources) == len(sources)
    preview = next(obj for obj in bpy.context.scene.objects
                   if obj.get("gltf_navigation_preview"))
    assert len(preview.data.polygons) > 0

    with ExitStack() as cleanup:
        requested = os.environ.get("NAVIGATION_TEST_OUTPUT")
        if requested:
            path = os.path.abspath(requested)
            os.makedirs(os.path.dirname(path), exist_ok=True)
        else:
            directory = cleanup.enter_context(tempfile.TemporaryDirectory())
            path = os.path.join(directory, "navigation.gltf")
        export_format = "GLB" if path.lower().endswith(".glb") else "GLTF_SEPARATE"
        result = bpy.ops.export_scene.gltf_custom(
            filepath=path,
            export_format=export_format,
            export_navigation=True,
            export_materials=False,
            export_animations=False,
            export_environment_map=False,
        )
        assert result == {"FINISHED"}, result
        if export_format == "GLB":
            with open(path, "rb") as handle:
                payload = handle.read()
            json_length, json_type = struct.unpack_from("<II", payload, 12)
            assert json_type == 0x4E4F534A
            document = json.loads(payload[20:20 + json_length].decode("utf8"))
            binary_at = 20 + json_length
            binary_length, binary_type = struct.unpack_from("<II", payload, binary_at)
            assert binary_type == 0x004E4942
            binary = payload[binary_at + 8:binary_at + 8 + binary_length]
        else:
            with open(path, "r", encoding="utf8") as handle:
                document = json.load(handle)
            with open(os.path.join(os.path.dirname(path), document["buffers"][0]["uri"]), "rb") as handle:
                binary = handle.read()
        assert "CUSTOM_physics_navigation" in document["extensionsUsed"]
        assert not any(node.get("name") == "Navigation Bake" for node in document["nodes"])
        extension = document["extensions"]["CUSTOM_physics_navigation"]
        assert extension["version"] == 1 and len(extension["profiles"]) == 1
        profile = extension["profiles"][0]
        assert profile["name"] == "player"
        assert profile["components"] >= 1
        assert profile["largestComponent"] >= 8
        assert profile["dimensions"] == [23, 19, 23]
        shapes = document["extensions"]["KHR_implicit_shapes"]["shapes"]
        assert {shape["type"] for shape in shapes} >= {"sphere", "capsule"}
        assert any(node.get("extensions", {}).get("KHR_physics_rigid_bodies", {})
                   .get("collider", {}).get("geometry", {}).get("convexHull")
                   for node in document["nodes"])
        disabled_node = next(node for node in document["nodes"]
                             if node.get("name") == "Disabled passive collider")
        assert "collider" in disabled_node["extensions"]["KHR_physics_rigid_bodies"]
        compound_node = next(node for node in document["nodes"]
                             if node.get("name") == "Active compound child")
        assert "collider" in compound_node["extensions"]["KHR_physics_rigid_bodies"]
        assert profile["solidCells"] > 490
        accessor = document["accessors"][profile["cells"]]
        assert accessor["componentType"] == 5125 and accessor["type"] == "SCALAR"
        view = document["bufferViews"][accessor["bufferView"]]
        offset = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
        values = struct.unpack_from(f"<{accessor['count']}I", binary, offset)
        assert values and list(values) == sorted(set(values))
        assert max(values) < profile["dimensions"][0] * profile["dimensions"][1] * profile["dimensions"][2]
        print("NAVIGATION_EXPORT_OK", json.dumps(profile, sort_keys=True))


if __name__ == "__main__":
    main()
