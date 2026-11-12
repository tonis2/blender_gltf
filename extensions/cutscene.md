# CUSTOM_cutscene

A root extension that makes a glTF file a **cutscene**: a timeline of what each actor
plays and where it walks, what the camera sees, and which events fire when. The file
carries no meshes. The engine loads it, plays it and frees it.

A cutscene is not a baked animation. Actors reuse the clips that already live in
their own `.glb` — the file only names them — and walk along paths that the engine
follows at run time. Only acting that exists for this one cutscene is stored here, as
ordinary glTF animations.

Export only. One file per cutscene, written as `<name>.cutscene.glb` (or `.gltf`).

```json
{
  "asset": {"version": "2.0", "generator": "gltf-exporter"},
  "extensionsUsed": ["CUSTOM_cutscene"],
  "scene": 0,
  "scenes": [{"name": "intro", "nodes": [0]}],
  "nodes": [
    {"name": "hero", "uid": "hero-ab12cd34", "children": [1]},
    {"name": "arm.R"}
  ],
  "animations": [{"name": "intro_trip", "channels": ["..."], "samplers": ["..."]}],
  "extensions": {"CUSTOM_cutscene": {
    "version": 1, "name": "intro", "duration": 10.0, "fps": 24.0,
    "actors": [{"row": "hero", "uid": "hero-ab12cd34", "node": 0, "strips": [
      {"type": "move", "start": 0.0, "end": 3.0, "path": 0, "ease": "linear", "turn": true,
       "clip": "walk", "loop": true, "speed": 1.08, "fade": 0.2},
      {"type": "play", "start": 3.0, "end": 3.5, "clip": "wave", "loop": false,
       "speed": 1.0, "fade": 0.2},
      {"type": "play", "start": 8.2917, "end": 8.625, "clip": "intro_trip", "animation": 0,
       "loop": false, "speed": 1.0, "fade": 0.2}
    ]}],
    "paths": [{"name": "Walk Path", "length": 9.72, "points": [[0, 0, 0], [0.249, 0, 0]]}],
    "camera": [
      {"at": 0.0, "eye": [0, 2, 10], "target": [0, 2, 5], "fov": 22.9, "cut": true},
      {"at": 2.0, "eye": [4, 2, 10], "target": [4, 2, 5], "fov": 22.9},
      {"at": 5.0, "eye": [10, 5, 0], "target": [0, 0, 0], "fov": 22.9, "cut": true}
    ],
    "events": [
      {"at": 0.0, "name": "music", "data": {"track": "tense"}, "always": true},
      {"at": 1.0, "name": "say", "data": {"text": "Not today."}}
    ]
  }}
}
```

## Conventions

- **Time** is in seconds from the cutscene's first frame. `duration` is its length and
  `fps` the frame rate it was authored at.
- **Space** is glTF scene space: Y up, metres, the same space the level was exported in.
- Keys that are absent take the defaults given below.

## Schema

### Extension object

| Property | Type | Description |
|----------|------|-------------|
| `version` | integer | `1` |
| `name` | string | The cutscene's name |
| `duration` | number | Length in seconds |
| `fps` | number | Authoring frame rate, for tools; not needed to play |
| `actors` | array of [Actor](#actor) | Objects the cutscene drives. Absent when it has none |
| `paths` | array of [Path](#path) | Walk paths, referenced by index from move strips |
| `camera` | array of [Camera key](#camera-key) | Sorted by `at`. Absent leaves the camera alone |
| `events` | array of [Event](#event) | Sorted by `at` |

### Actor

| Property | Type | Description |
|----------|------|-------------|
| `row` | string | Name the engine finds the live object by: the Blender object's name, or its *Engine Name* when set |
| `uid` | string | The object's glTF 2.1 draft `uid`, when it has been exported with one. A stabler handle than `row` |
| `node` | integer | The actor's stub node in this file. One-off animations target it and its children |
| `strips` | array of [Strip](#strip) | Sorted by `start` |

### Strip

Both kinds share:

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `type` | `"move"` \| `"play"` | | |
| `start`, `end` | number | | Seconds. `start` may be negative when a strip begins before the cutscene does |
| `clip` | string | | Name of an animation. In the actor's own asset unless `animation` is given |
| `animation` | integer | | Index of an animation **in this file**: `clip` is a one-off, see [One-off clips](#one-off-clips) |
| `loop` | boolean | | Repeat the clip. A clip that does not loop holds its last frame |
| `speed` | number | `1` | Playback rate |
| `fade` | number | | Seconds to cross-fade in from whatever was playing |
| `offset` | number | `0` | Clip time, in seconds, the strip starts at |

A `move` adds:

| Property | Type | Description |
|----------|------|-------------|
| `path` | integer | Index into `paths` |
| `ease` | `"linear"` \| `"in"` \| `"out"` \| `"inout"` | How distance along the path grows over `[start, end]`: `u`, `u²`, `1-(1-u)²`, `u²(3-2u)` |
| `turn` | boolean | Face along the path. `false` keeps the facing the actor had |

A move covers its whole path once between `start` and `end` and leaves the actor where
it ended. Its `clip` is optional; when present it loops for the length of the move and
`speed` is already set so the stride matches the ground covered.

Play strips never overlap each other or a move's clip: the exporter resolves Blender's
layered strips into a single line per actor.

### Path

| Property | Type | Description |
|----------|------|-------------|
| `name` | string | The curve object's name |
| `length` | number | Length in metres |
| `points` | array of `[x, y, z]` | The curve sampled at even spacing (0.25 m or a little less), first and last points exact |

Because the points are evenly spaced, distance `d` along the path is at index
`d / length * (count - 1)`.

### Camera key

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `at` | number | | Seconds |
| `eye` | `[x, y, z]` | | Camera position |
| `target` | `[x, y, z]` | | The point it looks at |
| `fov` | number | | Vertical field of view in **degrees**. Absent for an orthographic camera |
| `cut` | boolean | `false` | Jump to this key instead of moving to it. Set on the first key of every shot |
| `ease` | `"linear"` | `"inout"` | How the camera arrives at this key from the previous one |

Roll is not represented: the camera's up is always world up.

### Event

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `at` | number | | Seconds |
| `name` | string | | What happened; the engine decides what it means. Never `"end"`, which is the cutscene's own end |
| `data` | object | `{}` | Handed to the handler |
| `always` | boolean | `false` | Still fires when the cutscene is skipped |

## One-off clips

A strip whose action is in the actor's clip list is exported by name alone; the clip is
in the character's file. Any other action is written **into the cutscene file** as a
normal glTF animation, and the strip gets `animation` beside `clip`.

Those animations target stub nodes, which exist only to carry names:

- one node per actor, named after the Blender object, with its `uid` when it has one;
- under it, one child per bone a one-off animates, named after the bone.

Channel values are absolute local transforms, exactly as in the character's own clips,
so binding a one-off is a matter of mapping each stub node to the live node of the same
name under the actor. Bones the clip does not animate have no stub and keep their pose.

The file has no buffer at all unless it has one-off clips.

## Blender workflow

**Character setup (once per character).** Select the character (its armature or its
mesh) and, in the 3D viewport sidebar's *glTF* tab › *Clips*, list the actions that
belong in the character's `.glb`; *Add All Matching* fills the list with every action
that animates the rig. The ordinary export then writes each as
its own animation instead of only the active action. Give walk and run cycles a *Ground
Speed* (m/s at normal playback) so walks can match their stride to the ground.

**Cutscenes** live in the 3D viewport sidebar › *glTF* tab.

1. **Add a cutscene**: a name and a frame range of the scene timeline. *Preview This
   Cutscene* sets the preview range to it.
2. **Mark the actors**: select the objects that take part and press *Add Selected as
   Actors*. Only marked objects are exported; the panel points out unmarked objects
   that have strips in range.
3. **Lay out acting in the NLA editor**. Each strip becomes a play strip:

   | NLA strip | Exported as |
   |-----------|-------------|
   | Start and end | `start`, `end` |
   | Playback Scale | `speed` (its inverse) |
   | Repeat above 1 | `loop` |
   | Blend In | `fade` (0.2 s when zero) |
   | Action start frame | `offset` |

   Muted strips and tracks are skipped. Where strips overlap, the upper track wins, as
   it does in Blender.
4. **Draw walk paths**. *Draw Walk Path* puts you in Grease Pencil draw mode with
   strokes placed on surfaces; sketch on the ground, then *Finish Path*. Each stroke
   becomes a bezier curve that stays within *Simplify* of what you drew and is snapped
   to the ground. Any curve object works as a path, so curves made by hand are fine.
5. **Walk**. Select the character and a path, press *Walk Along Path*, and pick the
   clip, speed, ease and start frame. This adds a *move strip* on a **Cutscene Move**
   track: drag it to move the walk in time, stretch it to change its duration. After
   retiming a strip or editing a path, press *Refresh Moves*.
6. **Camera**. Bind cameras to timeline markers (`Ctrl+B`) to cut between them.
7. **Events**. Every other timeline marker in range is an event named after the marker.
   A leading `!` sets `always`. For payloads, point the cutscene's *Event Data* at a
   text datablock holding a JSON object of event name to data; a list gives one entry
   to each event of that name in time order:

   ```json
   {"music": {"track": "tense"}, "say": [{"text": "Not today."}, {"text": "Run."}]}
   ```
8. **Export Cutscene** (or *Export All*) writes into the chosen folder.

### How a walk previews

A move strip plays an action the tool generates: the actor's location and rotation at
every frame of the walk. That is only a preview — the file gets the path and the timing,
not those keys — but it makes Blender show what the engine will do.

- The move track starts with a one-frame **home** strip holding where the actor stood
  when the track was made. Without it Blender would show the actor at the world origin
  before its first walk. *Set Home From Current* rewrites it.
- The walk clip loops on a **Cutscene Walk Clips** track that the tool rebuilds on every
  refresh. Do not edit it; the move itself carries the clip. It has to stay the top track:
  a track added later hides the walk, since a track's first strip holds its first frame
  back to the start of the timeline. *Preview This Cutscene* and *Refresh Moves* put it
  back on top, and the panel says when it is buried.
- An actor's **active action** plays over all its strips in Blender, and the file never
  has it. The panel flags it; *Stash* moves it into a muted `[Action Stash]` track, as
  Blender's own Stash does.
- Move settings (path, ease, facing, clip) live on the generated action, because an NLA
  strip cannot hold properties.

### Camera modes

| Mode | Keys written |
|------|--------------|
| Keyframes | The start of each shot and the camera's own keyframes inside it. Motion between keys is the engine's ease, so constraints and parents are only right at the keys |
| Baked | Every frame is sampled, then keys a straight line already reproduces are dropped. Follows Track To, parents and curves exactly; keys carry `"ease": "linear"` |
| None | No `camera` at all |

`target` is placed at the camera's focus object or focus distance, else at what it
tracks, else 5 m ahead.

## Loading in three.c3

`three.cutscene.load` reads the file directly, and `dispose()` frees it:

```js
const hero = three.load('characters/hero.glb').instantiate('hero', { materials: true });
scene.add(hero);
const cs = three.cutscene.load('cutscenes/intro.cutscene.glb', { actors: { hero } });
cs.on('say', e => ui.say(e.text)).on('end', () => cs.dispose());
cs.play();
```

The loader turns the payload into the spec `three.cutscene.create` plays; the names are
already the same, and a move's `path` index becomes that path's points.

- **The character file** is exported with the armature at the origin: location and
  rotation cleared, scale kept. The cutscene places the instance root, so an armature that
  still stands where the level put it is drawn that far off the path.
- **Clip names** are the action names in Blender, so the character's `.glb` has to carry
  clips of the same names. Exporting it from the same actions (its *glTF Clips* list)
  guarantees that; a `.glb` from another tool needs its clips named to match.
- **Actors** are found by `row`: in `options.actors`, then in the level's rows, then by
  name in the scene. Pass what `instantiate()` gave you. `node()` on the armature leaves
  the skinned mesh out, and a by-name search of the scene can land on the armature node
  inside the instance, which has no clips.
- **One-off clips** are bound to the actor by stub node name and added to its asset for
  as long as the cutscene lives. A bone the file names and the actor lacks refuses the
  whole cutscene, naming the bone.
- A play strip stops at its `end` and holds the pose it ended on, as Blender shows it.
- `uid` is written for a future lookup; the engine matches actors by `row` today.

For any other engine: parse `CUSTOM_cutscene` from the root extensions without requiring
meshes, replace each move's `path` index by the points, and bind `animation` strips by
stub node name.

## Limits

- One clip per actor at a time. Overlapping NLA strips are flattened, with a warning.
- Reversed strips, non-Replace blending and animated influence or strip time are exported
  as plain clips, with a warning.
- A move runs its path once; it cannot be repeated or reversed.
- A repeated strip loops the whole clip. A strip that repeats only part of its action
  (a trimmed action range) is exported with a warning and loops everything.
- Every play strip holds at its end; the NLA's *Extrapolation: Nothing* is not exported.
- Path points are in scene space. An actor parented to a moving object needs them
  converted into its parent's space at load.
- Camera roll is dropped, with a warning.
- Marker names hold 63 bytes; longer event payloads belong in the Event Data text.
