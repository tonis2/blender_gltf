"""CUSTOM_cutscene: one cutscene as a small, mesh-less glTF.

The file is a timeline, not a baked animation. Actors play clips that already
live in their own asset (referenced by animation name), walk along paths, and
only the acting that exists for this cutscene alone is stored here as real
glTF animations on stub nodes. Camera shots and timed events ride along.

Authoring lives in cutscene_tools.py; this module only reads what is there:
NLA strips, the move metadata on generated actions, timeline markers and
cameras. See extensions/cutscene.md for the format and the Blender workflow.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from ..gltf.buffer import BufferBuilder
from ..gltf.serialize import write_glb, write_gltf
from ..gltf.types import Asset, Gltf, Node, Scene
from .animation import (
    AnimationExporter,
    _get_fcurves,
    action_bone_names,
    action_first_frame,
    action_has_object_channels,
    slot_handle_for,
)
from .converter import convert_location
from .scene import camera_yfov

if TYPE_CHECKING:
    import bpy


EXT_CUTSCENE = "CUSTOM_cutscene"

# ID property tagging the path sketch and the curves made from it, so the
# scene export leaves them out.
HELPER_PROP = "gltf_cutscene_helper"
# NLA tracks the walk tool owns. Move strips are found by their action, not
# by this name; the walk-clip track is preview only and never exported.
MOVE_TRACK = "Cutscene Move"
WALK_TRACK = "Cutscene Walk Clips"

EASES = ("linear", "in", "out", "inout")
DEFAULT_WALK_SPEED = 1.4  # m/s, an unhurried human walk
DEFAULT_FOCUS = 5.0  # camera target distance when nothing says what it looks at


# --- Pure helpers (no scene state) ---

def ease(kind: str, u: float) -> float:
    """The engine's easing curves, so a Blender preview and the game agree."""
    if kind == "in":
        return u * u
    if kind == "out":
        return 1.0 - (1.0 - u) * (1.0 - u)
    if kind == "inout":
        return u * u * (3.0 - 2.0 * u)
    return u


def dedupe_points(points) -> np.ndarray:
    """Drop consecutive repeats, which would be zero-length segments."""
    points = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    if len(points) < 2:
        return points
    keep = np.ones(len(points), dtype=bool)
    keep[1:] = np.linalg.norm(np.diff(points, axis=0), axis=1) > 1e-9
    return points[keep]


def polyline_lengths(points) -> np.ndarray:
    """Distance along the polyline at each point; the last entry is its length."""
    points = np.asarray(points, dtype=np.float64)
    steps = np.linalg.norm(np.diff(points, axis=0), axis=1)
    return np.concatenate(([0.0], np.cumsum(steps)))


def sample_polyline(points, lengths, distance: float) -> tuple[np.ndarray, np.ndarray]:
    """Point and unit tangent at ``distance`` along the polyline."""
    total = float(lengths[-1])
    distance = min(max(float(distance), 0.0), total)
    index = int(np.searchsorted(lengths, distance, side="right")) - 1
    index = min(max(index, 0), len(points) - 2)
    span = lengths[index + 1] - lengths[index]
    step = points[index + 1] - points[index]
    t = (distance - lengths[index]) / span if span > 1e-12 else 0.0
    norm = float(np.linalg.norm(step))
    tangent = step / norm if norm > 1e-12 else np.array([0.0, -1.0, 0.0])
    return points[index] + step * t, tangent


def resample_polyline(points, spacing: float = 0.25) -> tuple[np.ndarray, float]:
    """Evenly spaced points along the polyline (both ends kept exactly) and
    its length. The spacing is rounded so the points divide the length."""
    points = dedupe_points(points)
    if len(points) < 2:
        raise ValueError("a path needs at least two distinct points")
    lengths = polyline_lengths(points)
    total = float(lengths[-1])
    count = max(2, int(math.ceil(total / max(spacing, 1e-4))) + 1)
    at = np.linspace(0.0, total, count)
    out = np.stack([np.interp(at, lengths, points[:, axis]) for axis in range(3)], axis=1)
    return out, total


def rdp_indices(points, epsilon: float) -> np.ndarray:
    """Ramer-Douglas-Peucker: indices of the fewest points whose polyline
    stays within ``epsilon`` of the original."""
    points = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    if len(points) < 3 or epsilon <= 0.0:
        return np.arange(len(points))
    keep = np.zeros(len(points), dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last - first < 2:
            continue
        a, b = points[first], points[last]
        chord = b - a
        length = float(np.linalg.norm(chord))
        inner = points[first + 1:last]
        if length < 1e-12:
            distances = np.linalg.norm(inner - a, axis=1)
        else:
            distances = np.linalg.norm(np.cross(inner - a, chord), axis=1) / length
        worst = int(np.argmax(distances))
        if distances[worst] > epsilon:
            split = first + 1 + worst
            keep[split] = True
            stack.append((first, split))
            stack.append((split, last))
    return np.flatnonzero(keep)


def rdp(points, epsilon: float) -> np.ndarray:
    """The points rdp_indices() keeps."""
    points = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    return points[rdp_indices(points, epsilon)]


def polyline_distances(points, polyline) -> np.ndarray:
    """Distance from each point to the nearest place on the polyline."""
    points = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    polyline = np.asarray(polyline, dtype=np.float64).reshape(-1, 3)
    best = np.full(len(points), np.inf)
    for a, b in zip(polyline[:-1], polyline[1:]):
        step = b - a
        squared = float(step @ step)
        t = np.clip((points - a) @ step / squared, 0.0, 1.0) if squared > 1e-18 else 0.0
        nearest = a + np.outer(t, step) if squared > 1e-18 else a
        best = np.minimum(best, np.linalg.norm(points - nearest, axis=1))
    return best


def walk_rate(length: float, seconds: float, clip_speed: float) -> float:
    """Playback rate that makes a walk clip's stride match the ground covered:
    1 when the clip does not say how fast it travels."""
    if clip_speed <= 1e-6 or seconds <= 1e-6:
        return 1.0
    return (length / seconds) / clip_speed


def parse_event_name(raw: str) -> tuple[str, bool]:
    """A timeline marker's name as (event name, always). A leading ``!`` marks
    an event that still fires when the cutscene is skipped."""
    name = raw.strip()
    always = name.startswith("!")
    if always:
        name = name[1:].strip()
    if not name:
        raise ValueError("a timeline marker in the cutscene has no name to use as its event")
    if name == "end":
        raise ValueError(
            "a marker is named 'end', which the engine reserves for the cutscene's "
            "own end — rename it, or move the cutscene's end frame instead"
        )
    return name, always


def _key_close(a: dict, b: dict, pos_eps: float, fov_eps: float) -> bool:
    for field in ("eye", "target"):
        if math.dist(a[field], b[field]) > pos_eps:
            return False
    if (a.get("fov") is None) != (b.get("fov") is None):
        return False
    return a.get("fov") is None or abs(a["fov"] - b["fov"]) <= fov_eps


def _key_lerp(a: dict, b: dict, at: float) -> dict:
    u = (at - a["at"]) / (b["at"] - a["at"])
    out = {
        field: [a[field][k] + (b[field][k] - a[field][k]) * u for k in range(3)]
        for field in ("eye", "target")
    }
    if a.get("fov") is not None and b.get("fov") is not None:
        out["fov"] = a["fov"] + (b["fov"] - a["fov"]) * u
    return out


def reduce_linear_keys(keys: list[dict], pos_eps: float = 0.005, fov_eps: float = 0.05) -> list[dict]:
    """Drop every camera key that straight-line interpolation between its
    neighbours already reproduces. A shot that never moves becomes one key."""
    if len(keys) < 2:
        return list(keys)
    kept = [0]
    anchor = 0
    while anchor < len(keys) - 1:
        reach = anchor + 1
        for candidate in range(anchor + 2, len(keys)):
            a, b = keys[anchor], keys[candidate]
            if all(
                _key_close(_key_lerp(a, b, keys[m]["at"]), keys[m], pos_eps, fov_eps)
                for m in range(anchor + 1, candidate)
            ):
                reach = candidate
            else:
                break
        kept.append(reach)
        anchor = reach
    out = [keys[i] for i in kept]
    if all(_key_close(out[0], key, pos_eps, fov_eps) for key in out[1:]):
        return out[:1]
    return out


def flatten_play_strips(strips: list[dict], masks=()) -> tuple[list[dict], bool]:
    """Turn layered strips into the engine's single line of play strips.

    Blender's NLA lets strips overlap and the top track wins; the engine plays
    one clip per actor, the latest one begun. So each stretch of time goes to
    the highest ``priority`` strip covering it, and a strip that resumes after
    an interruption restarts with its ``offset`` advanced to where it would
    have been. ``masks`` are (start, end) spans a move's own clip owns.

    Returns the segments in time order and whether any strips overlapped."""
    bounds = sorted(
        {s["start"] for s in strips} | {s["end"] for s in strips}
        | {edge for mask in masks for edge in mask}
    )
    out: list[dict] = []
    overlapped = False
    current: tuple[int, dict] | None = None
    for a, b in zip(bounds, bounds[1:]):
        if b - a <= 1e-9:
            continue
        mid = (a + b) / 2.0
        if any(m0 <= mid < m1 for m0, m1 in masks):
            current = None
            continue
        live = [i for i, s in enumerate(strips) if s["start"] <= mid < s["end"]]
        if not live:
            current = None
            continue
        if len(live) > 1:
            overlapped = True
        winner = max(live, key=lambda i: (strips[i]["priority"], strips[i]["start"]))
        if current is not None and current[0] == winner:
            current[1]["end"] = b
            continue
        source = strips[winner]
        segment = {key: value for key, value in source.items() if key != "priority"}
        segment["start"] = a
        segment["end"] = b
        if a > source["start"] + 1e-9:
            segment["offset"] = (
                source.get("offset", 0.0) + (a - source["start"]) * source.get("speed", 1.0)
            )
        current = (winner, segment)
        out.append(segment)
    return out, overlapped


# --- Reading Blender data ---

def curve_polyline(obj: "bpy.types.Object", warnings: list[str] | None = None) -> tuple[np.ndarray, bool]:
    """A curve object's first spline as an ordered world-space polyline, and
    whether it is closed.

    Bezier and poly splines are evaluated here rather than through to_mesh(),
    which turns into a tube under bevel and follows modifiers. NURBS has no
    simple evaluator, so it goes through the object's own mesh."""
    from mathutils import Vector
    from mathutils.geometry import interpolate_bezier

    if obj is None or obj.type != "CURVE":
        raise ValueError(f"'{getattr(obj, 'name', 'nothing')}' is not a curve to walk along")
    splines = obj.data.splines
    if not len(splines):
        raise ValueError(f"Path '{obj.name}' has no spline")
    if len(splines) > 1 and warnings is not None:
        warnings.append(f"Path '{obj.name}' has {len(splines)} splines; only the first is walked")

    spline = splines[0]
    closed = bool(spline.use_cyclic_u)
    if spline.type == "BEZIER":
        knots = list(spline.bezier_points)
        resolution = max(int(obj.data.resolution_u), 12)
        pairs = list(zip(knots, knots[1:]))
        if closed and len(knots) > 1:
            pairs.append((knots[-1], knots[0]))
        local = [knots[0].co.copy()] if knots else []
        for a, b in pairs:
            local.extend(
                interpolate_bezier(a.co, a.handle_right, b.handle_left, b.co, resolution + 1)[1:]
            )
    elif spline.type == "POLY":
        local = [point.co.xyz for point in spline.points]
        if closed and local:
            local.append(local[0])
    else:
        if len(splines) > 1:
            raise ValueError(f"NURBS path '{obj.name}' must be a single spline")
        mesh = obj.to_mesh()
        try:
            if mesh is None or len(mesh.polygons):
                raise ValueError(
                    f"NURBS path '{obj.name}' has bevel or extrusion; remove it or convert to bezier"
                )
            local = [vertex.co.copy() for vertex in mesh.vertices]
        finally:
            obj.to_mesh_clear()
        if closed and local:
            local.append(local[0])

    matrix = obj.matrix_world
    world = dedupe_points([tuple(matrix @ Vector(point)) for point in local])
    if len(world) < 2:
        raise ValueError(f"Path '{obj.name}' needs at least two distinct points")
    return world, closed


def move_kind(action: "bpy.types.Action | None") -> str:
    """'MOVE', 'HOME' or 'NONE': what the walk tool made this action for."""
    meta = getattr(action, "gltf_cutscene_move", None)
    return meta.kind if meta is not None else "NONE"


def covering_action(obj: "bpy.types.Object") -> "bpy.types.Action | None":
    """The object's active action when Blender plays it over the object's NLA
    strips, which the cutscene file never sees; None otherwise."""
    anim = obj.animation_data
    if anim is None or anim.use_tweak_mode or anim.action is None:
        return None
    if move_kind(anim.action) != "NONE" or anim.action_influence <= 0.0:
        return None
    if not any(not track.mute and len(track.strips) for track in anim.nla_tracks):
        return None
    return anim.action


def clip_speed(obj: "bpy.types.Object", action: "bpy.types.Action") -> float:
    """Ground speed (m/s) the object's clip list gives this action, or 0."""
    for clip in getattr(obj, "gltf_clips", ()):
        if clip.action == action:
            return clip.speed
    return 0.0


def is_library_clip(obj: "bpy.types.Object", action: "bpy.types.Action") -> bool:
    """True when the action is in the object's glTF clip list, so it ships in
    the character's own file and a cutscene only needs its name."""
    return any(clip.action == action for clip in getattr(obj, "gltf_clips", ()))


def find_actors(scene: "bpy.types.Scene") -> list["bpy.types.Object"]:
    """The scene's objects marked as cutscene actors, by name."""
    actors = [
        obj for obj in scene.objects
        if getattr(getattr(obj, "gltf_cutscene", None), "enabled", False)
    ]
    return sorted(actors, key=lambda obj: obj.name)


def strips_in_range(obj: "bpy.types.Object", frame_start: float, frame_end: float):
    """(track index, strip) for each live action strip touching the frames."""
    anim = obj.animation_data
    if anim is None:
        return
    for priority, track in enumerate(anim.nla_tracks):
        if track.mute or track.name == WALK_TRACK:
            continue
        for strip in track.strips:
            if strip.mute or strip.type != "CLIP" or strip.action is None:
                continue
            if strip.frame_end <= frame_start or strip.frame_start >= frame_end:
                continue
            yield priority, strip


def load_event_data(text: "bpy.types.Text | None") -> dict:
    """A cutscene's event payloads: a JSON object of event name to data. A
    marker name holds 63 bytes, far too little for a line of dialogue."""
    if text is None:
        return {}
    raw = text.as_string().strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"Event data text '{text.name}' is not valid JSON: {error}") from None
    if not isinstance(data, dict):
        raise ValueError(
            f"Event data text '{text.name}' must be a JSON object of event name to data"
        )
    return data


@dataclass
class CutsceneWindow:
    """A cutscene's place on the scene timeline."""

    name: str
    frame_start: int
    frame_end: int
    fps: float

    def time(self, frame: float) -> float:
        return (frame - self.frame_start) / self.fps

    @property
    def duration(self) -> float:
        return (self.frame_end - self.frame_start) / self.fps


def _round(value: float) -> float:
    # Adding zero turns the -0.0 an axis flip leaves behind into 0.0.
    return round(float(value), 4) + 0.0


def _point(vector) -> list[float]:
    """A Blender-space position as rounded glTF [x, y, z]."""
    return [_round(component) for component in convert_location(vector)]


class CutsceneExporter:
    """Gathers one cutscene into a glTF dict + binary."""

    def __init__(
        self,
        context: "bpy.types.Context",
        window: CutsceneWindow,
        *,
        camera_mode: str = "KEYS",
        event_data: dict | None = None,
        path_spacing: float = 0.25,
        default_fade: float = 0.2,
    ) -> None:
        self.context = context
        self.scene = context.scene
        self.window = window
        self.camera_mode = camera_mode
        self.event_data = event_data or {}
        self.path_spacing = path_spacing
        self.default_fade = default_fade
        self.warnings: list[str] = []

        self.buffer = BufferBuilder()
        self.nodes: list[Node] = []
        self._actor_nodes: dict[str, int] = {}
        self._bone_nodes: dict[tuple[str, str], int] = {}
        self._paths: list[dict] = []
        self._path_index: dict[str, int] = {}
        self._one_offs: dict[tuple[str, str, int | None], int | None] = {}
        self._one_off_object_trs: set[int] = set()
        self._animation_exporter: AnimationExporter | None = None
        self._rolled: set[str] = set()

    # --- Assembly ---

    def build(self, bin_uri: str | None = None) -> tuple[dict, bytes]:
        """Gather everything and return (gltf_dict, binary). ``bin_uri`` names
        the sibling .bin when the JSON is written as a separate .gltf."""
        window = self.window
        actors = [
            actor for actor in (self._gather_actor(obj) for obj in find_actors(self.scene))
            if actor is not None
        ]
        camera = self._gather_camera() if self.camera_mode != "NONE" else []
        events = self._gather_events()
        if not actors and not camera and not events:
            raise ValueError(
                f"Cutscene '{window.name}' has nothing in frames {window.frame_start}-"
                f"{window.frame_end}: no actor strips, no camera, no markers"
            )

        payload: dict = {
            "version": 1,
            "name": window.name,
            "duration": _round(window.duration),
            "fps": _round(window.fps),
        }
        if actors:
            payload["actors"] = actors
        if self._paths:
            payload["paths"] = self._paths
        if camera:
            payload["camera"] = camera
        if events:
            payload["events"] = events

        extensions_used = {EXT_CUTSCENE}
        animations = None
        if self._animation_exporter is not None:
            animations = self._animation_exporter.animations or None
            extensions_used |= self._animation_exporter.extensions_used

        accessors, buffer_views, buffer_desc, binary = self.buffer.finalize()
        if buffer_desc is not None and bin_uri:
            buffer_desc.uri = bin_uri

        gltf = Gltf(
            asset=Asset(generator="gltf-exporter", version="2.0"),
            scene=0,
            scenes=[Scene(name=window.name, nodes=list(self._actor_nodes.values()) or None)],
            nodes=self.nodes or None,
            animations=animations,
            accessors=accessors or None,
            buffer_views=buffer_views or None,
            buffers=[buffer_desc] if buffer_desc else None,
            extensions={EXT_CUTSCENE: payload},
            extensions_used=sorted(extensions_used),
        )
        return gltf.to_dict(), binary

    # --- Actors ---

    def _gather_actor(self, obj: "bpy.types.Object") -> dict | None:
        anim = obj.animation_data
        if anim is None:
            return None
        if anim.use_tweak_mode:
            raise ValueError(
                f"'{obj.name}' is in NLA tweak mode — leave it (Tab in the NLA editor) "
                f"before exporting"
            )

        window = self.window
        moves: list[dict] = []
        plays: list[dict] = []
        for priority, strip in strips_in_range(obj, window.frame_start, window.frame_end):
            kind = move_kind(strip.action)
            if kind == "HOME":
                continue
            if kind == "MOVE":
                moves.append(self._gather_move(obj, strip))
            else:
                play = self._gather_play(obj, strip, priority)
                if play is not None:
                    plays.append(play)
        if not moves and not plays:
            return None

        covering = covering_action(obj)
        if covering is not None:
            self.warnings.append(
                f"'{obj.name}' has '{covering.name}' as its active action, which Blender plays "
                f"over its strips; the file has only the strips. Stash it to preview what the "
                f"engine plays"
            )

        masks = [(move["start"], move["end"]) for move in moves if "clip" in move]
        segments, overlapped = flatten_play_strips(plays, masks)
        if overlapped:
            self.warnings.append(
                f"'{obj.name}' has overlapping NLA strips; the engine plays one clip at a "
                f"time, so the top track wins where they overlap"
            )
        if moves and any(
            segment.get("animation") in self._one_off_object_trs for segment in segments
        ):
            self.warnings.append(
                f"'{obj.name}' walks a path and also plays a one-off clip that keys the "
                f"object's own transform; the two will fight over its position"
            )

        strips = sorted(moves + segments, key=lambda strip: strip["start"])
        for strip in strips:
            strip["start"] = _round(strip["start"])
            strip["end"] = _round(strip["end"])
            if "offset" in strip:
                strip["offset"] = _round(strip["offset"])

        row = obj.gltf_cutscene.row.strip() or obj.name
        actor: dict = {"row": row}
        uid = obj.get("gltf_uid")
        if isinstance(uid, str) and uid:
            actor["uid"] = uid
        actor["node"] = self._actor_node(obj)
        actor["strips"] = strips
        return actor

    def _actor_node(self, obj: "bpy.types.Object") -> int:
        """The actor's stub node: a name for one-off animations to target and
        for the engine to bind to the live object."""
        index = self._actor_nodes.get(obj.name)
        if index is None:
            uid = obj.get("gltf_uid")
            index = len(self.nodes)
            self.nodes.append(Node(name=obj.name, uid=uid if isinstance(uid, str) and uid else None))
            self._actor_nodes[obj.name] = index
        return index

    def _gather_move(self, obj: "bpy.types.Object", strip: "bpy.types.NlaStrip") -> dict:
        meta = strip.action.gltf_cutscene_move
        curve = meta.path
        if curve is None or curve.type != "CURVE":
            raise ValueError(
                f"The move '{strip.name}' on '{obj.name}' has lost its path — delete the "
                f"strip or walk the path again"
            )
        if strip.use_reverse or abs(strip.repeat - 1.0) > 1e-6:
            raise ValueError(
                f"The move '{strip.name}' on '{obj.name}' is reversed or repeated; a move "
                f"runs its path once — stretch the strip to retime it, then Refresh Moves"
            )

        index, length = self._path(curve)
        start = self.window.time(strip.frame_start)
        end = self.window.time(strip.frame_end)
        move: dict = {
            "type": "move",
            "start": start,
            "end": end,
            "path": index,
            "ease": meta.ease.lower(),
            "turn": bool(meta.turn),
        }
        clip = meta.clip
        if clip is not None:
            move["clip"] = clip.name
            move["loop"] = True
            move["speed"] = _round(walk_rate(length, end - start, clip_speed(obj, clip)))
            move["fade"] = _round(self.default_fade)
            animation = self._one_off(obj, clip, slot_handle_for(clip, obj))
            if animation is not None:
                move["animation"] = animation
        return move

    def _path(self, curve: "bpy.types.Object") -> tuple[int, float]:
        """Index and length of the curve's entry in ``paths``, gathered once."""
        index = self._path_index.get(curve.name)
        if index is None:
            world, _closed = curve_polyline(curve, self.warnings)
            points, length = resample_polyline(world, self.path_spacing)
            index = len(self._paths)
            self._paths.append({
                "name": curve.name,
                "length": _round(length),
                "points": [_point(point) for point in points],
            })
            self._path_index[curve.name] = index
        return index, self._paths[index]["length"]

    def _gather_play(
        self, obj: "bpy.types.Object", strip: "bpy.types.NlaStrip", priority: int,
    ) -> dict | None:
        action = strip.action
        handle = getattr(strip, "action_slot_handle", None)
        if handle == 0 and len(getattr(action, "slots", ())):
            self.warnings.append(
                f"The strip '{strip.name}' on '{obj.name}' has no action slot, so it "
                f"animates nothing; skipped"
            )
            return None
        handle = handle or None

        for unsupported, label in (
            (strip.use_reverse, "is reversed"),
            (strip.blend_type != "REPLACE", f"blends as {strip.blend_type}"),
            (strip.use_animated_influence, "has animated influence"),
            (strip.use_animated_time, "has animated strip time"),
        ):
            if unsupported:
                self.warnings.append(
                    f"The strip '{strip.name}' on '{obj.name}' {label}, which the engine "
                    f"does not do; exported as a plain clip"
                )

        # The engine loops a whole clip; it has no notion of looping a part.
        full = getattr(action, "curve_frame_range", None) or action.frame_range
        if strip.repeat > 1.0 + 1e-6 and (
            strip.action_frame_start > full[0] + 1e-3 or strip.action_frame_end < full[1] - 1e-3
        ):
            self.warnings.append(
                f"The strip '{strip.name}' on '{obj.name}' repeats only part of its action "
                f"(frames {strip.action_frame_start:g}-{strip.action_frame_end:g}); the "
                f"engine loops the whole clip"
            )

        fps = self.window.fps
        play: dict = {
            "type": "play",
            "start": self.window.time(strip.frame_start),
            "end": self.window.time(strip.frame_end),
            "priority": priority,
            "clip": action.name,
            "loop": strip.repeat > 1.0 + 1e-6,
            "speed": _round(1.0 / strip.scale if strip.scale else 1.0),
            "fade": _round(strip.blend_in / fps if strip.blend_in > 0.0 else self.default_fade),
        }
        offset = (strip.action_frame_start - action_first_frame(action, handle)) / fps
        if abs(offset) > 1e-6:
            play["offset"] = offset
        animation = self._one_off(obj, action, handle)
        if animation is not None:
            play["animation"] = animation
        return play

    # --- One-off clips ---

    def _one_off(
        self, obj: "bpy.types.Object", action: "bpy.types.Action", handle: int | None,
    ) -> int | None:
        """Index of the action as an animation in this file, or None when the
        actor's own asset already has it (it is in the clip list)."""
        if is_library_clip(obj, action):
            return None
        key = (obj.name, action.name, handle)
        if key in self._one_offs:
            return self._one_offs[key]

        actor_index = self._actor_node(obj)
        if obj.type == "ARMATURE":
            for bone in action_bone_names(action, handle):
                if bone not in obj.data.bones or (obj.name, bone) in self._bone_nodes:
                    continue
                bone_index = len(self.nodes)
                self.nodes.append(Node(name=bone))
                self._bone_nodes[(obj.name, bone)] = bone_index
                actor_node = self.nodes[actor_index]
                actor_node.children = (actor_node.children or []) + [bone_index]

        exporter = self._animations()
        animation = exporter.gather_clip(obj, action, self.window.fps, handle)
        index: int | None = None
        if animation is None:
            self.warnings.append(
                f"'{action.name}' is not in the clip list of '{obj.name}' and has nothing "
                f"that animates it; exported by name only"
            )
        else:
            index = len(exporter.animations)
            exporter.animations.append(animation)
            if action_has_object_channels(action, handle):
                self._one_off_object_trs.add(index)
        self._one_offs[key] = index
        return index

    def _animations(self) -> AnimationExporter:
        """The shared animation exporter, pointed at this file's stub nodes."""
        if self._animation_exporter is None:
            from ..exporter import ExportSettings  # exporter.py imports this package

            self._animation_exporter = AnimationExporter(
                self.buffer, ExportSettings(), self._actor_nodes, {},
                bone_to_node_index=self._bone_nodes,
            )
        return self._animation_exporter

    # --- Camera ---

    def _gather_camera(self) -> list[dict]:
        """Camera keys per shot. A shot runs from the cutscene start, or a
        camera-bound marker, to the next one; its camera is whatever the scene
        shows there, which follows Blender's own marker rules."""
        scene = self.scene
        window = self.window
        cuts = sorted({window.frame_start} | {
            marker.frame for marker in scene.timeline_markers
            if marker.camera is not None and window.frame_start < marker.frame <= window.frame_end
        })

        keys: list[dict] = []
        saved = scene.frame_current
        try:
            for i, first in enumerate(cuts):
                last = cuts[i + 1] - 1 if i + 1 < len(cuts) else window.frame_end
                scene.frame_set(first)
                camera = scene.camera
                if camera is None or camera.type != "CAMERA":
                    continue
                if self.camera_mode == "BAKED":
                    frames = list(range(first, last + 1))
                else:
                    frames = self._camera_key_frames(camera, first, last)

                shot = []
                for frame in frames:
                    scene.frame_set(frame)
                    shot.append(self._camera_key(camera, frame))
                if self.camera_mode == "BAKED":
                    shot = reduce_linear_keys(shot)
                # A camera that has stopped needs no key to say so: the shot
                # holds its last key until the next cut or the end.
                while len(shot) > 1 and _key_close(shot[-1], shot[-2], 1e-4, 1e-3):
                    shot.pop()
                if self.camera_mode == "BAKED":
                    for key in shot[1:]:
                        key["ease"] = "linear"
                shot[0]["cut"] = True
                keys.extend(shot)
        finally:
            scene.frame_set(saved)
        return keys

    @staticmethod
    def _camera_key_frames(camera: "bpy.types.Object", first: int, last: int) -> list[int]:
        """The shot's ends plus the camera's own keyframes between them."""
        frames = {first, last}
        for owner in (camera, camera.data):
            anim = owner.animation_data
            if anim is None or anim.action is None:
                continue
            for fcurve in _get_fcurves(anim.action, anim):
                for point in fcurve.keyframe_points:
                    frame = int(round(point.co[0]))
                    if first < frame < last:
                        frames.add(frame)
        return sorted(frames)

    def _camera_key(self, camera: "bpy.types.Object", frame: int) -> dict:
        """The camera at the scene's current frame as eye / target / fov."""
        matrix = camera.matrix_world
        eye = matrix.translation
        forward = -matrix.col[2].xyz.normalized()
        # The engine's camera cannot roll: flag a camera whose side axis tilts.
        if abs(matrix.col[0].xyz.normalized().z) > 0.02 and camera.name not in self._rolled:
            self._rolled.add(camera.name)
            self.warnings.append(
                f"Camera '{camera.name}' is rolled; the engine's camera stays level, so the "
                f"roll is dropped"
            )

        target = eye + forward * self._focus_distance(camera, eye, forward)
        key: dict = {
            "at": _round(self.window.time(frame)),
            "eye": _point(eye),
            "target": _point(target),
        }
        if camera.data.type == "PERSP":
            key["fov"] = round(math.degrees(camera_yfov(camera.data, self.scene.render)[0]), 3)
        return key

    @staticmethod
    def _focus_distance(camera: "bpy.types.Object", eye, forward) -> float:
        """How far ahead the camera's point of interest is: its focus object
        or distance, else what it tracks, else a fixed guess. The engine turns
        eye + target into an orbit, so this only shapes moves between keys."""
        distance = None
        dof = camera.data.dof
        if dof.focus_object is not None:
            distance = (dof.focus_object.matrix_world.translation - eye).dot(forward)
        elif dof.use_dof:
            distance = dof.focus_distance
        else:
            for constraint in camera.constraints:
                target = getattr(constraint, "target", None)
                if (
                    constraint.type in {"TRACK_TO", "DAMPED_TRACK", "LOCKED_TRACK"}
                    and target is not None and not constraint.mute
                ):
                    distance = (target.matrix_world.translation - eye).length
                    break
        return distance if distance is not None and distance > 0.01 else DEFAULT_FOCUS

    # --- Events ---

    def _gather_events(self) -> list[dict]:
        """Timeline markers that are not camera cuts, as named events."""
        window = self.window
        markers = sorted(
            (
                marker for marker in self.scene.timeline_markers
                if marker.camera is None
                and window.frame_start <= marker.frame <= window.frame_end
            ),
            key=lambda marker: (marker.frame, marker.name),
        )
        events: list[dict] = []
        seen: dict[str, int] = {}
        for marker in markers:
            name, always = parse_event_name(marker.name)
            event: dict = {"at": _round(window.time(marker.frame)), "name": name}
            data = self._event_data(name, seen.get(name, 0))
            seen[name] = seen.get(name, 0) + 1
            if data:
                event["data"] = data
            if always:
                event["always"] = True
            events.append(event)
        return events

    def _event_data(self, name: str, occurrence: int) -> dict | None:
        """Payload for the nth event of this name: one object shared by all of
        them, or a list handing one object to each in time order."""
        value = self.event_data.get(name)
        if isinstance(value, list):
            value = value[occurrence] if occurrence < len(value) else None
        return value if isinstance(value, dict) else None


def cutscene_path(directory: str, name: str, export_format: str = "GLB") -> Path:
    """Where a cutscene of this name is written: ``<name>.cutscene.glb``."""
    import bpy

    if directory.startswith("//") and not bpy.data.filepath:
        raise ValueError(
            "The export folder is relative to the .blend, which is not saved yet — save "
            "the file or pick an absolute folder"
        )
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    suffix = ".glb" if export_format == "GLB" else ".gltf"
    return Path(bpy.path.abspath(directory)) / f"{safe}.cutscene{suffix}"


def export_cutscene(
    context: "bpy.types.Context",
    cutscene,
    directory: str,
    export_format: str = "GLB",
) -> tuple[str, list[str]]:
    """Write one cutscene (anything with name / frame_start / frame_end, and
    optionally camera_mode / event_data) into ``directory``. Returns the path
    written and the warnings to show the user."""
    scene = context.scene
    name = cutscene.name.strip()
    if not name:
        raise ValueError("A cutscene needs a name to export under")
    if cutscene.frame_end <= cutscene.frame_start:
        raise ValueError(f"Cutscene '{name}' ends at or before its first frame")

    window = CutsceneWindow(
        name=name,
        frame_start=int(cutscene.frame_start),
        frame_end=int(cutscene.frame_end),
        fps=scene.render.fps / scene.render.fps_base,
    )
    path = cutscene_path(directory, name, export_format)
    exporter = CutsceneExporter(
        context, window,
        camera_mode=getattr(cutscene, "camera_mode", "KEYS"),
        event_data=load_event_data(getattr(cutscene, "event_data", None)),
    )
    separate = path.suffix == ".gltf"
    gltf_dict, binary = exporter.build(bin_uri=path.with_suffix(".bin").name if separate else None)

    path.parent.mkdir(parents=True, exist_ok=True)
    if separate:
        write_gltf(path, gltf_dict, binary)
    else:
        write_glb(path, gltf_dict, binary)
    return str(path), exporter.warnings
