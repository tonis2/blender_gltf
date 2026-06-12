# CUSTOM_animation_events

A custom glTF extension that exports **timed event markers** attached to an animation. Each event is a `(time, name)` pair — a named point on the animation timeline that the game engine can react to while the clip plays.

The extension carries **only the markers**. It does not animate anything itself; it is metadata that rides alongside the regular animation channels.

## Why this is useful

Animations are rarely just visual. A walk cycle needs to play a footstep sound when each foot lands; an attack needs to apply damage on the exact frame the blade connects; a spell-cast needs to spawn a particle effect when the hands come together. These are **gameplay sync points** — moments in the animation that other systems must fire on.

The usual alternative is to hardcode frame numbers in engine code (`if (frame == 14) playFootstep()`). That is brittle: the instant an animator re-times the clip, every hardcoded number is wrong, and the data lives in the wrong place (code, not the asset). `CUSTOM_animation_events` moves those sync points **into the animation asset itself**, authored by the animator in Blender, so they travel with the clip and survive re-timing.

Typical uses:

| Event name (example) | Engine reaction |
|----------------------|-----------------|
| `footstep_L`, `footstep_R` | Play a footstep sound / spawn a dust decal |
| `hit`, `damage_active_start` / `damage_active_end` | Open/close the attack's damage window |
| `cast` | Spawn a spell VFX, attach it to a bone |
| `sfx_swing` | Play a whoosh sound |
| `camera_shake` | Trigger a screen shake |
| `combo_window_open` | Allow the next attack input |
| `ik_off` / `ik_on` | Toggle foot IK during a turn |
| `footplant` | Lock the foot for root-motion correction |

Because events are just named strings, the engine decides what each one means — the extension imposes no fixed vocabulary.

## Extension placement

This is an **animation-level** extension. It lives on an entry in the top-level `animations` array, next to that animation's `channels` and `samplers`.

```json
{
  "animations": [
    {
      "name": "Attack_01",
      "channels": [ ... ],
      "samplers": [ ... ],
      "extensions": {
        "CUSTOM_animation_events": {
          "events": [
            { "time": 0.4583, "name": "sfx_swing" },
            { "time": 0.5833, "name": "hit" }
          ]
        }
      }
    }
  ],
  "extensionsUsed": ["CUSTOM_animation_events"]
}
```

## Schema

### Extension object

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `events` | array of [Event](#event) | Yes | The event markers for this animation, sorted ascending by `time` |

### Event

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `time` | number | Yes | When the event fires, in **seconds**, relative to the start of the animation. May be negative (see [Timing](#timing)) |
| `name` | string | Yes | Identifier for the event. The engine maps this to a behavior |

## Timing

`time` is measured in seconds from the animation's **t=0**, where t=0 is the same origin the animation channels are rebased to — the earliest keyframe of the clip. Convert to/from frames with the scene frame rate:

```
time_seconds = (marker_frame - first_keyframe_frame) / fps
```

A marker placed **before** the first keyframe produces a **negative** `time`. This is intentional and preserved on export — an engine that plays strictly within `[0, duration]` can simply ignore negative-time events, while an engine that supports a lead-in (e.g. anticipation cues) can honor them.

Events are always written **sorted ascending by `time`**, so an engine can advance through them with a single moving cursor as playback time increases.

## Full example

A one-second attack clip authored at 24 fps, with the first keyframe at frame 1:

```json
{
  "name": "Sword_Attack",
  "channels": [ ... ],
  "samplers": [ ... ],
  "extensions": {
    "CUSTOM_animation_events": {
      "events": [
        { "time": -0.0833, "name": "anticipation" },
        { "time": 0.2500,  "name": "sfx_swing" },
        { "time": 0.4167,  "name": "damage_active_start" },
        { "time": 0.4583,  "name": "hit" },
        { "time": 0.5417,  "name": "damage_active_end" },
        { "time": 0.7500,  "name": "combo_window_open" }
      ]
    }
  }
}
```

## Blender workflow

Events are authored as **pose markers on an Action** (not scene timeline markers):

1. Open the **Dope Sheet** and switch the mode to **Action Editor**.
2. Select the action you want to annotate.
3. Move the playhead to the frame where the event should fire.
4. **Marker > Add Marker** (`M`), then **Marker > Rename Marker** (`F2`) to give it a name like `footstep_L`.
5. Repeat for every sync point.

On export, every pose marker on an exported action becomes an event. The feature is controlled by the **"Export Animation Events"** toggle in the export panel (on by default).

On import, each event is recreated as a pose marker at `round(time * fps)` on the imported action(s). Re-exporting the imported file reproduces the same events, so the data round-trips. Markers that already exist with the same name and frame are not duplicated.

## Game engine implementation guide

### Minimal implementation

1. **Parse** the `CUSTOM_animation_events` extension when loading each animation. Store the sorted `events` list alongside the clip.
2. **Track playback time** for each playing animation instance (seconds since the clip started).
3. **Each frame**, fire every event whose `time` falls between the previous frame's playback time and the current one:
   ```
   for event in events:
       if prev_time < event.time <= current_time:
           dispatch(event.name)
   ```
   Because `events` is pre-sorted, keep a per-instance cursor index instead of scanning the whole list every frame.
4. **Dispatch** by mapping `event.name` to a handler (sound, VFX, gameplay logic). Unknown names should be ignored, not treated as errors.

### Looping animations

When a looping clip wraps from the end back to the start, reset the cursor and re-fire events from the top. Guard against double-firing an event that sits exactly on the loop boundary.

### Playback-rate and blending notes

- Event times are in **clip-local seconds**. If you play a clip at a non-1.0 speed, scale the comparison by the same factor (or scale playback time into clip-local time before comparing).
- When cross-fading between two clips, each clip keeps firing its own events for as long as it contributes to the blend. Some engines suppress events from a clip below a weight threshold — that is an engine policy choice, not part of this extension.
- If you scrub or seek backward, reset the cursor to avoid skipping or re-firing.

### Negative-time events

Decide on a policy:
- **Ignore** them if your playback clock starts at 0 (simplest, safe default).
- **Honor** them if you support a pre-roll / anticipation window before the visible animation begins.

## Interaction with other extensions

| Extension | How it interacts |
|-----------|-----------------|
| Core glTF `animations` | This extension is purely additive metadata on an animation. An importer that does not understand it loads the animation normally and skips the events |
| `KHR_audio_emitter` | A natural pairing: an event like `footstep_L` can trigger a positional sound source defined by the audio extension |
| `CUSTOM_particle_emitter` | Events such as `cast` or `impact` are convenient cues for spawning a one-shot particle burst |

## Notes for implementers

- The extension is **not** listed in `extensionsRequired` — files using it remain loadable by any conformant glTF importer, which will simply drop the events.
- Event `name` values are free-form. Establish a naming convention in your project (e.g. `sfx_*`, `vfx_*`, gameplay verbs) so the engine-side dispatch table stays predictable.
- There is no per-event payload beyond the name. If you need parameters (volume, which bone, a sound asset id), encode them in the name (`sfx_swing_heavy`) or maintain an engine-side lookup keyed by the name.
