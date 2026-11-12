"""Background check for cutscene authoring and CUSTOM_cutscene export.

Run with Blender:
  blender --background --factory-startup --python tests/cutscene_export.py
"""

import json
import math
import os
import sys
import tempfile
from pathlib import Path

import bpy
import numpy as np

try:
    import blender_gltf
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    import blender_gltf
if not hasattr(bpy.types.Scene, "gltf_cutscenes"):
    blender_gltf.register()

from blender_gltf import cutscene_tools as tools
from blender_gltf.export import cutscene as cs
from blender_gltf.exporter import ExportSettings, GltfExporter
from blender_gltf.gltf.serialize import read_glb


def close(a, b, eps=1e-3):
    return all(abs(x - y) <= eps for x, y in zip(a, b))


def raises(fn, fragment):
    try:
        fn()
    except ValueError as error:
        assert fragment in str(error), f"{fragment!r} not in {error!s}"
    else:
        raise AssertionError(f"expected a ValueError mentioning {fragment!r}")


# --- Pure helpers ---

def check_helpers():
    for kind, expected in (("linear", 0.25), ("in", 0.0625), ("out", 0.4375), ("inout", 0.15625)):
        assert abs(cs.ease(kind, 0.25) - expected) < 1e-9, kind

    corner = np.array([(0, 0, 0), (3, 0, 0), (3, 4, 0)], dtype=float)
    lengths = cs.polyline_lengths(corner)
    assert close(lengths, (0, 3, 7))
    point, tangent = cs.sample_polyline(corner, lengths, 5.0)
    assert close(point, (3, 2, 0)) and close(tangent, (0, 1, 0))

    points, total = cs.resample_polyline(corner, 0.5)
    assert abs(total - 7.0) < 1e-9 and len(points) == 15
    assert close(points[0], corner[0]) and close(points[-1], corner[-1])
    steps = np.linalg.norm(np.diff(points, axis=0), axis=1)
    assert steps.max() <= 0.5 + 1e-9

    wobble = [(x, 0.01 * (x % 2), 0) for x in range(9)] + [(8, 5, 0)]
    assert len(cs.rdp(wobble, 0.05)) == 3
    assert len(cs.rdp(wobble, 0.0)) == len(wobble)

    assert cs.parse_event_name("say") == ("say", False)
    assert cs.parse_event_name(" !music ") == ("music", True)
    raises(lambda: cs.parse_event_name("end"), "reserves")
    raises(lambda: cs.parse_event_name("!"), "no name")

    assert cs.walk_rate(10.0, 5.0, 1.0) == 2.0
    assert cs.walk_rate(10.0, 5.0, 0.0) == 1.0

    def key(at, x, fov=40.0):
        return {"at": at, "eye": [x, 0, 0], "target": [x, 0, 5], "fov": fov}

    still = [key(i / 24, 1.0) for i in range(24)]
    assert len(cs.reduce_linear_keys(still)) == 1
    glide = [key(i / 24, i * 0.1) for i in range(24)]
    assert [k["at"] for k in cs.reduce_linear_keys(glide)] == [0.0, 23 / 24]
    bend = [key(i / 24, min(i, 10) * 0.1) for i in range(24)]
    assert [round(k["at"] * 24) for k in cs.reduce_linear_keys(bend)] == [0, 10, 23]

    # A talk strip on a lower track is interrupted by a wave above it and
    # resumes afterwards, picking up where it would have been.
    talk = {"type": "play", "clip": "talk", "start": 0.0, "end": 6.0, "priority": 0, "speed": 2.0}
    wave = {"type": "play", "clip": "wave", "start": 2.0, "end": 3.0, "priority": 1, "speed": 1.0}
    flat, overlapped = cs.flatten_play_strips([talk, wave])
    assert overlapped
    assert [(s["clip"], s["start"], s["end"]) for s in flat] == [
        ("talk", 0.0, 2.0), ("wave", 2.0, 3.0), ("talk", 3.0, 6.0)]
    assert "offset" not in flat[0] and flat[2]["offset"] == 6.0
    assert all("priority" not in s for s in flat)
    # A move's own clip owns its span.
    flat, overlapped = cs.flatten_play_strips([talk], masks=[(1.0, 4.0)])
    assert not overlapped
    assert [(s["start"], s["end"]) for s in flat] == [(0.0, 1.0), (4.0, 6.0)]


# --- Scene building ---

def make_rig(name, location=(0, 0, 0)):
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
    obj.location = location
    return obj


def make_action(name, obj, curves, interpolation="LINEAR"):
    action = tools.new_object_action(name, obj)
    fcurves = tools._action_fcurves(action)
    for (data_path, index), keys in curves.items():
        fcurve = fcurves.new(data_path, index=index)
        for frame, value in keys:
            fcurve.keyframe_points.insert(frame, value).interpolation = interpolation
    return action


def add_plane(name, size, location):
    bpy.ops.mesh.primitive_plane_add(size=size, location=location)
    obj = bpy.context.object
    obj.name = name
    return obj


def add_camera(name, location, look_at):
    data = bpy.data.cameras.new(name)
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = location
    from mathutils import Vector
    direction = Vector(look_at) - Vector(location)
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    return obj


def add_strip(obj, track_name, action, frame):
    anim = obj.animation_data or obj.animation_data_create()
    track = anim.nla_tracks.get(track_name)
    if track is None:
        track = anim.nla_tracks.new()
        track.name = track_name
    strip = track.strips.new(action.name, frame, action)
    tools._assign_slot(strip, obj)
    return strip


def payload(document):
    assert cs.EXT_CUTSCENE in document["extensionsUsed"]
    return document["extensions"][cs.EXT_CUTSCENE]


def export(scene, folder, name="intro"):
    item = next(c for c in scene.gltf_cutscenes.cutscenes if c.name == name)
    path, warnings = cs.export_cutscene(bpy.context, item, folder, "GLB")
    document, binary = read_glb(Path(path))
    return document, binary, warnings


def strip_of(actor, kind, clip=None):
    found = [s for s in actor["strips"] if s["type"] == kind and (clip is None or s.get("clip") == clip)]
    assert len(found) == 1, (kind, clip, actor["strips"])
    return found[0]


def main():
    check_helpers()

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    scene = bpy.context.scene
    scene.render.fps = 24
    scene.render.fps_base = 1.0
    scene.frame_set(1)

    add_plane("Ground", 60.0, (0, 0, 0))
    add_plane("Roof", 4.0, (3, 0, 3))  # overhead, above part of the path

    # --- Paths: sketch strokes become curves snapped to the ground ---
    sketch = tools.ensure_sketch(bpy.context)
    assert sketch.type == "GREASEPENCIL" and sketch.get(cs.HELPER_PROP) == "sketch"
    assert tools.ensure_sketch(bpy.context) == sketch
    sketch.location = (0, 0, 0.3)  # drawn floating above the ground
    drawing = sketch.data.layers.active.current_frame().drawing
    drawing.add_strokes([40])
    stroke_points = [(i * 6 / 19, 0.0, 0.0) for i in range(20)] + [(6.0, (i + 1) * 4 / 20, 0.0) for i in range(20)]
    for point, co in zip(drawing.strokes[0].points, stroke_points):
        point.position = co
    bpy.context.view_layer.update()

    world = tools.sketch_strokes_world(sketch)
    assert len(world) == 1 and close(world[0][0], (0, 0, 0.3)) and close(world[0][-1], (6, 4, 0.3))
    curves = tools.strokes_to_curves(bpy.context, world, simplify=0.05, snap=True)
    tools.clear_sketch(sketch)
    assert len(curves) == 1 and not tools.sketch_strokes_world(sketch)
    path = curves[0]
    assert path.get(cs.HELPER_PROP) == "path" and path.type == "CURVE"
    knots = path.data.splines[0].bezier_points
    assert 3 < len(knots) < 20, len(knots)  # far fewer than the 40 drawn
    # Snapped to the ground at z=0, not left at 0.3 and not lifted to the roof.
    assert all(abs(knot.co.z) < 1e-4 for knot in knots), [tuple(k.co) for k in knots]

    polyline, closed = cs.curve_polyline(path)
    assert not closed and close(polyline[0], (0, 0, 0)) and close(polyline[-1], (6, 4, 0))
    path_length = float(cs.polyline_lengths(polyline)[-1])
    assert 9.9 < path_length < 10.05, path_length
    # The curve follows the stroke: an auto bezier through just the corner
    # points would bulge half a metre wide of it.
    flat_stroke = [(x, y, 0.0) for x, y, _z in stroke_points]
    assert cs.polyline_distances(flat_stroke, polyline).max() <= 0.05 + 1e-6
    assert cs.polyline_distances(polyline, flat_stroke).max() <= 0.1

    ring = tools.polyline_to_curve(bpy.context, [(0, 0, 0), (2, 0, 0), (2, 2, 0), (0, 2, 0)], "Ring")
    ring.data.splines[0].use_cyclic_u = True
    ring_points, ring_closed = cs.curve_polyline(ring)
    assert ring_closed and close(ring_points[0], ring_points[-1])
    bpy.data.objects.remove(ring)

    # --- The actor and its clips ---
    hero = make_rig("hero", location=(1.0, 2.0, 0.0))
    hero.rotation_euler.z = 0.5
    hero["gltf_uid"] = "hero-0001"
    spine_rot = 'pose.bones["spine"].rotation_quaternion'
    arm_loc = 'pose.bones["arm"].location'
    walk = make_action("walk", hero, {(spine_rot, 0): [(1, 1.0), (25, 1.0)]})
    wave = make_action("wave", hero, {(arm_loc, 2): [(1, 0.0), (13, 0.25)]})
    trip = make_action("intro_trip", hero, {(arm_loc, 0): [(1, 0.0), (9, 0.5)]}, "BEZIER")
    for action, speed in ((walk, 1.5), (wave, 0.0)):
        clip = hero.gltf_clips.add()
        clip.action = action
        clip.speed = speed

    data = scene.gltf_cutscenes
    item = data.cutscenes.add()
    item.name = "intro"
    item.frame_start, item.frame_end = 101, 341  # 10 s
    data.index = 0

    # --- Walk: a move strip generated from the path ---
    move = tools.create_move(hero, path, 101, 173, scene, ease="LINEAR", turn=True, clip=walk)
    assert hero.gltf_cutscene.enabled
    assert (move.frame_start, move.frame_end) == (101.0, 173.0)
    assert cs.move_kind(move.action) == "MOVE"
    assert tools.home_strip(hero) is not None
    assert hero.animation_data.nla_tracks.get(cs.WALK_TRACK) is not None

    scene.frame_set(50)   # before the first move: at home, not the world origin
    assert close(hero.location, (1.0, 2.0, 0.0)), tuple(hero.location)
    assert abs(hero.rotation_euler.z - 0.5) < 1e-4
    scene.frame_set(101)  # path start, facing +X: -Y turned by +90 degrees
    assert close(hero.location, (0, 0, 0))
    assert abs(hero.rotation_euler.z - math.pi / 2) < 1e-2, hero.rotation_euler.z
    scene.frame_set(137)  # half way in time is half way along (linear ease)
    lengths = cs.polyline_lengths(polyline)
    expected, _tangent = cs.sample_polyline(polyline, lengths, path_length / 2)
    assert close(hero.location, expected, 1e-2), (tuple(hero.location), expected)
    scene.frame_set(173)
    assert close(hero.location, (6, 4, 0))
    assert abs(hero.rotation_euler.z - math.pi) < 1e-2  # facing +Y at the end
    scene.frame_set(300)  # holds where it ended
    assert close(hero.location, (6, 4, 0))

    # Stretching the strip retimes the move: refresh refills the keys to fit.
    move.scale = 2.0
    assert move.frame_end == 245.0
    assert tools.refresh_moves(scene) == 1
    move = tools.move_strips(hero)[0]
    assert (move.frame_start, move.frame_end, move.scale) == (101.0, 245.0, 1.0)
    scene.frame_set(173)
    assert close(hero.location, expected, 1e-2)
    move.scale = 0.5
    tools.refresh_moves(scene)
    move = tools.move_strips(hero)[0]
    assert (move.frame_start, move.frame_end) == (101.0, 173.0)

    # A second move that does not turn keeps the facing the first one left.
    back = tools.polyline_to_curve(bpy.context, [(6, 4, 0), (6, 8, 0), (2, 8, 0)], "Back")
    second = tools.create_move(hero, back, 250, 298, scene, ease="INOUT", turn=False)
    scene.frame_set(298)
    assert close(hero.location, (2, 8, 0))
    assert abs(hero.rotation_euler.z - math.pi) < 1e-2
    raises(lambda: tools.create_move(hero, back, 260, 280, scene), "No room")

    # --- Acting strips, one of them a one-off ---
    add_strip(hero, "Acting", wave, 173)          # 173-185, in the clip list
    looped = add_strip(hero, "Acting", wave, 190)  # looping, slower, fading in
    looped.repeat = 2.0
    looped.scale = 2.0
    looped.blend_in = 6.0
    add_strip(hero, "Acting", trip, 300)          # 300-308, not in the clip list

    # An object with strips that nobody marked as an actor is left out.
    extra = make_rig("extra")
    add_strip(extra, "Acting", make_action("extra_idle", extra, {(arm_loc, 0): [(1, 0.0), (9, 1.0)]}), 120)
    assert [obj.name for obj in tools._unmarked_with_strips(scene, item)] == ["extra"]

    # --- Camera shots and events ---
    cam_a = add_camera("CamA", (0.0, -10.0, 2.0), (0.0, 0.0, 2.0))
    cam_a.keyframe_insert("location", frame=101)
    cam_a.location.x = 4.0
    cam_a.keyframe_insert("location", frame=149)
    cam_b = add_camera("CamB", (10.0, 0.0, 5.0), (0.0, 0.0, 0.0))
    target = bpy.data.objects.new("Focus", None)
    scene.collection.objects.link(target)
    cam_b.data.dof.focus_object = target
    cam_b.data.lens = 50.0
    cam_b.data.keyframe_insert("lens", frame=221)
    cam_b.data.lens = 100.0
    cam_b.data.keyframe_insert("lens", frame=341)
    scene.timeline_markers.new("shot_a", frame=101).camera = cam_a
    scene.timeline_markers.new("shot_b", frame=221).camera = cam_b
    scene.timeline_markers.new("!music", frame=101)
    scene.timeline_markers.new("say", frame=125)
    scene.timeline_markers.new("say", frame=197)
    scene.timeline_markers.new("later", frame=900)  # outside the cutscene
    text = bpy.data.texts.new("intro events")
    text.write(json.dumps({"say": [{"text": "Not today."}, {"text": "Run."}], "music": {"track": "tense"}}))
    item.event_data = text

    scene.frame_set(7)
    with tempfile.TemporaryDirectory() as folder:
        document, binary, warnings = export(scene, folder)
        assert sorted(os.listdir(folder)) == ["intro.cutscene.glb"]

        # --- File shape ---
        assert scene.frame_current == 7, "export must put the playhead back"
        assert "meshes" not in document and "skins" not in document
        cut = payload(document)
        assert cut["version"] == 1 and cut["name"] == "intro"
        assert cut["duration"] == 10.0 and cut["fps"] == 24.0

        # --- Actor: stub node + strips ---
        assert [a["row"] for a in cut["actors"]] == ["hero"]
        actor = cut["actors"][0]
        assert actor["uid"] == "hero-0001"
        nodes = document["nodes"]
        stub = nodes[actor["node"]]
        assert stub["name"] == "hero" and stub["uid"] == "hero-0001"
        assert document["scenes"][0]["nodes"] == [actor["node"]]
        assert [s["start"] for s in actor["strips"]] == sorted(s["start"] for s in actor["strips"])

        walk_move = [s for s in actor["strips"] if s["type"] == "move"][0]
        assert (walk_move["start"], walk_move["end"]) == (0.0, 3.0)
        assert walk_move["ease"] == "linear" and walk_move["turn"] is True
        assert walk_move["clip"] == "walk" and walk_move["loop"] is True
        assert "animation" not in walk_move
        # Stride matched to the ground covered: (length / 3 s) / 1.5 m/s.
        assert abs(walk_move["speed"] - (path_length / 3.0) / 1.5) < 1e-3
        second_move = [s for s in actor["strips"] if s["type"] == "move"][1]
        assert (second_move["start"], second_move["end"]) == (round(149 / 24, 4), round(197 / 24, 4))
        assert second_move["ease"] == "inout" and second_move["turn"] is False
        assert "clip" not in second_move

        plays = [s for s in actor["strips"] if s["type"] == "play"]
        assert [p["clip"] for p in plays] == ["wave", "wave", "intro_trip"]
        once, loop, one_off = plays
        assert (once["start"], once["end"]) == (3.0, 3.5)
        assert once["loop"] is False and once["speed"] == 1.0 and once["fade"] == 0.2
        assert "animation" not in once and "offset" not in once
        assert loop["loop"] is True and loop["speed"] == 0.5 and loop["fade"] == 0.25
        assert (loop["start"], loop["end"]) == (round(89 / 24, 4), round(137 / 24, 4))
        # Neither the home strip nor the walk-clip preview strip is exported.
        assert len(actor["strips"]) == 5

        # --- One-off clip: a real animation on stub bone nodes ---
        index = one_off["animation"]
        animation = document["animations"][index]
        assert animation["name"] == "intro_trip" and len(document["animations"]) == 1
        targets = {(nodes[c["target"]["node"]]["name"], c["target"]["path"]) for c in animation["channels"]}
        assert targets == {("arm", "translation")}
        arm_index = animation["channels"][0]["target"]["node"]
        assert stub["children"] == [arm_index]
        assert len(nodes) == 2, [n["name"] for n in nodes]  # only bones a one-off animates
        sampler = animation["samplers"][0]
        assert "interpolation" not in sampler
        assert document["accessors"][sampler["input"]]["count"] == 9
        assert len(binary) > 0 and document["buffers"][0]["byteLength"] == len(binary)

        # --- Paths: inline, evenly spaced, Y-up ---
        assert [p["name"] for p in cut["paths"]] == [path.name, "Back"]
        assert walk_move["path"] == 0 and second_move["path"] == 1
        exported = cut["paths"][0]
        assert abs(exported["length"] - path_length) < 1e-3
        assert close(exported["points"][0], (0, 0, 0)) and close(exported["points"][-1], (6, 0, -4))
        gaps = np.linalg.norm(np.diff(np.array(exported["points"]), axis=0), axis=1)
        assert gaps.max() <= 0.25 + 1e-3 and gaps.min() > 0.2

        # --- Camera: a cut per shot, keys at the camera's own keyframes ---
        camera = cut["camera"]
        assert [k["at"] for k in camera] == [0.0, 2.0, 5.0, 10.0], [k["at"] for k in camera]
        assert [bool(k.get("cut")) for k in camera] == [True, False, True, False]
        assert close(camera[0]["eye"], (0, 2, 10)) and close(camera[0]["target"], (0, 2, 5))
        assert close(camera[1]["eye"], (4, 2, 10))  # CamA moved by frame 149, then held
        assert close(camera[2]["eye"], (10, 5, 0))
        # CamB's target sits on its focus object, the origin.
        assert close(camera[2]["target"], (0, 0, 0), 1e-2), camera[2]["target"]
        assert camera[2]["fov"] > camera[3]["fov"] > 0  # the lens zooms in
        assert all("ease" not in k for k in camera)

        # --- Events: sorted, payloads from the text block ---
        events = cut["events"]
        assert [(e["name"], e["at"]) for e in events] == [("music", 0.0), ("say", 1.0), ("say", 4.0)]
        assert events[0]["always"] is True and events[0]["data"] == {"track": "tense"}
        assert events[1]["data"] == {"text": "Not today."} and events[2]["data"] == {"text": "Run."}
        assert "always" not in events[1]
        assert not warnings, warnings

        # --- Baked camera: every frame sampled, then reduced ---
        item.camera_mode = "BAKED"
        baked = payload(export(scene, folder)[0])["camera"]
        frames = [round(k["at"] * 24) for k in baked]
        # CamA eases between its two keys, so the bake keeps frames inside the
        # move, none after it stops at frame 48, and starts again at the cut.
        assert frames[0] == 0 and 120 in frames
        assert len([f for f in frames if 0 < f < 48]) >= 2
        assert len([f for f in frames if 48 < f < 120]) <= 1
        # Straight lines between the kept keys reproduce the eased move.
        for frame in (110, 125, 140, 160):
            scene.frame_set(frame)
            at = (frame - 101) / 24
            after = next(i for i, k in enumerate(baked) if k["at"] >= at)
            a, b = baked[after - 1], baked[after]
            u = (at - a["at"]) / (b["at"] - a["at"])
            assert abs(a["eye"][0] + (b["eye"][0] - a["eye"][0]) * u - cam_a.location.x) < 0.01
        scene.frame_set(7)
        assert [f for f, k in zip(frames, baked) if k.get("cut")] == [0, 120]
        assert all(("ease" in k) != bool(k.get("cut")) for k in baked)
        assert all(k.get("ease", "linear") == "linear" for k in baked)
        item.camera_mode = "NONE"
        assert "camera" not in payload(export(scene, folder)[0])
        item.camera_mode = "KEYS"

        # --- Without one-offs the file has no buffer at all ---
        trip_strip = next(s for s in tools._all_strips(hero) if s.action == trip)
        trip_strip.mute = True
        lean, lean_binary, _ = export(scene, folder)
        assert "animations" not in lean and "buffers" not in lean and lean_binary == b""
        assert "children" not in lean["nodes"][0] and len(lean["nodes"]) == 1
        trip_strip.mute = False

        # --- Separate .gltf: JSON beside a .bin named in the buffer ---
        path_written, _ = cs.export_cutscene(bpy.context, item, folder, "GLTF_SEPARATE")
        assert path_written.endswith("intro.cutscene.gltf")
        with open(path_written, "r", encoding="utf8") as handle:
            separate = json.load(handle)
        assert separate["buffers"][0]["uri"] == "intro.cutscene.bin"
        assert os.path.exists(os.path.join(folder, "intro.cutscene.bin"))

        # --- Warnings and errors ---
        looped.use_reverse = True
        looped.action_frame_end = 9.0  # repeat a trimmed part of the clip
        overlap = add_strip(hero, "Reactions", wave, 176)  # over the first wave
        _, _, warnings = export(scene, folder)
        assert any("reversed" in w for w in warnings), warnings
        assert any("overlapping" in w for w in warnings), warnings
        assert any("repeats only part" in w for w in warnings), warnings
        looped.use_reverse = False
        looped.action_frame_end = 13.0
        hero.animation_data.nla_tracks.remove(hero.animation_data.nla_tracks["Reactions"])

        scene.timeline_markers.new("end", frame=200)
        raises(lambda: export(scene, folder), "reserves")
        scene.timeline_markers.remove(scene.timeline_markers["end"])

        second.action.gltf_cutscene_move.path = None
        raises(lambda: export(scene, folder), "lost its path")
        second.action.gltf_cutscene_move.path = back

        text.clear()
        text.write("{not json")
        raises(lambda: export(scene, folder), "not valid JSON")
        item.event_data = None

        empty = data.cutscenes.add()
        empty.name = "empty"
        empty.frame_start, empty.frame_end = 5000, 5100
        only_camera = payload(export(scene, folder, "empty")[0])
        assert set(only_camera) == {"version", "name", "duration", "fps", "camera"}
        empty.camera_mode = "NONE"
        raises(lambda: export(scene, folder, "empty"), "has nothing")

        # --- The scene export leaves the authoring helpers out ---
        scene_path = os.path.join(folder, "scene.gltf")
        GltfExporter(bpy.context, ExportSettings(
            filepath=scene_path, export_format="GLTF_SEPARATE", export_materials=False,
            export_physics=False, export_particles=False, export_interactivity=False,
            export_audio=False, export_environment_map=False,
        )).export()
        with open(scene_path, "r", encoding="utf8") as handle:
            level = json.load(handle)
        names = {node.get("name") for node in level["nodes"]}
        assert {"hero", "Ground", "CamA"} <= names
        assert not names & {sketch.name, path.name, "Back"}, names
        # The character's own file carries its clip list; the generated move
        # and home actions are not clips.
        assert sorted(a["name"] for a in level["animations"] if a["name"] in {"walk", "wave"}) == ["walk", "wave"]
        assert not any("move" in a["name"] or "home" in a["name"] for a in level["animations"])
        assert "gltf_clips" not in json.dumps(level)

    # --- Operators (as far as background mode can drive them) ---
    assert bpy.ops.gltf_custom.add_cutscene() == {"FINISHED"}
    assert data.cutscenes[data.index].name == "cutscene"
    assert bpy.ops.gltf_custom.cutscene_preview_range() == {"FINISHED"}
    assert bpy.ops.gltf_custom.remove_cutscene() == {"FINISHED"}
    assert bpy.ops.gltf_custom.refresh_moves() == {"FINISHED"}

    for obj in scene.objects:
        obj.select_set(False)
    hero.select_set(True)
    path.select_set(True)
    bpy.context.view_layer.objects.active = hero
    before = len(tools.move_strips(hero))
    assert bpy.ops.gltf_custom.walk_path(clip="walk", speed=1.5, frame_start=400) == {"FINISHED"}
    walked = tools.move_strips(hero)
    assert len(walked) == before + 1
    assert abs((walked[-1].frame_end - walked[-1].frame_start) - round(path_length / 1.5 * 24)) < 1e-6
    assert bpy.ops.gltf_custom.set_home() == {"FINISHED"}

    path.select_set(False)
    extra.select_set(True)
    assert bpy.ops.gltf_custom.add_cutscene_actors() == {"FINISHED"}
    assert extra.gltf_cutscene.enabled
    assert bpy.ops.gltf_custom.remove_cutscene_actor(name="extra") == {"FINISHED"}
    assert not extra.gltf_cutscene.enabled

    bpy.context.view_layer.objects.active = extra
    assert bpy.ops.gltf_custom.add_matching_clips() == {"FINISHED"}
    assert [clip.action.name for clip in extra.gltf_clips] == ["extra_idle", "intro_trip", "walk", "wave"]
    assert bpy.ops.gltf_custom.remove_clip() == {"FINISHED"}

    # The sidebar's clip list follows a selected skinned mesh to its armature.
    skin = bpy.data.objects.new("extra_skin", bpy.data.meshes.new("extra_skin"))
    scene.collection.objects.link(skin)
    skin.modifiers.new("Armature", "ARMATURE").object = extra
    bpy.context.view_layer.objects.active = skin
    assert tools.clip_owner(skin) == extra and tools.VIEW3D_PT_gltf_clips.poll(bpy.context)
    count = len(extra.gltf_clips)
    assert bpy.ops.gltf_custom.add_clip() == {"FINISHED"}
    assert len(extra.gltf_clips) == count + 1 and not len(skin.gltf_clips)
    assert bpy.ops.gltf_custom.remove_clip() == {"FINISHED"}
    assert tools.clip_owner(path) is None

    # Picking the mesh with a path walks the armature, as does picking both.
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    skin.select_set(True)
    path.select_set(True)
    assert tools._walk_selection(bpy.context) == (extra, path)
    extra.select_set(True)
    assert tools._walk_selection(bpy.context) == (extra, path)
    extra.select_set(False)
    path.select_set(False)
    assert bpy.ops.gltf_custom.add_cutscene_actors() == {"FINISHED"}
    assert extra.gltf_cutscene.enabled and not skin.gltf_cutscene.enabled
    assert bpy.ops.gltf_custom.remove_cutscene_actor(name="extra") == {"FINISHED"}
    skin.select_set(False)
    bpy.data.objects.remove(skin)

    # Sketch mode: entering and leaving works headless; drawing itself needs a viewport.
    assert bpy.ops.gltf_custom.draw_path() == {"FINISHED"}
    assert bpy.context.object == sketch and bpy.context.mode == "PAINT_GREASE_PENCIL"
    assert scene.tool_settings.gpencil_stroke_placement_view3d == "SURFACE"
    drawing = sketch.data.layers.active.current_frame().drawing
    drawing.add_strokes([3])
    for point, co in zip(drawing.strokes[0].points, [(0, -3, 0), (2, -3, 0), (4, -3, 0)]):
        point.position = co
    made_before = {obj.name for obj in scene.objects if obj.get(cs.HELPER_PROP) == "path"}
    assert bpy.ops.gltf_custom.finish_path() == {"FINISHED"}
    assert bpy.context.mode == "OBJECT"
    assert scene.tool_settings.gpencil_stroke_placement_view3d != "SURFACE"
    made = {obj.name for obj in scene.objects if obj.get(cs.HELPER_PROP) == "path"} - made_before
    assert len(made) == 1
    assert bpy.ops.gltf_custom.snap_path() == {"FINISHED"}
    assert bpy.ops.gltf_custom.finish_path() == {"CANCELLED"}  # nothing drawn

    check_active_action_covering(scene)
    check_buried_walk_track(scene, hero)
    check_walks_keep_the_lean(scene)

    print("cutscene_export: OK")


def check_walks_keep_the_lean(scene):
    """A walk turns the actor about the vertical and keeps the lean it stands
    with, in Euler and in Quaternion mode alike, as the engine does."""
    from mathutils import Vector

    path = tools.polyline_to_curve(bpy.context, [(30, 0, 0), (34, 0, 0), (34, -4, 0)], "Lean path")
    up = Vector((0, 0, 1))
    facings = {}
    for mode in ("XYZ", "QUATERNION"):
        rig = make_rig(f"lean_{mode}", location=(30, 0, 0))
        rig.rotation_mode = "XYZ"
        rig.rotation_euler = (math.radians(-8), math.radians(10), 0.3)
        home = rig.rotation_euler.to_quaternion()
        leaning = math.degrees(home.to_matrix().col[2].angle(up))
        rig.rotation_mode = mode
        if mode == "QUATERNION":
            rig.rotation_quaternion = home
        tools.create_move(rig, path, 1, 41, scene, ease="LINEAR", turn=True)
        for frame in (10, 35):  # heading along +X, then down -Y
            scene.frame_set(frame)
            matrix = rig.matrix_world.to_3x3()
            lean = math.degrees(matrix.col[2].angle(up))
            assert abs(lean - leaning) < 0.1, (mode, frame, lean, leaning)
            facings.setdefault(frame, []).append(-matrix.col[1])
        bpy.data.objects.remove(rig)
    for frame, (euler, quaternion) in facings.items():
        assert euler.angle(quaternion) < 1e-3, (frame, tuple(euler), tuple(quaternion))
    assert facings[10][0].x > 0.9 and facings[35][0].y < -0.9, facings
    bpy.data.objects.remove(path)


def check_buried_walk_track(scene, hero):
    """Acting laid out after a walk lands above the walk preview and hides it;
    previewing the cutscene puts the walk back on top."""
    assert not tools.walk_track_buried(hero)
    late = make_action("late_wave", hero, {('pose.bones["arm"].location', 1): [(1, 0.0), (5, 1.0)]})
    add_strip(hero, "late acting", late, 900)
    assert tools.walk_track_buried(hero)

    scene.gltf_cutscenes.index = [c.name for c in scene.gltf_cutscenes.cutscenes].index("intro")
    assert bpy.ops.gltf_custom.cutscene_preview_range() == {"FINISHED"}
    assert not tools.walk_track_buried(hero)
    assert hero.animation_data.nla_tracks[-1].name == cs.WALK_TRACK


def check_active_action_covering(scene):
    """An active action plays over the NLA in Blender but is not in the file:
    it is warned about, and stashing it lets Blender show the strips."""
    with tempfile.TemporaryDirectory() as folder:
        rig = make_rig("cover", location=(20, 0, 0))
        arm_x = ('pose.bones["arm"].location', 0)
        wave = make_action("cover_wave", rig, {arm_x: [(1, 0.0), (10, 1.0)]})
        add_strip(rig, "acting", wave, 1)
        pose = make_action("cover_pose", rig, {arm_x: [(1, 5.0)]})
        rig.animation_data.action = pose
        rig.gltf_cutscene.enabled = True

        item = scene.gltf_cutscenes.cutscenes.add()
        item.name, item.frame_start, item.frame_end, item.camera_mode = "covered", 1, 20, "NONE"

        scene.frame_set(5)
        assert close([rig.pose.bones["arm"].location.x], [5.0]), "the active action plays over the strip"
        assert cs.covering_action(rig) == pose
        _, _, warnings = export(scene, folder, "covered")
        assert any("'cover'" in w and "active action" in w for w in warnings), warnings

        assert bpy.ops.gltf_custom.stash_actor_action(name="cover") == {"FINISHED"}
        anim = rig.animation_data
        assert anim.action is None and cs.covering_action(rig) is None
        stash = anim.nla_tracks[tools.STASH_TRACK]
        assert stash.mute and stash.strips[0].action == pose
        scene.frame_set(5)
        assert close([rig.pose.bones["arm"].location.x], [4.0 / 9.0]), "the strip plays once stashed"

        document, _, warnings = export(scene, folder, "covered")
        assert not any("'cover'" in w for w in warnings), warnings
        actor = next(a for a in payload(document)["actors"] if a["row"] == "cover")
        assert [s.get("clip") for s in actor["strips"]] == ["cover_wave"], actor["strips"]
        assert bpy.ops.gltf_custom.stash_actor_action(name="cover") == {"CANCELLED"}


if __name__ == "__main__":
    main()
