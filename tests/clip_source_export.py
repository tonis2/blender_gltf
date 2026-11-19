"""Background check for an armature's clip source (Clips from): a level
export writes the character file's own animations, fitted to the rig's rest
where rotation or scale was applied, plus the clips authored in Blender.

Run with Blender:
  blender --background --factory-startup --python tests/clip_source_export.py
"""

import contextlib
import io
import math
import os
import sys
import tempfile
from pathlib import Path

import bpy
import numpy as np
from mathutils import Euler, Quaternion, Vector

try:
    import blender_gltf
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    import blender_gltf
if not hasattr(bpy.types.Object, "gltf_clip_source"):
    blender_gltf.register()

from blender_gltf.exporter import ExportSettings, GltfExporter
from blender_gltf.gltf.serialize import read_glb
from blender_gltf.gltf.types import Gltf
from blender_gltf.import_.buffer_reader import BufferReader
from blender_gltf.importer import GltfImporter, ImportSettings

HIPS_LOC = 'pose.bones["hips"].location'
HIPS_ROT = 'pose.bones["hips"].rotation_quaternion'
SPINE_ROT = 'pose.bones["spine"].rotation_quaternion'
LEG_ROT = 'pose.bones["leg"].rotation_quaternion'


def fresh_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.scene.render.fps = 24


def make_character(name, height):
    """A root bone (hips) with two children (spine, leg) and a skinned box."""
    data = bpy.data.armatures.new(name)
    rig = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    hips = data.edit_bones.new("hips")
    hips.head, hips.tail = (0, 0, height), (0, 0, height + 0.2)
    spine = data.edit_bones.new("spine")
    spine.head, spine.tail = (0, 0, height + 0.2), (0, 0.1, height + 0.6)
    spine.parent = hips
    leg = data.edit_bones.new("leg")
    leg.head, leg.tail = (0.1, 0, height), (0.15, 0, 0)
    leg.parent = hips
    bpy.ops.object.mode_set(mode="OBJECT")

    mesh = bpy.data.meshes.new(f"{name}_body")
    vertices = [(x, y, z) for z in (0.0, height) for x, y in ((-0.2, -0.2), (0.2, -0.2), (0.2, 0.2), (-0.2, 0.2))]
    faces = [(i, (i + 1) % 4, (i + 1) % 4 + 4, i + 4) for i in range(4)]
    mesh.from_pydata(vertices, [], faces)
    body = bpy.data.objects.new(f"{name}_body", mesh)
    bpy.context.scene.collection.objects.link(body)
    groups = {bone: body.vertex_groups.new(name=bone) for bone in ("hips", "leg")}
    for index, vertex in enumerate(vertices):
        groups["hips" if vertex[2] > 0.5 * height else "leg"].add([index], 1.0, "REPLACE")
    body.modifiers.new("Armature", "ARMATURE").object = rig
    body.parent = rig
    return rig


def turn(axis, degrees):
    """Quaternion keys (w, x, y, z) for a turn about one axis."""
    q = Quaternion(Vector(axis), math.radians(degrees))
    return list(q)


def make_action(name, rig, curves, interpolation="LINEAR"):
    """``curves``: {data_path: [(frame, values), ...]}; object TRS paths too."""
    action = bpy.data.actions.new(name)
    if hasattr(action, "slots"):
        slot = action.slots.new(id_type="OBJECT", name=rig.name)
        action.layers.new("Layer").strips.new(type="KEYFRAME")
        fcurves = action.layers[0].strips[0].channelbag(slot, ensure=True).fcurves
    else:
        fcurves = action.fcurves
    for data_path, keys in curves.items():
        for index in range(len(keys[0][1])):
            fcurve = fcurves.new(data_path, index=index)
            for frame, values in keys:
                fcurve.keyframe_points.insert(frame, values[index]).interpolation = interpolation
    return action


def walk_keys(rig, name, lean, interpolation="LINEAR"):
    """Keys the root's translation and rotation and the spine's rotation."""
    return make_action(name, rig, {
        HIPS_LOC: [(1, (0.0, 0.0, 0.0)), (13, (0.1, 0.3, 0.05)), (25, (0.0, 0.6, 0.0))],
        HIPS_ROT: [(1, turn((0, 0, 1), 0)), (13, turn((0, 0, 1), 20)), (25, turn((1, 0, 0), lean))],
        SPINE_ROT: [(1, turn((1, 0, 0), 0)), (9, turn((1, 0, 0), lean)), (25, turn((0, 1, 0), -lean))],
    }, interpolation)


def export(path):
    """Export the scene to a .glb; returns (document, reader, printed text)."""
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        GltfExporter(bpy.context, ExportSettings(
            filepath=path, export_format="GLB", export_materials=False,
            export_physics=False, export_particles=False, export_interactivity=False,
            export_audio=False, export_environment_map=False,
        )).export()
    sys.stdout.write(printed.getvalue())
    document, binary = read_glb(Path(path))
    return document, BufferReader(Gltf.from_dict(document), binary, Path(path).parent), printed.getvalue()


def make_source(folder, name, height, clips):
    """Build a character with ``clips`` ({name: (lean, interpolation)}) on its
    clip list and export it as ``name``.glb; returns (path, document, reader)."""
    fresh_scene()
    rig = make_character(name, height)
    actions = [walk_keys(rig, clip, lean, interpolation) for clip, (lean, interpolation) in clips.items()]
    rig.animation_data_create().action = actions[0]
    for action in actions:
        rig.gltf_clips.add().action = action
    path = os.path.join(folder, f"{name}.glb")
    document, reader, _ = export(path)
    assert [a["name"] for a in document["animations"]] == list(clips), document["animations"]
    return path, document, reader


def import_file(path):
    before = set(bpy.data.objects)
    GltfImporter(bpy.context, ImportSettings(filepath=path)).import_file()
    rigs = [obj for obj in bpy.data.objects if obj not in before and obj.type == "ARMATURE"]
    assert len(rigs) == 1, [obj.name for obj in rigs]
    return rigs[0]


def skin_joints(document, skin_name):
    skins = [skin for skin in document["skins"] if skin.get("name") == skin_name]
    assert len(skins) == 1, (skin_name, [skin.get("name") for skin in document["skins"]])
    return skins[0]["joints"]


def own_animations(document, skin_name):
    joints = set(skin_joints(document, skin_name))
    return [a for a in document["animations"] if {c["target"]["node"] for c in a["channels"]} <= joints]


def joint_node(document, skin_name, bone):
    found = [j for j in skin_joints(document, skin_name) if document["nodes"][j]["name"] == bone]
    assert len(found) == 1, (skin_name, bone)
    return document["nodes"][found[0]]


def keys(document, reader, anim):
    """{(node name, path): (times, values, interpolation)} of one animation."""
    found = {}
    for channel in anim["channels"]:
        sampler = anim["samplers"][channel["sampler"]]
        target = (document["nodes"][channel["target"]["node"]]["name"], channel["target"]["path"])
        assert target not in found, target
        found[target] = (
            reader.read_accessor(sampler["input"]),
            reader.read_accessor(sampler["output"]),
            sampler.get("interpolation", "LINEAR"),
        )
    return found


def rest(node):
    x, y, z, w = node.get("rotation", (0.0, 0.0, 0.0, 1.0))
    return np.array(node.get("translation", (0.0, 0.0, 0.0)), dtype=np.float64), Quaternion((w, x, y, z))


def rest_fit(level, source, skin_name):
    """From the two files' joint rests: k over the non-root joints, q and both
    rest translations of the root (hips)."""
    total_level = total_source = 0.0
    for j in skin_joints(level, skin_name):
        name = level["nodes"][j]["name"]
        if name == "hips":
            continue
        level_t, _ = rest(level["nodes"][j])
        source_t, _ = rest(joint_node(source, skin_name, name))
        if np.linalg.norm(source_t) > 1e-6:
            total_level += np.linalg.norm(level_t)
            total_source += np.linalg.norm(source_t)
    k = total_level / total_source
    (level_t, level_r), (source_t, source_r) = rest(joint_node(level, skin_name, "hips")), rest(joint_node(source, skin_name, "hips"))
    return k, level_r @ source_r.inverted(), level_t, source_t


def angle(q):
    return 2.0 * math.acos(min(1.0, abs(q.w)))


def predict(values, target, k, q, level_t, source_t):
    """The formula: T' = L_t + k·q·(T − S_t) and R' = q·R at the root, T' = k·T below."""
    bone, path = target
    if path == "translation" and bone == "hips":
        return np.array([level_t + k * np.array(q @ Vector(v - source_t)) for v in values])
    if path == "translation":
        return values * k
    if path == "rotation" and bone == "hips":
        turned = [q @ Quaternion((v[3], v[0], v[1], v[2])) for v in values]
        return np.array([[r.x, r.y, r.z, r.w] for r in turned])
    return values


def assert_copied(level, level_reader, source, source_reader, skin_name, names, refitted):
    """Each named animation of the skin holds the source's keys at the same
    times; a refitted rig's follow the formula, the rest stay exact. Returns k, q."""
    by_name = {a["name"]: a for a in own_animations(level, skin_name)}
    source_by_name = {a["name"]: a for a in source["animations"]}
    k, q, level_t, source_t = rest_fit(level, source, skin_name)
    moved = max(abs(k - 1.0), angle(q), float(np.abs(level_t - source_t).max()))
    assert (moved > 1e-2) == refitted, (skin_name, k, angle(q), level_t, source_t)
    for name in names:
        got = keys(level, level_reader, by_name[name])
        want = keys(source, source_reader, source_by_name[name])
        assert set(got) == set(want), (name, sorted(got), sorted(want))
        for target, (times, values, interpolation) in want.items():
            got_times, got_values, got_interpolation = got[target]
            assert got_interpolation == interpolation, (name, target)
            assert np.array_equal(got_times, times), (name, target)
            exact = target[0] != "hips" and (target[1] != "translation" or abs(k - 1.0) <= 1e-6)
            if exact or not refitted:
                assert np.array_equal(got_values, values), (name, target)
            else:
                expected = predict(values, target, k, q, level_t, source_t)
                assert np.allclose(got_values, expected, atol=1e-5), (name, target, np.abs(got_values - expected).max())
    return k, q


def check_level(folder):
    """One imported character: moved, re-rested, with a leftover active action
    and one clip authored in Blender."""
    source_path, source, source_reader = make_source(
        folder, "hero", 1.0, {"walk": (15, "LINEAR"), "wave": (-25, "CONSTANT")},
    )

    fresh_scene()
    rig = import_file(source_path)
    assert os.path.samefile(bpy.path.abspath(rig.gltf_clip_source), source_path), rig.gltf_clip_source

    # A level copy can sit elsewhere than in its file: shift every bone,
    # which moves only the root's rest (turns are check_applied's).
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    for bone in rig.data.edit_bones:  # head and tail: EditBone.transform re-rolls
        bone.head += Vector((0.1, -0.2, 0.05))
        bone.tail += Vector((0.1, -0.2, 0.05))
    bpy.ops.object.mode_set(mode="OBJECT")

    rig.location = (5.0, -3.0, 0.5)
    rig.rotation_mode = "XYZ"
    rig.rotation_euler = (0.0, 0.0, 1.2)
    rig.scale = (1.5, 1.5, 1.5)

    leftover = make_action("leftover", rig, {
        'pose.bones["spine"].location': [(1, (0.0, 0.0, 0.0)), (9, (0.0, 0.2, 0.0))],
        "location": [(1, (5.0, -3.0, 0.5)), (9, (6.0, -3.0, 0.5))],
    })
    rig.animation_data_create().action = leftover
    extra = make_action("extra", rig, {LEG_ROT: [(1, turn((1, 0, 0), 0)), (7, turn((1, 0, 0), 40))]})
    rig.gltf_clips.add().action = extra

    level, level_reader, printed = export(os.path.join(folder, "level.glb"))
    assert "[glTF export] hero: 2 clips from hero.glb" in printed, printed
    names = [a["name"] for a in level["animations"]]
    assert names == ["walk", "wave", "extra"], names
    assert [a["name"] for a in own_animations(level, "hero")] == names
    assert_copied(level, level_reader, source, source_reader, "hero", ["walk", "wave"], refitted=True)
    wave = next(a for a in level["animations"] if a["name"] == "wave")
    assert {s.get("interpolation") for s in wave["samplers"]} == {"STEP"}

    # A missing file warns and falls back to the clip list, active action first.
    rig.gltf_clip_source = os.path.join(folder, "missing.glb")
    level, _, printed = export(os.path.join(folder, "fallback.glb"))
    assert "[glTF export] WARNING: 'hero'" in printed and "missing.glb" in printed, printed
    assert [a["name"] for a in own_animations(level, "hero")] == ["leftover", "walk", "wave", "extra"], \
        [a["name"] for a in own_animations(level, "hero")]


def check_two_characters(folder):
    """Two characters with the same bone names and different files each get
    their own file's clips, even with nothing on their clip lists."""
    hero_path, hero, hero_reader = make_source(
        folder, "hero", 1.0, {"walk": (10, "LINEAR"), "idle": (5, "LINEAR")},
    )
    rival_path, rival, rival_reader = make_source(
        folder, "rival", 1.4, {"idle": (-30, "LINEAR"), "run": (35, "LINEAR"), "angry": (50, "CONSTANT")},
    )

    fresh_scene()
    rigs = [import_file(hero_path), import_file(rival_path)]
    assert [rig.name for rig in rigs] == ["hero", "rival"]
    for rig in rigs:
        rig.gltf_clips.clear()  # as in a scene built before import filled the list

    level, level_reader, printed = export(os.path.join(folder, "pair.glb"))
    assert "hero: 2 clips from hero.glb" in printed and "rival: 3 clips from rival.glb" in printed, printed
    assert [a["name"] for a in own_animations(level, "hero")] == ["walk", "idle"]
    assert [a["name"] for a in own_animations(level, "rival")] == ["idle", "run", "angry"]
    assert len(level["animations"]) == 5, [a["name"] for a in level["animations"]]
    assert_copied(level, level_reader, hero, hero_reader, "hero", ["walk", "idle"], refitted=False)
    assert_copied(level, level_reader, rival, rival_reader, "rival", ["idle", "run", "angry"], refitted=False)


def check_applied(folder):
    """Rotation and a uniform scale applied into an imported character's bones:
    the copied keys follow the formula, with k and q those applied."""
    source_path, source, source_reader = make_source(
        folder, "hero", 1.0, {"walk": (15, "LINEAR"), "wave": (-25, "CONSTANT")},
    )
    fresh_scene()
    rig = import_file(source_path)
    applied = Euler((math.radians(10), 0.0, math.radians(70)))
    rig.rotation_mode = "XYZ"
    rig.rotation_euler = applied
    rig.scale = (1.5, 1.5, 1.5)
    rig.location = (2.0, 1.0, 0.0)
    for obj in bpy.context.scene.objects:
        obj.select_set(obj == rig)
    bpy.context.view_layer.objects.active = rig
    assert bpy.ops.object.transform_apply(location=False, rotation=True, scale=True) == {"FINISHED"}

    level, level_reader, printed = export(os.path.join(folder, "applied.glb"))
    assert "[glTF export] hero: 2 clips from hero.glb" in printed, printed
    assert [a["name"] for a in own_animations(level, "hero")] == ["walk", "wave"]
    k, q = assert_copied(level, level_reader, source, source_reader, "hero", ["walk", "wave"], refitted=True)
    assert abs(k - 1.5) < 1e-4, k
    assert abs(angle(q) - angle(applied.to_quaternion())) < 1e-4, (angle(q), applied)

    # Bones that changed shape are not one scale of the file: warn, fall back.
    bpy.ops.object.mode_set(mode="EDIT")
    rig.data.edit_bones["leg"].head.y += 0.05
    bpy.ops.object.mode_set(mode="OBJECT")
    level, _, printed = export(os.path.join(folder, "reshaped.glb"))
    assert "[glTF export] WARNING: 'hero'" in printed and "'leg'" in printed, printed
    assert "clips from" not in printed, printed
    assert sorted(a["name"] for a in own_animations(level, "hero")) == ["walk", "wave"]


def check_cubic_refit():
    """CUBICSPLINE: a root's values turn, scale and move; tangents only turn
    and scale; below the root translations scale and rotations stay."""
    from blender_gltf.export.animation import refit_keys

    q = Quaternion((0.0, 1.0, 0.0), math.radians(90))
    root = (np.array([q.x, q.y, q.z, q.w]), np.array([1.0, 2.0, 3.0]), np.array([0.5, 0.0, 0.0]))
    rows = np.array([[0.5, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 0.25]] * 2)
    got = refit_keys(rows, "translation", True, 2.0, root)
    for i, row in enumerate(rows):
        turned = np.array(q @ Vector(row - root[2] if i % 3 == 1 else row))
        want = root[1] + 2.0 * turned if i % 3 == 1 else 2.0 * turned
        assert np.allclose(got[i], want, atol=1e-6), (i, got[i], want)
    assert np.allclose(refit_keys(rows, "translation", True, 2.0), 2.0 * rows)

    quats = np.array([[0.0, 0.0, 0.0, 1.0], [0.0, 0.0, 0.6, 0.8], [0.1, 0.0, 0.0, 0.0]])
    got = refit_keys(quats, "rotation", True, 2.0, root)
    assert np.allclose(got, predict(quats, ("hips", "rotation"), 2.0, q, None, None), atol=1e-6)
    assert np.array_equal(refit_keys(quats, "rotation", True, 2.0), quats)


def main():
    check_cubic_refit()
    with tempfile.TemporaryDirectory() as folder:
        check_level(folder)
    with tempfile.TemporaryDirectory() as folder:
        check_two_characters(folder)
    with tempfile.TemporaryDirectory() as folder:
        check_applied(folder)
    print("clip_source_export: OK")


if __name__ == "__main__":
    main()
