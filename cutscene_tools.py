"""Authoring tools for cutscenes and character clip lists.

- Clips (3D viewport sidebar, glTF tab): the actions a character's own .glb
  carries, for the selected armature or the armature of the selected mesh.
- Cutscenes (3D viewport sidebar, glTF tab): named frame ranges whose actors
  are driven by NLA strips, with a walk tool that turns a curve into a move
  strip and a sketch tool that draws those curves on the ground.

What ends up in a file is decided by export/cutscene.py; this module creates
the Blender data it reads. See extensions/cutscene.md.
"""

import math

import bpy
import numpy as np
from bpy.props import (
    BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty,
    PointerProperty, StringProperty,
)
from mathutils import Euler, Matrix, Quaternion, Vector

from .export import cutscene as cs
from .export.animation import _BONE_FCURVE_RE, _get_fcurves, slot_handle_for


SKETCH_NAME = "Cutscene Path Sketch"
PATHS_COLLECTION = "Cutscene Paths"
# Where the stroke placement in use before sketching is kept, on the sketch.
_PLACEMENT_PROP = "_gltf_cutscene_placement"
# Enum value of a LINEAR keyframe, for foreach_set.
_LINEAR = 1

EASE_ITEMS = [
    ("LINEAR", "Linear", "Even speed from start to end"),
    ("IN", "Ease In", "Start slow"),
    ("OUT", "Ease Out", "End slow"),
    ("INOUT", "Ease In and Out", "Start and end slow"),
]


def scene_fps(scene) -> float:
    return scene.render.fps / scene.render.fps_base


# --- Clip list ---

def matching_actions(obj) -> list:
    """Actions that animate at least one pose bone of this armature."""
    bones = set(obj.data.bones.keys())
    found = []
    for action in bpy.data.actions:
        if cs.move_kind(action) != "NONE":
            continue
        for fcurve in _get_fcurves(action):
            match = _BONE_FCURVE_RE.match(fcurve.data_path)
            if match and match.group(1) in bones:
                found.append(action)
                break
    return found


class GltfClip(bpy.types.PropertyGroup):
    """One action exported as an animation of the armature's own file."""

    action: PointerProperty(
        name="Action", type=bpy.types.Action,
        description="Exported as a glTF animation named after this action",
    )
    speed: FloatProperty(
        name="Ground Speed", min=0.0, default=0.0, unit="VELOCITY",
        description=(
            "How fast the clip travels over the ground at normal playback, for walk "
            "and run cycles. A walk along a path plays the clip faster or slower to "
            "match; 0 leaves it at normal speed"
        ),
    )


class DATA_UL_gltf_clips(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        row = layout.row(align=True)
        row.prop(item, "action", text="", icon="ACTION")
        row.prop(item, "speed", text="")


def skin_armature(obj):
    """The armature a skinned mesh deforms with, or None."""
    for modifier in getattr(obj, "modifiers", ()):
        if modifier.type == "ARMATURE" and modifier.object is not None:
            return modifier.object
    return None


def clip_owner(obj):
    """The armature whose clip list this object shows: the armature itself,
    or the one a skinned mesh follows, since clicking a character in the
    viewport usually selects its mesh."""
    if obj is None:
        return None
    if obj.type == "ARMATURE":
        return obj
    owner = skin_armature(obj)
    if owner is None and obj.parent is not None and obj.parent.type == "ARMATURE":
        owner = obj.parent
    return owner


def actor_for(obj):
    """The object a cutscene drives when this one is picked: a skinned mesh
    stands for its armature, which is what moves the character."""
    return skin_armature(obj) or obj


def _unique(objects) -> list:
    seen = {}
    for obj in objects:
        seen.setdefault(obj.name_full, obj)
    return list(seen.values())


class OBJECT_OT_gltf_add_clip(bpy.types.Operator):
    """Add an action to this armature's glTF clip list"""

    bl_idname = "gltf_custom.add_clip"
    bl_label = "Add Clip"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return clip_owner(context.object) is not None

    def execute(self, context):
        obj = clip_owner(context.object)
        clip = obj.gltf_clips.add()
        anim = obj.animation_data
        active = anim.action if anim else None
        if active is not None and cs.move_kind(active) == "NONE" and not cs.is_library_clip(obj, active):
            clip.action = active
        obj.gltf_clips_index = len(obj.gltf_clips) - 1
        return {"FINISHED"}


class OBJECT_OT_gltf_remove_clip(bpy.types.Operator):
    """Remove the selected action from this armature's glTF clip list"""

    bl_idname = "gltf_custom.remove_clip"
    bl_label = "Remove Clip"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = clip_owner(context.object)
        return obj is not None and len(obj.gltf_clips) > 0

    def execute(self, context):
        obj = clip_owner(context.object)
        index = min(max(obj.gltf_clips_index, 0), len(obj.gltf_clips) - 1)
        obj.gltf_clips.remove(index)
        obj.gltf_clips_index = max(0, min(index, len(obj.gltf_clips) - 1))
        return {"FINISHED"}


class OBJECT_OT_gltf_add_matching_clips(bpy.types.Operator):
    """Add every action that animates this armature's bones to its glTF clip list"""

    bl_idname = "gltf_custom.add_matching_clips"
    bl_label = "Add All Matching"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return clip_owner(context.object) is not None

    def execute(self, context):
        obj = clip_owner(context.object)
        # Empty rows are left behind when an action is deleted; clear them out.
        for index in reversed(range(len(obj.gltf_clips))):
            if obj.gltf_clips[index].action is None:
                obj.gltf_clips.remove(index)
        added = 0
        for action in sorted(matching_actions(obj), key=lambda item: item.name):
            if cs.is_library_clip(obj, action):
                continue
            obj.gltf_clips.add().action = action
            added += 1
        self.report({"INFO"}, f"Added {added} clip(s); the list holds {len(obj.gltf_clips)}")
        return {"FINISHED"}


class VIEW3D_PT_gltf_clips(bpy.types.Panel):
    """Panel in the 3D viewport sidebar for the selected character's glTF clip list."""

    bl_label = "Clips"
    bl_idname = "VIEW3D_PT_gltf_clips"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "glTF"

    @classmethod
    def poll(cls, context):
        return clip_owner(context.object) is not None

    def draw(self, context):
        layout = self.layout
        obj = clip_owner(context.object)
        layout.label(text=obj.name, icon="OUTLINER_OB_ARMATURE")
        row = layout.row()
        row.template_list(
            "DATA_UL_gltf_clips", "", obj, "gltf_clips", obj, "gltf_clips_index", rows=3,
        )
        col = row.column(align=True)
        col.operator("gltf_custom.add_clip", icon="ADD", text="")
        col.operator("gltf_custom.remove_clip", icon="REMOVE", text="")
        layout.operator("gltf_custom.add_matching_clips", icon="ACTION")
        layout.label(text="Exported with the armature, each as its own animation.")


# --- Cutscene data ---

class GltfCutsceneActor(bpy.types.PropertyGroup):
    """Marks an object as taking part in cutscenes."""

    enabled: BoolProperty(
        name="Cutscene Actor", default=False,
        description="Export this object's NLA strips with the cutscenes they fall in",
    )
    row: StringProperty(
        name="Engine Name",
        description=(
            "Name the engine finds this actor by (a level row id). Leave empty to "
            "use the object's name"
        ),
    )


class GltfCutsceneMove(bpy.types.PropertyGroup):
    """On an action the walk tool generated: what it was generated from. NLA
    strips hold no properties of their own, so a move's settings live here."""

    kind: EnumProperty(
        name="Kind",
        items=[
            ("NONE", "None", "An ordinary action"),
            ("MOVE", "Move", "Generated walk along a path"),
            ("HOME", "Home", "Where the actor stands before its first move"),
        ],
        default="NONE",
    )
    path: PointerProperty(name="Path", type=bpy.types.Object)
    ease: EnumProperty(name="Ease", items=EASE_ITEMS, default="LINEAR")
    turn: BoolProperty(name="Face Along Path", default=True)
    clip: PointerProperty(name="Clip", type=bpy.types.Action)


class GltfCutscene(bpy.types.PropertyGroup):
    """A named stretch of the scene timeline exported as one cutscene file."""

    name: StringProperty(name="Name", default="Cutscene")
    frame_start: IntProperty(name="Start", default=1, description="First frame of the cutscene")
    frame_end: IntProperty(name="End", default=250, description="Last frame of the cutscene")
    camera_mode: EnumProperty(
        name="Camera",
        items=[
            ("KEYS", "Keyframes", "A camera key at each shot's ends and at the camera's own keyframes"),
            ("BAKED", "Baked", "Sample every frame, so constraints and parents are followed exactly"),
            ("NONE", "None", "Leave the game's camera alone"),
        ],
        default="KEYS",
    )
    event_data: PointerProperty(
        name="Event Data", type=bpy.types.Text,
        description=(
            "Text holding a JSON object of event name to data, handed to the event's "
            "handlers. A list gives one entry to each event of that name in time order"
        ),
    )


class GltfCutscenes(bpy.types.PropertyGroup):
    """This scene's cutscenes and where they are exported."""

    cutscenes: CollectionProperty(type=GltfCutscene)
    index: IntProperty()
    directory: StringProperty(
        name="Folder", subtype="DIR_PATH", default="//cutscenes/",
        description="Folder the cutscene files are written to",
    )
    export_format: EnumProperty(
        name="Format",
        items=[
            ("GLB", "glTF Binary (.glb)", "One binary file per cutscene"),
            ("GLTF_SEPARATE", "glTF Separate (.gltf + .bin)", "Readable JSON, plus a .bin when the cutscene has one-off clips"),
        ],
        default="GLB",
    )


def active_cutscene(scene):
    data = scene.gltf_cutscenes
    if 0 <= data.index < len(data.cutscenes):
        return data.cutscenes[data.index]
    return None


# --- Generated actions ---

def new_object_action(name: str, obj):
    """An empty action ready to animate ``obj``. Its slot is named for the
    object, which is what makes an NLA strip pick it up by itself."""
    action = bpy.data.actions.new(name)
    if not hasattr(action, "slots"):  # Blender 4.2 / 4.3
        action.id_root = "OBJECT"
        return action
    action.slots.new(id_type="OBJECT", name=obj.name)
    action.layers.new("Layer").strips.new(type="KEYFRAME")
    return action


def _action_fcurves(action):
    """The writable fcurve collection of an action made by new_object_action."""
    if not hasattr(action, "slots"):
        return action.fcurves
    return action.layers[0].strips[0].channelbag(action.slots[0], ensure=True).fcurves


def _write_keys(action, channels: dict, frames) -> None:
    """Replace the action's curves with one linear key per frame:
    ``channels`` maps (data_path, index) to the values at ``frames``."""
    fcurves = _action_fcurves(action)
    for fcurve in list(fcurves):
        fcurves.remove(fcurve)
    frames = np.asarray(frames, dtype=np.float32)
    for (data_path, index), values in channels.items():
        fcurve = fcurves.new(data_path, index=index)
        fcurve.keyframe_points.add(len(frames))
        co = np.empty(len(frames) * 2, dtype=np.float32)
        co[0::2] = frames
        co[1::2] = values
        fcurve.keyframe_points.foreach_set("co", co)
        fcurve.keyframe_points.foreach_set("interpolation", [_LINEAR] * len(frames))
        fcurve.update()


def _assign_slot(strip, obj) -> None:
    """Make sure a strip drives ``obj``: Blender picks the slot itself only
    when the action offers an obvious one."""
    if not hasattr(strip, "action_slot") or strip.action_slot is not None:
        return
    handle = slot_handle_for(strip.action, obj)
    for slot in strip.action.slots:
        if slot.handle == handle:
            strip.action_slot = slot
            return


def _place_strip(strip, start: float, end: float) -> None:
    """Move a strip's ends without ever passing start beyond end."""
    if end >= strip.frame_end:
        strip.frame_end = end
        strip.frame_start = start
    else:
        strip.frame_start = start
    strip.frame_end = end


def _rotation_channels(obj, yaws, base_euler) -> dict:
    """Rotation curves turning the object to each yaw (about Blender Z), over
    the lean it has at home (base_euler's X and Y). The engine turns an actor
    the same way."""
    yaws = np.asarray(yaws, dtype=np.float64)
    if obj.rotation_mode == "QUATERNION":
        lean = Euler((base_euler[0], base_euler[1], 0.0), "XYZ").to_quaternion()
        turn_w, turn_z = np.cos(yaws / 2.0), np.sin(yaws / 2.0)
        return {
            ("rotation_quaternion", 0): turn_w * lean.w - turn_z * lean.z,
            ("rotation_quaternion", 1): turn_w * lean.x - turn_z * lean.y,
            ("rotation_quaternion", 2): turn_w * lean.y + turn_z * lean.x,
            ("rotation_quaternion", 3): turn_w * lean.z + turn_z * lean.w,
        }
    return {
        ("rotation_euler", 0): np.full(len(yaws), base_euler[0]),
        ("rotation_euler", 1): np.full(len(yaws), base_euler[1]),
        ("rotation_euler", 2): yaws,
    }


def _check_rotation_mode(obj) -> None:
    if obj.rotation_mode == "AXIS_ANGLE":
        raise ValueError(
            f"'{obj.name}' uses Axis Angle rotation; switch it to Quaternion or an Euler "
            f"mode to walk it along a path"
        )


def _parent_inverse(obj) -> Matrix:
    """World space into the space the object's location is expressed in."""
    if obj.parent is None:
        return Matrix.Identity(4)
    return (obj.parent.matrix_world @ obj.matrix_parent_inverse).inverted()


def _move_samples(obj, curve, count: int, ease_kind: str):
    """count + 1 positions and yaws along the curve, in the object's parent
    space. The yaw points the object's -Y (glTF +Z, the engine's forward)
    down the path."""
    world, _closed = cs.curve_polyline(curve)
    to_local = _parent_inverse(obj)
    local = cs.dedupe_points([tuple(to_local @ Vector(point)) for point in world])
    lengths = cs.polyline_lengths(local)
    total = float(lengths[-1])

    positions = np.empty((count + 1, 3))
    yaws = np.empty(count + 1)
    for i in range(count + 1):
        u = cs.ease(ease_kind, i / count)
        point, tangent = cs.sample_polyline(local, lengths, u * total)
        positions[i] = point
        yaws[i] = math.atan2(tangent[0], -tangent[1])
    return positions, np.unwrap(yaws)


# --- Move strips ---

def _all_strips(obj):
    anim = obj.animation_data
    if anim is None:
        return
    for track in anim.nla_tracks:
        for strip in track.strips:
            yield strip


def move_strips(obj) -> list:
    """The object's move strips in time order."""
    moves = [
        strip for strip in _all_strips(obj)
        if strip.action is not None and cs.move_kind(strip.action) == "MOVE"
    ]
    return sorted(moves, key=lambda strip: strip.frame_start)


def home_strip(obj):
    for strip in _all_strips(obj):
        if strip.action is not None and cs.move_kind(strip.action) == "HOME":
            return strip
    return None


def _check_not_tweaking(obj) -> None:
    anim = obj.animation_data
    if anim is not None and anim.use_tweak_mode:
        raise ValueError(
            f"'{obj.name}' is in NLA tweak mode — leave it (Tab in the NLA editor) first"
        )


def write_home(obj, action) -> None:
    """Store the object's current location and rotation as its home."""
    channels = {("location", i): [obj.location[i]] for i in range(3)}
    if obj.rotation_mode == "QUATERNION":
        channels.update(
            {("rotation_quaternion", i): [obj.rotation_quaternion[i]] for i in range(4)}
        )
    else:
        channels.update({("rotation_euler", i): [obj.rotation_euler[i]] for i in range(3)})
    _write_keys(action, channels, [0.0])


def ensure_move_track(obj, before_frame: float):
    """The object's move track, created with its home strip.

    Without a home the NLA shows the actor at the world origin before its
    first move: a track animating location has to say something at every
    frame, and "nothing yet" reads as the property's default. The home strip
    is a single key of where the actor stood when the track was made."""
    anim = obj.animation_data or obj.animation_data_create()
    track = anim.nla_tracks.get(cs.MOVE_TRACK)
    if track is None:
        track = anim.nla_tracks.new()
        track.name = cs.MOVE_TRACK
    if home_strip(obj) is None:
        action = new_object_action(f"{obj.name} home", obj)
        action.gltf_cutscene_move.kind = "HOME"
        write_home(obj, action)
        strip = track.strips.new("Home", int(math.floor(before_frame)) - 10, action)
        _assign_slot(strip, obj)
        strip.extrapolation = "HOLD"
    return track


def _home_state(obj, strip) -> tuple[float, tuple[float, float, float]]:
    """(yaw, euler) the actor has at home."""
    values: dict = {}
    for fcurve in _get_fcurves(strip.action) if strip is not None else ():
        values[(fcurve.data_path, fcurve.array_index)] = fcurve.evaluate(0.0)
    if ("rotation_quaternion", 0) in values:
        quat = Quaternion([values.get(("rotation_quaternion", i), 0.0) for i in range(4)])
        euler = quat.to_euler("XYZ")
        return euler.z, (euler.x, euler.y, euler.z)
    euler = tuple(
        values.get(("rotation_euler", i), obj.rotation_euler[i]) for i in range(3)
    )
    return euler[2], euler


def _rebuild_walk_track(obj, moves, scene) -> None:
    """Recreate the preview strips that loop each move's walk clip. The track
    is removed and re-added so it stays above whatever the user laid out."""
    tracks = obj.animation_data.nla_tracks
    stale = tracks.get(cs.WALK_TRACK)
    if stale is not None:
        tracks.remove(stale)

    walking = [
        strip for strip in moves
        if strip.action.gltf_cutscene_move.clip is not None
        and strip.action.gltf_cutscene_move.path is not None
    ]
    if not walking:
        return
    track = tracks.new()
    track.name = cs.WALK_TRACK
    fps = scene_fps(scene)
    for strip in walking:
        meta = strip.action.gltf_cutscene_move
        clip = meta.clip
        frames = strip.frame_end - strip.frame_start
        length = float(cs.polyline_lengths(cs.curve_polyline(meta.path)[0])[-1])
        rate = cs.walk_rate(length, frames / fps, cs.clip_speed(obj, clip))
        try:
            preview = track.strips.new(clip.name, int(strip.frame_start), clip)
        except RuntimeError:
            continue  # overlapping moves: the earlier preview keeps the track
        _assign_slot(preview, obj)
        clip_frames = max(preview.action_frame_end - preview.action_frame_start, 1e-3)
        preview.scale = min(max(1.0 / rate, 0.0001), 1000.0)
        preview.repeat = min(max(frames / (clip_frames * preview.scale), 0.1), 1000.0)
        preview.extrapolation = "NOTHING"
        preview.blend_type = "REPLACE"


def walk_track_buried(obj) -> bool:
    """Whether a track laid out after the last refresh sits above the walk
    preview. Its strips then hide the walk cycle, and a track's first strip
    holds its first frame back to the start of the timeline."""
    anim = obj.animation_data
    if anim is None:
        return False
    tracks = list(anim.nla_tracks)
    names = [track.name for track in tracks]
    if cs.WALK_TRACK not in names:
        return False
    above = tracks[names.index(cs.WALK_TRACK) + 1:]
    return any(not track.mute and len(track.strips) for track in above)


def raise_walk_tracks(scene) -> int:
    """Put every actor's walk preview back on top. Returns how many moved."""
    raised = 0
    for obj in cs.find_actors(scene):
        if walk_track_buried(obj) and not obj.animation_data.use_tweak_mode:
            _rebuild_walk_track(obj, move_strips(obj), scene)
            raised += 1
    return raised


def refresh_object(obj, scene) -> int:
    """Regenerate every move of the object from its path and strip.

    The strip's rectangle is the move: keys are refilled to span exactly its
    frames, so a strip that was dragged or stretched plays the whole path in
    its new time. Returns the number of moves refreshed."""
    if obj.animation_data is None:
        return 0
    _check_not_tweaking(obj)
    moves = move_strips(obj)
    if not moves:
        _rebuild_walk_track(obj, moves, scene)
        return 0
    _check_rotation_mode(obj)

    home = home_strip(obj)
    yaw, base_euler = _home_state(obj, home)

    seen: list = []
    for strip in moves:
        # Duplicated strips share one action; each move needs its own keys.
        if strip.action in seen:
            strip.action = strip.action.copy()
            _assign_slot(strip, obj)
        seen.append(strip.action)
        action = strip.action
        meta = action.gltf_cutscene_move
        if meta.path is None or meta.path.type != "CURVE":
            continue

        start = strip.frame_start
        count = max(1, int(round(strip.frame_end - strip.frame_start)))
        positions, yaws = _move_samples(obj, meta.path, count, meta.ease.lower())
        if meta.turn:
            # Arrive by the short way round from wherever it was facing.
            yaws += round((yaw - yaws[0]) / math.tau) * math.tau
            yaw = float(yaws[-1])
        else:
            yaws[:] = yaw

        channels = {("location", i): positions[:, i] for i in range(3)}
        channels.update(_rotation_channels(obj, yaws, base_euler))
        _write_keys(action, channels, np.arange(count + 1))

        strip.scale = 1.0
        strip.repeat = 1.0
        strip.use_reverse = False
        strip.action_frame_start = 0.0
        strip.action_frame_end = float(count)
        _place_strip(strip, start, start + count)
        strip.extrapolation = "HOLD_FORWARD"

    # A move dragged to before the home strip would leave the actor at the
    # origin again until home begins; keep home ahead of the first move.
    if home is not None and home.frame_end > moves[0].frame_start:
        first = int(math.floor(moves[0].frame_start)) - 10
        _place_strip(home, first, first + 1)

    _rebuild_walk_track(obj, moves, scene)
    return len(moves)


def refresh_moves(scene) -> int:
    """Refresh every actor's moves. Returns the number of moves refreshed."""
    return sum(refresh_object(obj, scene) for obj in scene.objects if obj.animation_data)


def create_move(obj, curve, frame_start: float, frame_end: float, scene, *,
                ease: str = "LINEAR", turn: bool = True, clip=None):
    """Add a move strip walking ``obj`` along ``curve`` over the frames."""
    cs.curve_polyline(curve)  # reject a bad path before touching the object
    _check_rotation_mode(obj)
    _check_not_tweaking(obj)

    track = ensure_move_track(obj, frame_start)
    count = max(1, int(round(frame_end - frame_start)))
    action = new_object_action(f"{obj.name} move", obj)
    meta = action.gltf_cutscene_move
    meta.kind = "MOVE"
    meta.path = curve
    meta.ease = ease
    meta.turn = turn
    meta.clip = clip
    # Placeholder keys give the strip its length; refresh_object fills them in.
    _write_keys(action, {("location", 0): np.zeros(count + 1)}, np.arange(count + 1))

    try:
        strip = track.strips.new(action.name, int(round(frame_start)), action)
    except RuntimeError:
        bpy.data.actions.remove(action)
        raise ValueError(
            f"No room on the '{cs.MOVE_TRACK}' track of '{obj.name}' at frame "
            f"{int(round(frame_start))} — another move is already there"
        ) from None
    _assign_slot(strip, obj)
    obj.gltf_cutscene.enabled = True
    refresh_object(obj, scene)
    return strip


# --- Path sketching ---

def _paths_collection(scene):
    collection = bpy.data.collections.get(PATHS_COLLECTION)
    if collection is None:
        collection = bpy.data.collections.new(PATHS_COLLECTION)
    if collection.name not in scene.collection.children:
        scene.collection.children.link(collection)
    return collection


def _grease_pencils():
    """Grease Pencil v3 datablocks: under their own name while Blender 4.3 to
    4.5 still kept the legacy kind in ``grease_pencils``."""
    return getattr(bpy.data, "grease_pencils_v3", bpy.data.grease_pencils)


def find_sketch(scene):
    for obj in scene.objects:
        if obj.get(cs.HELPER_PROP) == "sketch" and obj.type == "GREASEPENCIL":
            return obj
    return None


def ensure_sketch(context):
    """The scene's path sketch: a Grease Pencil object with a layer, a frame
    to draw on and a visible stroke material."""
    scene = context.scene
    sketch = find_sketch(scene)
    if sketch is None:
        data = _grease_pencils().new(SKETCH_NAME)
        material = bpy.data.materials.new(SKETCH_NAME)
        bpy.data.materials.create_gpencil_data(material)
        material.grease_pencil.color = (1.0, 0.35, 0.0, 1.0)
        data.materials.append(material)
        sketch = bpy.data.objects.new(SKETCH_NAME, data)
        sketch[cs.HELPER_PROP] = "sketch"
        sketch.show_in_front = True
        _paths_collection(scene).objects.link(sketch)

    layers = sketch.data.layers
    layer = layers.active or (layers[0] if len(layers) else layers.new("Paths"))
    layers.active = layer
    if layer.current_frame() is None:
        try:
            layer.frames.new(scene.frame_current)
        except RuntimeError:
            pass  # a key already sits on this frame
    return sketch


def sketch_strokes_world(sketch) -> list:
    """Every stroke of the sketch as world-space points. All frames are read:
    drawing with auto-key on spreads strokes over several."""
    strokes = []
    for layer in sketch.data.layers:
        matrix = sketch.matrix_world @ layer.matrix_local
        for frame in layer.frames:
            drawing = frame.drawing
            if drawing is None:
                continue
            for stroke in drawing.strokes:
                points = np.array([tuple(matrix @ point.position) for point in stroke.points])
                if len(points) >= 2:
                    strokes.append(points)
    return strokes


def clear_sketch(sketch) -> None:
    for layer in sketch.data.layers:
        for frame in layer.frames:
            if frame.drawing is not None and len(frame.drawing.strokes):
                frame.drawing.remove_strokes()


def snap_points(context, points, lift: float = 0.5, reach: float = 100.0) -> np.ndarray:
    """Drop each point onto the surface under it. The ray starts just above
    the point, not from the sky, so a roof or a branch overhead is not hit."""
    scene = context.scene
    depsgraph = context.evaluated_depsgraph_get()
    out = np.array(points, dtype=np.float64).reshape(-1, 3)
    down = Vector((0.0, 0.0, -1.0))
    for i, point in enumerate(out):
        origin = Vector(point) + Vector((0.0, 0.0, lift))
        hit, location, *_rest = scene.ray_cast(depsgraph, origin, down, distance=lift + reach)
        if hit:
            out[i] = location
    return out


def _set_knots(spline, points) -> None:
    """Make the spline pass through exactly these points, auto handles."""
    missing = len(points) - len(spline.bezier_points)
    if missing > 0:
        spline.bezier_points.add(missing)
    for knot, point in zip(spline.bezier_points, points):
        knot.co = point
        knot.handle_left_type = knot.handle_right_type = "AUTO"


def polyline_to_curve(context, points, name: str = "Walk Path"):
    """A bezier curve object through the points (auto handles), tagged as a
    cutscene helper and filed under the paths collection."""
    data = bpy.data.curves.new(name, "CURVE")
    data.dimensions = "3D"
    _set_knots(data.splines.new("BEZIER"), points)
    obj = bpy.data.objects.new(name, data)
    obj[cs.HELPER_PROP] = "path"
    obj.show_in_front = True
    _paths_collection(context.scene).objects.link(obj)
    return obj


def fit_curve(context, stroke, tolerance: float, name: str = "Walk Path"):
    """A path curve that stays within ``tolerance`` of the stroke.

    Simplifying the stroke is not enough on its own: an auto-handle bezier
    through the few points left swings wide of every corner. So start from
    the simplified points and keep adding the stroke point the curve misses
    most as a knot until it follows the stroke."""
    stroke = cs.dedupe_points(stroke)
    keep = [int(i) for i in cs.rdp_indices(stroke, tolerance)]
    curve = polyline_to_curve(context, stroke[keep], name)
    spline = curve.data.splines[0]
    while len(keep) < len(stroke):
        missed = cs.polyline_distances(stroke, cs.curve_polyline(curve)[0])
        worst = int(np.argmax(missed))
        if missed[worst] <= tolerance or worst in keep:
            break
        keep = sorted(keep + [worst])
        _set_knots(spline, stroke[keep])
    return curve


def strokes_to_curves(context, strokes, *, simplify: float = 0.05, snap: bool = True) -> list:
    """One path curve per sketched stroke."""
    curves = []
    for stroke in strokes:
        stroke = cs.dedupe_points(stroke)
        if len(stroke) < 2:
            continue
        if snap:
            stroke = snap_points(context, stroke)
        curves.append(fit_curve(context, stroke, simplify))
    return curves


class VIEW3D_OT_gltf_draw_path(bpy.types.Operator):
    """Sketch walk paths on the ground with Grease Pencil. Finish Path turns the strokes into curves"""

    bl_idname = "gltf_custom.draw_path"
    bl_label = "Draw Walk Path"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        if bpy.app.version < (4, 3, 0):
            self.report({"ERROR"}, "Sketching paths needs Blender 4.3 or newer; draw a curve instead")
            return {"CANCELLED"}
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        sketch = ensure_sketch(context)
        for obj in context.selected_objects:
            obj.select_set(False)
        sketch.hide_set(False)
        sketch.select_set(True)
        context.view_layer.objects.active = sketch

        settings = context.scene.tool_settings
        if _PLACEMENT_PROP not in sketch:
            sketch[_PLACEMENT_PROP] = settings.gpencil_stroke_placement_view3d
        settings.gpencil_stroke_placement_view3d = "SURFACE"

        bpy.ops.object.mode_set(mode="PAINT_GREASE_PENCIL")
        try:
            bpy.ops.wm.tool_set_by_id(name="builtin.brush")
        except (RuntimeError, TypeError):
            pass  # no 3D viewport in this context; the mode's own default tool stays
        self.report({"INFO"}, "Draw on the ground, then press Finish Path")
        return {"FINISHED"}


class VIEW3D_OT_gltf_finish_path(bpy.types.Operator):
    """Turn the sketched strokes into walk path curves"""

    bl_idname = "gltf_custom.finish_path"
    bl_label = "Finish Path"
    bl_options = {"REGISTER", "UNDO"}

    simplify: FloatProperty(
        name="Simplify", min=0.0, default=0.05, unit="LENGTH",
        description="How far the path may stray from the stroke; larger gives fewer control points",
    )
    snap: BoolProperty(
        name="Snap to Ground", default=True,
        description="Drop the path's points onto the surface under them",
    )

    @classmethod
    def poll(cls, context):
        return find_sketch(context.scene) is not None

    def execute(self, context):
        sketch = find_sketch(context.scene)
        strokes = sketch_strokes_world(sketch)
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        settings = context.scene.tool_settings
        if _PLACEMENT_PROP in sketch:
            settings.gpencil_stroke_placement_view3d = sketch[_PLACEMENT_PROP]
            del sketch[_PLACEMENT_PROP]
        if not strokes:
            self.report({"WARNING"}, "Nothing was drawn — use Draw Walk Path first")
            return {"CANCELLED"}

        curves = strokes_to_curves(context, strokes, simplify=self.simplify, snap=self.snap)
        clear_sketch(sketch)
        for obj in context.selected_objects:
            obj.select_set(False)
        for curve in curves:
            curve.select_set(True)
        if curves:
            context.view_layer.objects.active = curves[-1]
        self.report({"INFO"}, f"Made {len(curves)} walk path(s)")
        return {"FINISHED"}


class VIEW3D_OT_gltf_snap_path(bpy.types.Operator):
    """Drop the control points of the selected curves onto the surface under them"""

    bl_idname = "gltf_custom.snap_path"
    bl_label = "Snap Path to Ground"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return any(obj.type == "CURVE" for obj in context.selected_objects)

    def execute(self, context):
        count = 0
        for obj in context.selected_objects:
            if obj.type != "CURVE":
                continue
            matrix = obj.matrix_world
            inverse = matrix.inverted()
            for spline in obj.data.splines:
                knots = spline.bezier_points if spline.type == "BEZIER" else spline.points
                if not len(knots):
                    continue
                world = [tuple(matrix @ knot.co.xyz) for knot in knots]
                snapped = snap_points(context, world)
                for knot, point in zip(knots, snapped):
                    local = inverse @ Vector(point)
                    if spline.type == "BEZIER":
                        knot.co = local
                        knot.handle_left_type = knot.handle_right_type = "AUTO"
                    else:
                        knot.co = (*local, knot.co.w)
                count += len(knots)
        self.report({"INFO"}, f"Snapped {count} point(s)")
        return {"FINISHED"}


# --- Walk operators ---

def _walk_selection(context):
    """(actor, curve) when exactly one of each is selected. A character's
    mesh counts as its armature, alone or picked along with it."""
    selected = [obj for obj in context.selected_objects if obj.get(cs.HELPER_PROP) != "sketch"]
    curves = [obj for obj in selected if obj.type == "CURVE"]
    actors = _unique(actor_for(obj) for obj in selected if obj.type != "CURVE")
    if len(curves) == 1 and len(actors) == 1:
        return actors[0], curves[0]
    return None, None


class OBJECT_OT_gltf_walk_path(bpy.types.Operator):
    """Walk the selected character along the selected path: adds a move strip to its NLA"""

    bl_idname = "gltf_custom.walk_path"
    bl_label = "Walk Along Path"
    bl_options = {"REGISTER", "UNDO"}

    clip: StringProperty(
        name="Clip", description="Action looped while walking. Leave empty to only move",
    )
    speed: FloatProperty(
        name="Speed", min=0.0, default=0.0, unit="VELOCITY",
        description="Ground speed; sets how long the walk lasts. 0 uses the clip's speed",
    )
    ease: EnumProperty(name="Ease", items=EASE_ITEMS, default="LINEAR")
    turn: BoolProperty(
        name="Face Along Path", default=True,
        description="Turn the character to face where it is going",
    )
    frame_start: IntProperty(name="Start Frame", default=1)

    def invoke(self, context, event):
        actor, curve = _walk_selection(context)
        if actor is None:
            self.report({"ERROR"}, "Select one character and one path curve")
            return {"CANCELLED"}
        self.frame_start = context.scene.frame_current
        self.clip = ""
        self.speed = cs.DEFAULT_WALK_SPEED
        for clip in actor.gltf_clips:
            if clip.action is not None and clip.speed > 0.0:
                self.clip = clip.action.name
                self.speed = clip.speed
                break
        return context.window_manager.invoke_props_dialog(self, width=340)

    def draw(self, context):
        layout = self.layout
        actor, curve = _walk_selection(context)
        if actor is not None:
            layout.label(text=f"{actor.name} along {curve.name}", icon="CURVE_PATH")
        col = layout.column()
        col.prop_search(self, "clip", bpy.data, "actions", icon="ACTION")
        col.prop(self, "speed")
        col.prop(self, "ease")
        col.prop(self, "turn")
        col.prop(self, "frame_start")

    def execute(self, context):
        actor, curve = _walk_selection(context)
        if actor is None:
            self.report({"ERROR"}, "Select one character and one path curve")
            return {"CANCELLED"}
        clip = bpy.data.actions.get(self.clip) if self.clip else None
        if self.clip and clip is None:
            self.report({"ERROR"}, f"No action named '{self.clip}'")
            return {"CANCELLED"}

        scene = context.scene
        try:
            length = float(cs.polyline_lengths(cs.curve_polyline(curve)[0])[-1])
            speed = self.speed
            if speed <= 0.0:
                speed = (cs.clip_speed(actor, clip) if clip else 0.0) or cs.DEFAULT_WALK_SPEED
            frames = max(1, int(round(length / speed * scene_fps(scene))))
            create_move(
                actor, curve, self.frame_start, self.frame_start + frames, scene,
                ease=self.ease, turn=self.turn, clip=clip,
            )
        except ValueError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"{actor.name} walks {length:.2f} m over {frames} frames")
        return {"FINISHED"}


class SCENE_OT_gltf_refresh_moves(bpy.types.Operator):
    """Regenerate every move from its path and strip. Run after editing a path or retiming a move strip"""

    bl_idname = "gltf_custom.refresh_moves"
    bl_label = "Refresh Moves"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            count = refresh_moves(context.scene)
        except ValueError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Refreshed {count} move(s)")
        return {"FINISHED"}


class OBJECT_OT_gltf_set_home(bpy.types.Operator):
    """Use the active object's current place as where it stands before its first move"""

    bl_idname = "gltf_custom.set_home"
    bl_label = "Set Home From Current"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.object is not None and home_strip(actor_for(context.object)) is not None

    def execute(self, context):
        obj = actor_for(context.object)
        try:
            _check_not_tweaking(obj)
            _check_rotation_mode(obj)
            write_home(obj, home_strip(obj).action)
            refresh_object(obj, context.scene)
        except ValueError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


# --- Cutscene list operators ---

class SCENE_OT_gltf_add_cutscene(bpy.types.Operator):
    """Add a cutscene covering the scene's frame range (the preview range when one is set)"""

    bl_idname = "gltf_custom.add_cutscene"
    bl_label = "Add Cutscene"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        data = scene.gltf_cutscenes
        taken = {item.name for item in data.cutscenes}
        name, number = "cutscene", 1
        while name in taken:
            number += 1
            name = f"cutscene_{number}"
        item = data.cutscenes.add()
        item.name = name
        if scene.use_preview_range:
            item.frame_start, item.frame_end = scene.frame_preview_start, scene.frame_preview_end
        else:
            item.frame_start, item.frame_end = scene.frame_start, scene.frame_end
        data.index = len(data.cutscenes) - 1
        return {"FINISHED"}


class SCENE_OT_gltf_remove_cutscene(bpy.types.Operator):
    """Remove the selected cutscene. Its strips, paths and markers stay"""

    bl_idname = "gltf_custom.remove_cutscene"
    bl_label = "Remove Cutscene"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return active_cutscene(context.scene) is not None

    def execute(self, context):
        data = context.scene.gltf_cutscenes
        data.cutscenes.remove(data.index)
        data.index = max(0, min(data.index, len(data.cutscenes) - 1))
        return {"FINISHED"}


class SCENE_OT_gltf_cutscene_preview_range(bpy.types.Operator):
    """Set the timeline's preview range to the selected cutscene and jump to its start"""

    bl_idname = "gltf_custom.cutscene_preview_range"
    bl_label = "Preview This Cutscene"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return active_cutscene(context.scene) is not None

    def execute(self, context):
        scene = context.scene
        item = active_cutscene(scene)
        try:
            raise_walk_tracks(scene)
        except ValueError as error:
            self.report({"WARNING"}, f"Walk previews not refreshed: {error}")
        scene.use_preview_range = True
        scene.frame_preview_start = item.frame_start
        scene.frame_preview_end = max(item.frame_end, item.frame_start + 1)
        scene.frame_set(item.frame_start)
        return {"FINISHED"}


class OBJECT_OT_gltf_add_cutscene_actors(bpy.types.Operator):
    """Mark the selected objects as cutscene actors, so their NLA strips are exported"""

    bl_idname = "gltf_custom.add_cutscene_actors"
    bl_label = "Add Selected as Actors"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(context.selected_objects)

    def execute(self, context):
        count = 0
        picked = [obj for obj in context.selected_objects if not obj.get(cs.HELPER_PROP) and obj.type != "CURVE"]
        for obj in _unique(actor_for(obj) for obj in picked):
            obj.gltf_cutscene.enabled = True
            count += 1
        self.report({"INFO"}, f"{count} object(s) marked as actors")
        return {"FINISHED"}


class OBJECT_OT_gltf_remove_cutscene_actor(bpy.types.Operator):
    """Stop exporting this object with cutscenes. Its strips stay"""

    bl_idname = "gltf_custom.remove_cutscene_actor"
    bl_label = "Remove Actor"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty()

    def execute(self, context):
        obj = context.scene.objects.get(self.name)
        if obj is not None:
            obj.gltf_cutscene.enabled = False
        return {"FINISHED"}


STASH_TRACK = "[Action Stash]"


def stash_action(obj) -> "bpy.types.Action | None":
    """Move the object's active action into a muted NLA track, as Blender's own
    Stash does, so its strips play on their own. Returns the action."""
    anim = obj.animation_data
    action = anim.action if anim is not None else None
    if action is None:
        return None
    slot = getattr(anim, "action_slot", None)
    track = anim.nla_tracks.new()
    track.name = STASH_TRACK
    strip = track.strips.new(action.name, int(action.frame_range[0]), action)
    if slot is not None and hasattr(strip, "action_slot"):
        try:
            strip.action_slot = slot
        except (AttributeError, RuntimeError, TypeError):
            pass
    track.mute = True
    anim.action = None
    return action


class OBJECT_OT_gltf_stash_actor_action(bpy.types.Operator):
    """Move this actor's active action into a muted NLA track, so Blender plays its strips as the engine will"""

    bl_idname = "gltf_custom.stash_actor_action"
    bl_label = "Stash Active Action"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty()

    def execute(self, context):
        obj = context.scene.objects.get(self.name)
        anim = obj.animation_data if obj is not None else None
        if anim is None or anim.action is None or cs.move_kind(anim.action) != "NONE":
            return {"CANCELLED"}
        action = stash_action(obj)
        self.report({"INFO"}, f"Stashed '{action.name}' on {obj.name}: it stays in the NLA, muted")
        return {"FINISHED"}


class SCENE_OT_gltf_export_cutscene(bpy.types.Operator):
    """Write the cutscene as a glTF file the engine loads, plays and frees"""

    bl_idname = "gltf_custom.export_cutscene"
    bl_label = "Export Cutscene"

    all: BoolProperty(
        name="All", default=False, description="Export every cutscene of the scene",
    )

    @classmethod
    def poll(cls, context):
        return active_cutscene(context.scene) is not None

    def execute(self, context):
        scene = context.scene
        data = scene.gltf_cutscenes
        targets = list(data.cutscenes) if self.all else [active_cutscene(scene)]
        written = []
        for item in targets:
            try:
                path, warnings = cs.export_cutscene(
                    context, item, data.directory, data.export_format,
                )
            except ValueError as error:
                self.report({"ERROR"}, f"{item.name}: {error}")
                return {"CANCELLED"}
            for warning in warnings:
                self.report({"WARNING"}, f"{item.name}: {warning}")
            written.append(path)
        self.report({"INFO"}, "Exported " + ", ".join(written))
        return {"FINISHED"}


# --- Panel ---

class VIEW3D_UL_gltf_cutscenes(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        row = layout.row(align=True)
        row.prop(item, "name", text="", emboss=False, icon="SEQUENCE")
        row.label(text=f"{item.frame_start}-{item.frame_end}")


def _unmarked_with_strips(scene, item) -> list:
    """Objects with strips in the cutscene that are not marked as actors."""
    found = []
    for obj in scene.objects:
        if obj.animation_data is None or obj.gltf_cutscene.enabled:
            continue
        if next(cs.strips_in_range(obj, item.frame_start, item.frame_end), None) is not None:
            found.append(obj)
    return found


class VIEW3D_PT_gltf_cutscene(bpy.types.Panel):
    """Panel in the 3D viewport sidebar for authoring and exporting cutscenes."""

    bl_label = "Cutscenes"
    bl_idname = "VIEW3D_PT_gltf_cutscene"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "glTF"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        data = scene.gltf_cutscenes

        row = layout.row()
        row.template_list(
            "VIEW3D_UL_gltf_cutscenes", "", data, "cutscenes", data, "index", rows=2,
        )
        col = row.column(align=True)
        col.operator("gltf_custom.add_cutscene", icon="ADD", text="")
        col.operator("gltf_custom.remove_cutscene", icon="REMOVE", text="")

        item = active_cutscene(scene)
        if item is not None:
            col = layout.column(align=True)
            col.use_property_split = True
            col.use_property_decorate = False
            col.prop(item, "frame_start")
            col.prop(item, "frame_end")
            col.prop(item, "camera_mode")
            col.prop(item, "event_data")
            layout.operator("gltf_custom.cutscene_preview_range", icon="PREVIEW_RANGE")

        header, body = layout.panel("GLTF_cutscene_actors", default_closed=False)
        header.label(text="Actors")
        if body:
            for obj in cs.find_actors(scene):
                row = body.row(align=True)
                row.label(text=obj.name, icon="OUTLINER_OB_ARMATURE" if obj.type == "ARMATURE" else "OBJECT_DATA")
                row.prop(obj.gltf_cutscene, "row", text="")
                row.operator("gltf_custom.remove_cutscene_actor", text="", icon="X").name = obj.name
                covering = cs.covering_action(obj)
                if covering is not None:
                    warn = body.row(align=True)
                    warn.label(text=f"'{covering.name}' plays over its strips", icon="ERROR")
                    warn.operator("gltf_custom.stash_actor_action", text="Stash").name = obj.name
                if walk_track_buried(obj):
                    warn = body.row(align=True)
                    warn.label(text="A track above hides the walk", icon="ERROR")
                    warn.operator("gltf_custom.refresh_moves", text="Refresh")
            body.operator("gltf_custom.add_cutscene_actors", icon="ADD")
            if item is not None:
                for obj in _unmarked_with_strips(scene, item):
                    body.label(text=f"{obj.name} has strips here but is not an actor", icon="INFO")

        header, body = layout.panel("GLTF_cutscene_paths", default_closed=False)
        header.label(text="Walk Paths")
        if body:
            sketch = find_sketch(scene)
            sketching = (
                sketch is not None and context.object == sketch
                and context.mode == "PAINT_GREASE_PENCIL"
            )
            col = body.column(align=True)
            col.operator("gltf_custom.draw_path", icon="GREASEPENCIL", depress=sketching)
            col.operator("gltf_custom.finish_path", icon="CHECKMARK")
            body.operator("gltf_custom.snap_path", icon="SNAP_FACE")
            col = body.column(align=True)
            col.operator("gltf_custom.walk_path", icon="CURVE_PATH")
            col.operator("gltf_custom.refresh_moves", icon="FILE_REFRESH")
            col.operator("gltf_custom.set_home", icon="HOME")

        header, body = layout.panel("GLTF_cutscene_export", default_closed=False)
        header.label(text="Export")
        if body:
            body.prop(data, "directory", text="")
            body.prop(data, "export_format", text="")
            row = body.row(align=True)
            row.operator("gltf_custom.export_cutscene", icon="EXPORT").all = False
            row.operator("gltf_custom.export_cutscene", text="Export All").all = True


_CLASSES = (
    GltfClip,
    GltfCutsceneActor,
    GltfCutsceneMove,
    GltfCutscene,
    GltfCutscenes,
    DATA_UL_gltf_clips,
    VIEW3D_UL_gltf_cutscenes,
    OBJECT_OT_gltf_add_clip,
    OBJECT_OT_gltf_remove_clip,
    OBJECT_OT_gltf_add_matching_clips,
    VIEW3D_OT_gltf_draw_path,
    VIEW3D_OT_gltf_finish_path,
    VIEW3D_OT_gltf_snap_path,
    OBJECT_OT_gltf_walk_path,
    SCENE_OT_gltf_refresh_moves,
    OBJECT_OT_gltf_set_home,
    SCENE_OT_gltf_add_cutscene,
    SCENE_OT_gltf_remove_cutscene,
    SCENE_OT_gltf_cutscene_preview_range,
    OBJECT_OT_gltf_add_cutscene_actors,
    OBJECT_OT_gltf_remove_cutscene_actor,
    OBJECT_OT_gltf_stash_actor_action,
    SCENE_OT_gltf_export_cutscene,
    VIEW3D_PT_gltf_cutscene,
    VIEW3D_PT_gltf_clips,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Object.gltf_clips = CollectionProperty(type=GltfClip)
    bpy.types.Object.gltf_clips_index = IntProperty()
    bpy.types.Object.gltf_cutscene = PointerProperty(type=GltfCutsceneActor)
    bpy.types.Action.gltf_cutscene_move = PointerProperty(type=GltfCutsceneMove)
    bpy.types.Scene.gltf_cutscenes = PointerProperty(type=GltfCutscenes)


def unregister():
    del bpy.types.Scene.gltf_cutscenes
    del bpy.types.Action.gltf_cutscene_move
    del bpy.types.Object.gltf_cutscene
    del bpy.types.Object.gltf_clips_index
    del bpy.types.Object.gltf_clips
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
