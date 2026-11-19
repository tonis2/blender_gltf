"""Background check that an imported character keeps all its clips: the
importer fills the clip list, and a scene export writes every clip again
under the file's names, even for a second copy whose actions are walk.001.

Run with Blender:
  blender --background --factory-startup --python tests/import_clip_list.py
"""

import os
import sys
import tempfile
from pathlib import Path

import bpy

try:
    import blender_gltf
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    import blender_gltf
if not hasattr(bpy.types.Object, "gltf_clips"):
    blender_gltf.register()

from blender_gltf.exporter import ExportSettings, GltfExporter
from blender_gltf.gltf.serialize import read_glb
from blender_gltf.importer import GltfImporter, ImportSettings

CLIPS = ("walk", "idle", "fight")


def make_character(name):
    data = bpy.data.armatures.new(name)
    rig = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    spine = data.edit_bones.new("spine")
    spine.head, spine.tail = (0, 0, 0), (0, 0, 1)
    arm = data.edit_bones.new("arm")
    arm.head, arm.tail = (0, 0, 1), (1, 0, 1)
    arm.parent = spine
    bpy.ops.object.mode_set(mode="OBJECT")

    mesh = bpy.data.meshes.new(f"{name}_body")
    vertices = [(x, y, z) for z in (0.0, 1.0) for x, y in ((-0.2, -0.2), (0.2, -0.2), (0.2, 0.2), (-0.2, 0.2))]
    faces = [(i, (i + 1) % 4, (i + 1) % 4 + 4, i + 4) for i in range(4)]
    mesh.from_pydata(vertices, [], faces)
    body = bpy.data.objects.new(f"{name}_body", mesh)
    bpy.context.scene.collection.objects.link(body)
    groups = {bone: body.vertex_groups.new(name=bone) for bone in ("spine", "arm")}
    for index, vertex in enumerate(vertices):
        groups["arm" if vertex[2] > 0.5 else "spine"].add([index], 1.0, "REPLACE")
    body.modifiers.new("Armature", "ARMATURE").object = rig
    body.parent = rig
    return rig


def make_action(name, rig, data_path, index, keys):
    action = bpy.data.actions.new(name)
    if hasattr(action, "slots"):
        slot = action.slots.new(id_type="OBJECT", name=rig.name)
        action.layers.new("Layer").strips.new(type="KEYFRAME")
        fcurves = action.layers[0].strips[0].channelbag(slot, ensure=True).fcurves
    else:
        fcurves = action.fcurves
    fcurve = fcurves.new(data_path, index=index)
    for frame, value in keys:
        fcurve.keyframe_points.insert(frame, value).interpolation = "LINEAR"
    return action


def export(path):
    GltfExporter(bpy.context, ExportSettings(
        filepath=path, export_format="GLB", export_materials=False,
        export_physics=False, export_particles=False, export_interactivity=False,
        export_audio=False, export_environment_map=False,
    )).export()
    document, _binary = read_glb(Path(path))
    return document


def import_file(path):
    before = set(bpy.data.objects), set(bpy.data.actions)
    GltfImporter(bpy.context, ImportSettings(filepath=path)).import_file()
    rigs = [obj for obj in bpy.data.objects if obj not in before[0] and obj.type == "ARMATURE"]
    assert len(rigs) == 1, [obj.name for obj in rigs]
    return rigs[0], set(bpy.data.actions) - before[1]


def joints_of(document, skin_name):
    skins = [skin for skin in document["skins"] if skin.get("name") == skin_name]
    assert len(skins) == 1, (skin_name, [skin.get("name") for skin in document["skins"]])
    return set(skins[0]["joints"])


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.scene.render.fps = 24

    rig = make_character("rig")
    spine_rot = 'pose.bones["spine"].rotation_quaternion'
    arm_loc = 'pose.bones["arm"].location'
    actions = [
        make_action("walk", rig, arm_loc, 0, [(1, 0.0), (13, 0.5)]),
        make_action("idle", rig, spine_rot, 1, [(1, 0.0), (25, 0.1)]),
        make_action("fight", rig, arm_loc, 2, [(1, 0.0), (7, 0.3)]),
    ]
    rig.animation_data_create().action = actions[0]
    for action in actions:
        rig.gltf_clips.add().action = action

    with tempfile.TemporaryDirectory() as folder:
        source = os.path.join(folder, "character.glb")
        document = export(source)
        assert [a["name"] for a in document["animations"]] == list(CLIPS)

        bpy.ops.wm.read_factory_settings(use_empty=True)
        assert hasattr(bpy.types.Object, "gltf_clips")
        assert not len(bpy.data.actions) and not len(bpy.data.objects)
        bpy.context.scene.render.fps = 24

        first, first_actions = import_file(source)
        second, second_actions = import_file(source)
        assert first.name != second.name
        # Blender renamed the second copy's actions; the clip list keeps the file's names.
        assert sorted(a.name for a in second_actions) == sorted(f"{name}.001" for name in CLIPS)
        for imported, own in ((first, first_actions), (second, second_actions)):
            clips = list(imported.gltf_clips)
            assert [clip.name for clip in clips] == list(CLIPS), [clip.name for clip in clips]
            assert {clip.action for clip in clips} == own and len(clips) == 3
            assert all(clip.action.name.split(".")[0] == clip.name for clip in clips)

        level = export(os.path.join(folder, "level.glb"))

    nodes = level["nodes"]
    animations = level["animations"]
    assert len(animations) == 6, [a["name"] for a in animations]
    assert not any("." in a["name"] for a in animations), [a["name"] for a in animations]
    for imported in (first, second):
        joints = joints_of(level, imported.name)
        own = [a for a in animations if {c["target"]["node"] for c in a["channels"]} <= joints]
        assert sorted(a["name"] for a in own) == sorted(CLIPS), (imported.name, [a["name"] for a in own])
    for anim in animations:
        targets = {nodes[c["target"]["node"]]["name"] for c in anim["channels"]}
        assert targets <= {"spine", "arm"}, (anim["name"], targets)

    print("import_clip_list: OK")


if __name__ == "__main__":
    main()
