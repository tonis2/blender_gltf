"""Background check for the armature glTF clip list (multi-clip export).

Run with Blender:
  blender --background --factory-startup --python tests/clip_list_export.py
"""

import json
import os
import sys
import tempfile

import bpy

try:
    import blender_gltf
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    import blender_gltf
if not hasattr(bpy.types.Object, "gltf_clips"):
    blender_gltf.register()

from blender_gltf.export.animation import slot_handle_for
from blender_gltf.exporter import ExportSettings, GltfExporter


def make_rig(name):
    armature = bpy.data.armatures.new(name)
    obj = bpy.data.objects.new(name, armature)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    spine = armature.edit_bones.new("spine")
    spine.head, spine.tail = (0, 0, 0), (0, 0, 1)
    arm = armature.edit_bones.new("arm")
    arm.head, arm.tail = (0, 0, 1), (1, 0, 1)
    arm.parent = spine
    bpy.ops.object.mode_set(mode="OBJECT")
    return obj


def add_curves(action, slot_name, curves, interpolation="LINEAR"):
    """Key ``curves`` ({(data_path, index): [(frame, value), ...]}) into a
    slot of the action named for ``slot_name``."""
    if hasattr(action, "slots"):
        slot = action.slots.new(id_type="OBJECT", name=slot_name)
        if not len(action.layers):
            action.layers.new("Layer").strips.new(type="KEYFRAME")
        fcurves = action.layers[0].strips[0].channelbag(slot, ensure=True).fcurves
    else:
        fcurves = action.fcurves
    for (data_path, index), keys in curves.items():
        fcurve = fcurves.new(data_path, index=index)
        for frame, value in keys:
            fcurve.keyframe_points.insert(frame, value).interpolation = interpolation
    return action


def make_action(name, slot_name, curves, interpolation="LINEAR"):
    return add_curves(bpy.data.actions.new(name), slot_name, curves, interpolation)


def export(path):
    settings = ExportSettings(
        filepath=path,
        export_format="GLTF_SEPARATE",
        export_materials=False,
        export_physics=False,
        export_particles=False,
        export_interactivity=False,
        export_audio=False,
        export_environment_map=False,
    )
    GltfExporter(bpy.context, settings).export()
    with open(path, "r", encoding="utf8") as handle:
        return json.load(handle)


def animation(document, name):
    found = [a for a in document.get("animations", []) if a.get("name") == name]
    assert len(found) == 1, f"expected one animation named {name!r}, found {len(found)}"
    return found[0]


def channel_targets(document, anim):
    nodes = document["nodes"]
    return {(nodes[c["target"]["node"]]["name"], c["target"]["path"]) for c in anim["channels"]}


def main():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    bpy.context.scene.render.fps = 24

    rig = make_rig("Rig")
    spine_rot = 'pose.bones["spine"].rotation_quaternion'
    arm_loc = 'pose.bones["arm"].location'

    idle = make_action("Idle", "Rig", {(spine_rot, 0): [(1, 1.0), (25, 1.0)]})
    walk = make_action("Walk", "Rig", {(arm_loc, 0): [(1, 0.0), (11, 0.5)]}, "BEZIER")
    wave = make_action("Wave", "Rig", {(arm_loc, 2): [(5, 0.0), (17, 0.25)]})
    make_action("Secret", "Rig", {(arm_loc, 1): [(1, 0.0), (9, 1.0)]})

    # One action animating two objects: the first slot belongs to someone
    # else, the second to Rig. Slot handles count up from the same seed in
    # every action, so Rig's assigned handle (Idle's first) matches the wrong
    # slot here: a lookup that leans on it exports the arm, not the spine.
    shared = make_action("Shared", "Other", {(arm_loc, 0): [(1, 0.0), (5, 1.0)]})
    add_curves(shared, "Rig", {('pose.bones["spine"].location', 2): [(10, 0.0), (34, 1.0)]})
    if hasattr(shared, "slots"):
        assert shared.slots[0].handle != shared.slots[1].handle
        assert slot_handle_for(shared, rig) == shared.slots[1].handle

    rig.animation_data_create().action = idle
    for action in (idle, walk, None, wave, shared):
        clip = rig.gltf_clips.add()
        if action is not None:
            clip.action = action

    with tempfile.TemporaryDirectory() as folder:
        document = export(os.path.join(folder, "rig.gltf"))

    names = sorted(a.get("name") for a in document["animations"])
    assert names == ["Idle", "Shared", "Walk", "Wave"], names

    # Bezier-keyed bones are sampled per frame and lerped: a cubic sampler
    # would need three values per key.
    walk_anim = animation(document, "Walk")
    assert channel_targets(document, walk_anim) == {("arm", "translation")}
    sampler = walk_anim["samplers"][0]
    assert "interpolation" not in sampler, sampler
    accessors = document["accessors"]
    assert accessors[sampler["input"]]["count"] == 11
    assert accessors[sampler["output"]]["count"] == 11
    assert abs(accessors[sampler["input"]]["max"][0] - 10 / 24) < 1e-5

    # Linear keys stay as authored, rebased so the first key is t=0.
    wave_anim = animation(document, "Wave")
    wave_input = accessors[wave_anim["samplers"][0]["input"]]
    assert wave_input["count"] == 2
    assert abs(wave_input["min"][0]) < 1e-6 and abs(wave_input["max"][0] - 12 / 24) < 1e-5

    shared_anim = animation(document, "Shared")
    assert channel_targets(document, shared_anim) == {("spine", "translation")}, \
        channel_targets(document, shared_anim)
    shared_input = accessors[shared_anim["samplers"][0]["input"]]
    assert abs(shared_input["max"][0] - 24 / 24) < 1e-5

    check_name_override()
    check_add_matching()

    print("clip_list_export: OK")


def reset():
    for collection in (bpy.data.objects, bpy.data.armatures, bpy.data.actions):
        for item in list(collection):
            collection.remove(item)


def check_name_override():
    """A clip's name replaces its action's name in the file, the active
    action's too; a name used twice leaves the later clip its action's name."""
    reset()
    rig = make_rig("Named")
    arm_loc = 'pose.bones["arm"].location'
    run = make_action("run.002", "Named", {(arm_loc, 0): [(1, 0.0), (9, 1.0)]})
    jump = make_action("jump.001", "Named", {(arm_loc, 1): [(1, 0.0), (9, 1.0)]})
    hop = make_action("hop", "Named", {(arm_loc, 2): [(1, 0.0), (9, 1.0)]})
    rest = make_action("rest.001", "Named", {(arm_loc, 0): [(1, 0.0), (5, 0.5)]})
    rig.animation_data_create().action = run
    for action, name in ((run, "run"), (jump, "jump"), (hop, "jump"), (rest, "")):
        clip = rig.gltf_clips.add()
        clip.action = action
        clip.name = name

    from blender_gltf.export.animation import clip_export_name
    assert clip_export_name(rig, run) == "run" and clip_export_name(rig, rest) == "rest.001"

    with tempfile.TemporaryDirectory() as folder:
        document = export(os.path.join(folder, "named.gltf"))
    names = sorted(a.get("name") for a in document["animations"])
    assert names == ["hop", "jump", "rest.001", "run"], names
    assert channel_targets(document, animation(document, "jump")) == {("arm", "translation")}


def check_add_matching():
    """Add All Matching takes one action per base name, preferring the plain
    name, and names a suffixed one's clip after the base."""
    reset()
    rig = make_rig("Pack")
    arm_loc = 'pose.bones["arm"].location'
    for name in ("walk", "walk.001", "idle.001"):
        make_action(name, "Pack", {(arm_loc, 0): [(1, 0.0), (9, 1.0)]})
    bpy.context.view_layer.objects.active = rig
    assert bpy.ops.gltf_custom.add_matching_clips() == {"FINISHED"}
    clips = [(clip.action.name, clip.name) for clip in rig.gltf_clips]
    assert clips == [("idle.001", "idle"), ("walk", "")], clips
    # Run again: both base names are on the list already.
    make_action("idle", "Pack", {(arm_loc, 0): [(1, 0.0), (9, 1.0)]})
    assert bpy.ops.gltf_custom.add_matching_clips() == {"FINISHED"}
    assert len(rig.gltf_clips) == 2


if __name__ == "__main__":
    main()
