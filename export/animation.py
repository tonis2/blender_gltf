from __future__ import annotations

import re
from collections import defaultdict
from typing import TYPE_CHECKING

import numpy as np

from ..gltf.buffer import BufferBuilder
from ..gltf.constants import ComponentType, DataType
from ..gltf.types import (
    Animation,
    AnimationChannel,
    AnimationChannelTarget,
    AnimationSampler,
)
from .converter import convert_location_array, convert_rotation_array, convert_scale_array

if TYPE_CHECKING:
    import bpy
    from ..exporter import ExportSettings


# Blender fcurve data_path -> (glTF channel path, DataType, converter)
_TRS_PATH_MAP: dict[str, tuple[str, DataType]] = {
    "location": ("translation", DataType.VEC3),
    "rotation_quaternion": ("rotation", DataType.VEC4),
    "rotation_euler": ("rotation", DataType.VEC4),
    "scale": ("scale", DataType.VEC3),
}

# Number of components per Blender data_path
_PATH_COMPONENTS: dict[str, int] = {
    "location": 3,
    "rotation_quaternion": 4,
    "rotation_euler": 3,
    "scale": 3,
}

# Blender interpolation -> glTF interpolation
_INTERPOLATION_MAP: dict[str, str] = {
    "CONSTANT": "STEP",
    "LINEAR": "LINEAR",
    "BEZIER": "CUBICSPLINE",
}

# Principled BSDF socket name -> (glTF JSON pointer suffix, DataType, num_components)
_MATERIAL_SOCKET_MAP: dict[str, tuple[str, DataType, int]] = {
    "Base Color": ("pbrMetallicRoughness/baseColorFactor", DataType.VEC4, 4),
    "Metallic": ("pbrMetallicRoughness/metallicFactor", DataType.SCALAR, 1),
    "Roughness": ("pbrMetallicRoughness/roughnessFactor", DataType.SCALAR, 1),
    "Emission Color": ("emissiveFactor", DataType.VEC3, 3),
    "Alpha": ("pbrMetallicRoughness/baseColorFactor", DataType.VEC4, 4),
}

# Regex to parse material node tree fcurve data_paths
# e.g. nodes["Principled BSDF"].inputs[0].default_value
_MAT_FCURVE_RE = re.compile(
    r'nodes\["([^"]+)"\]\.inputs\[(\d+)\]\.default_value'
)


def _get_fcurves(
    action: "bpy.types.Action",
    anim_data: "bpy.types.AnimData | None" = None,
) -> list["bpy.types.FCurve"]:
    """Extract fcurves from an action, supporting both legacy and layered (Blender 5+) actions."""
    # Legacy actions (Blender < 4.4): action.fcurves exists directly
    if hasattr(action, "fcurves") and not callable(getattr(action, "fcurves", None)):
        try:
            return list(action.fcurves)
        except TypeError:
            pass

    # Layered actions (Blender 5+): fcurves in channelbags
    if hasattr(action, "layers"):
        # If we have animation_data, use its slot_handle to find the right channelbag
        slot_handle = None
        if anim_data and hasattr(anim_data, "action_slot_handle"):
            slot_handle = anim_data.action_slot_handle

        fcurves: list["bpy.types.FCurve"] = []
        for layer in action.layers:
            if not hasattr(layer, "strips"):
                continue
            for strip in layer.strips:
                if not hasattr(strip, "channelbags"):
                    continue
                for cb in strip.channelbags:
                    # If we have a slot_handle, only use matching channelbag
                    if slot_handle is not None and cb.slot_handle != slot_handle:
                        continue
                    fcurves.extend(cb.fcurves)
        return fcurves

    return []


class AnimationExporter:
    def __init__(
        self,
        buffer: BufferBuilder,
        settings: "ExportSettings",
        object_to_node_index: dict[str, int],
        material_to_index: dict[str, int],
    ) -> None:
        self.buffer = buffer
        self.settings = settings
        self.object_to_node_index = object_to_node_index
        self.material_to_index = material_to_index
        self.animations: list[Animation] = []
        self.extensions_used: set[str] = set()

    def gather(self, context: "bpy.types.Context") -> None:
        """Gather all animations from the scene."""
        scene = context.scene
        fps = scene.render.fps / scene.render.fps_base

        # --- Object TRS + weight animations ---
        # Group (object, action) pairs by action name
        action_objects: dict[str, list[tuple["bpy.types.Object", "bpy.types.Action"]]] = defaultdict(list)

        for obj in scene.objects:
            if obj.animation_data is None or obj.animation_data.action is None:
                continue
            if obj.name not in self.object_to_node_index:
                continue
            action = obj.animation_data.action
            action_objects[action.name].append((obj, action))

        for action_name, pairs in action_objects.items():
            anim = self._gather_action(action_name, pairs, fps)
            if anim is not None:
                self.animations.append(anim)

        # --- Shape key weight animations (on shape_keys.animation_data) ---
        if self.settings.export_morph_targets:
            self._gather_shape_key_animations(scene, fps)

        # --- Material animations (KHR_animation_pointer) ---
        if self.settings.export_materials:
            self._gather_material_animations(fps)

    def _gather_action(
        self,
        action_name: str,
        obj_action_pairs: list[tuple["bpy.types.Object", "bpy.types.Action"]],
        fps: float,
    ) -> Animation | None:
        """Gather one glTF Animation from a Blender Action (may span multiple objects)."""
        channels: list[AnimationChannel] = []
        samplers: list[AnimationSampler] = []

        for obj, action in obj_action_pairs:
            node_index = self.object_to_node_index[obj.name]

            # Group fcurves by data_path
            fcurve_groups: dict[str, dict[int, "bpy.types.FCurve"]] = defaultdict(dict)
            for fcurve in _get_fcurves(action, obj.animation_data):
                fcurve_groups[fcurve.data_path][fcurve.array_index] = fcurve

            # TRS channels
            for data_path in ("location", "rotation_quaternion", "rotation_euler", "scale"):
                if data_path not in fcurve_groups:
                    continue
                # Skip rotation_euler if quaternion curves also exist
                if data_path == "rotation_euler" and "rotation_quaternion" in fcurve_groups:
                    continue
                result = self._gather_trs_channel(
                    obj, node_index, fcurve_groups[data_path], data_path, fps,
                )
                if result is not None:
                    sampler, channel = result
                    sampler_idx = len(samplers)
                    samplers.append(sampler)
                    channel.sampler = sampler_idx
                    channels.append(channel)

        if not channels:
            return None

        return Animation(name=action_name, channels=channels, samplers=samplers)

    def _gather_trs_channel(
        self,
        obj: "bpy.types.Object",
        node_index: int,
        fcurves: dict[int, "bpy.types.FCurve"],
        data_path: str,
        fps: float,
    ) -> tuple[AnimationSampler, AnimationChannel] | None:
        """Build a sampler + channel for one TRS property."""
        gltf_path, data_type = _TRS_PATH_MAP[data_path]
        num_components = _PATH_COMPONENTS[data_path]

        # Collect all unique keyframe frame numbers
        frames: set[float] = set()
        for fcurve in fcurves.values():
            for kp in fcurve.keyframe_points:
                frames.add(kp.co[0])

        if not frames:
            return None

        sorted_frames = sorted(frames)
        times = np.array([f / fps for f in sorted_frames], dtype=np.float32)

        # Determine interpolation from first keyframe of first fcurve
        first_fcurve = next(iter(fcurves.values()))
        blender_interp = first_fcurve.keyframe_points[0].interpolation if first_fcurve.keyframe_points else "LINEAR"
        gltf_interp = _INTERPOLATION_MAP.get(blender_interp, "LINEAR")

        # Get rest values for missing components
        rest_values = self._get_rest_values(obj, data_path, num_components)

        if gltf_interp == "CUBICSPLINE":
            values = self._evaluate_cubicspline(
                fcurves, sorted_frames, num_components, rest_values, fps,
            )
        else:
            # Evaluate at each keyframe
            values = np.empty((len(sorted_frames), num_components), dtype=np.float32)
            for i, frame in enumerate(sorted_frames):
                for c in range(num_components):
                    if c in fcurves:
                        values[i, c] = fcurves[c].evaluate(frame)
                    else:
                        values[i, c] = rest_values[c]

        # Handle euler -> quaternion conversion
        if data_path == "rotation_euler":
            import mathutils
            rotation_mode = obj.rotation_mode
            quats = np.empty((len(values), 4), dtype=np.float32)
            if gltf_interp == "CUBICSPLINE":
                # For cubicspline, convert each triple (in_tangent, value, out_tangent)
                for i in range(len(values)):
                    euler = mathutils.Euler(values[i], rotation_mode)
                    q = euler.to_quaternion()
                    quats[i] = [q.w, q.x, q.y, q.z]
            else:
                for i in range(len(values)):
                    euler = mathutils.Euler(values[i], rotation_mode)
                    q = euler.to_quaternion()
                    quats[i] = [q.w, q.x, q.y, q.z]
            values = quats

        # Apply coordinate conversion
        if gltf_interp == "CUBICSPLINE":
            # For cubicspline, values are interleaved: [in_tangent, value, out_tangent] x N
            # Reshape to (N*3, components), convert, reshape back
            n_keyframes = len(sorted_frames)
            flat = values.reshape(n_keyframes * 3, -1)
            flat = self._convert_values(flat, gltf_path)
            values = flat.reshape(n_keyframes * 3, -1)
        else:
            values = self._convert_values(values, gltf_path)

        # Write accessors
        input_acc = self.buffer.add_accessor(
            times, ComponentType.FLOAT, DataType.SCALAR, include_bounds=True,
        )
        output_acc = self.buffer.add_accessor(
            values, ComponentType.FLOAT, data_type,
        )

        sampler = AnimationSampler(
            input=input_acc,
            output=output_acc,
            interpolation=gltf_interp if gltf_interp != "LINEAR" else None,
        )
        channel = AnimationChannel(
            target=AnimationChannelTarget(node=node_index, path=gltf_path),
        )
        return sampler, channel

    def _evaluate_cubicspline(
        self,
        fcurves: dict[int, "bpy.types.FCurve"],
        sorted_frames: list[float],
        num_components: int,
        rest_values: list[float],
        fps: float,
    ) -> np.ndarray:
        """Evaluate cubicspline keyframes: returns (N*3, components) interleaved
        [in_tangent_0, value_0, out_tangent_0, in_tangent_1, value_1, ...]."""
        n = len(sorted_frames)
        values = np.empty((n * 3, num_components), dtype=np.float32)

        for i, frame in enumerate(sorted_frames):
            for c in range(num_components):
                if c in fcurves:
                    fc = fcurves[c]
                    # Find the keyframe point at this frame
                    kp = self._find_keyframe_at(fc, frame)
                    if kp is not None:
                        # Convert tangent handles from frame-space to time-space
                        in_tangent = kp.handle_left[1] / fps
                        out_tangent = kp.handle_right[1] / fps
                        values[i * 3 + 0, c] = in_tangent
                        values[i * 3 + 1, c] = kp.co[1]
                        values[i * 3 + 2, c] = out_tangent
                    else:
                        val = fc.evaluate(frame)
                        values[i * 3 + 0, c] = 0.0
                        values[i * 3 + 1, c] = val
                        values[i * 3 + 2, c] = 0.0
                else:
                    values[i * 3 + 0, c] = 0.0
                    values[i * 3 + 1, c] = rest_values[c]
                    values[i * 3 + 2, c] = 0.0

        return values

    def _find_keyframe_at(
        self, fcurve: "bpy.types.FCurve", frame: float
    ) -> "bpy.types.Keyframe | None":
        for kp in fcurve.keyframe_points:
            if abs(kp.co[0] - frame) < 0.001:
                return kp
        return None

    def _get_rest_values(
        self, obj: "bpy.types.Object", data_path: str, num_components: int
    ) -> list[float]:
        """Get the object's rest/default values for a given property."""
        if data_path == "location":
            return list(obj.location)
        elif data_path == "rotation_quaternion":
            return list(obj.rotation_quaternion)
        elif data_path == "rotation_euler":
            return list(obj.rotation_euler)
        elif data_path == "scale":
            return list(obj.scale)
        return [0.0] * num_components

    def _convert_values(self, values: np.ndarray, gltf_path: str) -> np.ndarray:
        """Apply coordinate system conversion based on glTF path."""
        if gltf_path == "translation":
            return convert_location_array(values)
        elif gltf_path == "rotation":
            return convert_rotation_array(values)
        elif gltf_path == "scale":
            return convert_scale_array(values)
        return values

    # --- Shape key weight animation ---

    def _gather_shape_key_animations(
        self, scene: "bpy.types.Scene", fps: float,
    ) -> None:
        """Gather shape key weight animations from obj.data.shape_keys.animation_data."""
        for obj in scene.objects:
            if obj.type != "MESH" or obj.name not in self.object_to_node_index:
                continue
            if not obj.data or not obj.data.shape_keys:
                continue

            shape_keys = obj.data.shape_keys
            if shape_keys.animation_data is None or shape_keys.animation_data.action is None:
                continue
            if len(shape_keys.key_blocks) < 2:
                continue

            node_index = self.object_to_node_index[obj.name]
            action = shape_keys.animation_data.action
            result = self._gather_weight_animation(
                obj, node_index, shape_keys, action, fps,
            )
            if result is not None:
                self.animations.append(result)

    def _gather_weight_animation(
        self,
        obj: "bpy.types.Object",
        node_index: int,
        shape_keys: "bpy.types.Key",
        action: "bpy.types.Action",
        fps: float,
    ) -> Animation | None:
        """Gather morph target weight animation for an object."""
        target_names = [kb.name for kb in shape_keys.key_blocks[1:]]
        num_targets = len(target_names)

        # Group fcurves: data_path = 'key_blocks["KeyName"].value'
        weight_fcurves: dict[int, "bpy.types.FCurve"] = {}
        all_frames: set[float] = set()

        for fcurve in _get_fcurves(action, shape_keys.animation_data):
            match = re.match(r'key_blocks\["([^"]+)"\]\.value', fcurve.data_path)
            if match:
                key_name = match.group(1)
                if key_name in target_names:
                    target_idx = target_names.index(key_name)
                    weight_fcurves[target_idx] = fcurve
                    for kp in fcurve.keyframe_points:
                        all_frames.add(kp.co[0])

        if not weight_fcurves or not all_frames:
            return None

        sorted_frames = sorted(all_frames)
        times = np.array([f / fps for f in sorted_frames], dtype=np.float32)

        # Evaluate all weights at each keyframe, interleaved
        # glTF format: [w0_t0, w1_t0, ..., w0_t1, w1_t1, ...]
        values = np.empty(len(sorted_frames) * num_targets, dtype=np.float32)
        for i, frame in enumerate(sorted_frames):
            for t in range(num_targets):
                if t in weight_fcurves:
                    values[i * num_targets + t] = weight_fcurves[t].evaluate(frame)
                else:
                    values[i * num_targets + t] = shape_keys.key_blocks[t + 1].value

        input_acc = self.buffer.add_accessor(
            times, ComponentType.FLOAT, DataType.SCALAR, include_bounds=True,
        )
        output_acc = self.buffer.add_accessor(
            values, ComponentType.FLOAT, DataType.SCALAR,
        )

        sampler = AnimationSampler(input=input_acc, output=output_acc)
        channel = AnimationChannel(
            target=AnimationChannelTarget(node=node_index, path="weights"),
        )
        return Animation(
            name=action.name,
            channels=[channel],
            samplers=[sampler],
        )

    # --- Material animation (KHR_animation_pointer) ---

    def _gather_material_animations(self, fps: float) -> None:
        """Gather material property animations as KHR_animation_pointer channels."""
        import bpy

        for mat in bpy.data.materials:
            if mat.animation_data is None or mat.animation_data.action is None:
                continue
            if mat.name not in self.material_to_index:
                continue

            mat_index = self.material_to_index[mat.name]
            action = mat.animation_data.action

            channels, samplers = self._gather_material_action(mat, mat_index, action, fps)
            if channels:
                anim = Animation(
                    name=f"{mat.name}_material",
                    channels=channels,
                    samplers=samplers,
                )
                self.animations.append(anim)

    def _gather_material_action(
        self,
        mat: "bpy.types.Material",
        mat_index: int,
        action: "bpy.types.Action",
        fps: float,
    ) -> tuple[list[AnimationChannel], list[AnimationSampler]]:
        """Extract animation channels from a material action."""
        channels: list[AnimationChannel] = []
        samplers: list[AnimationSampler] = []

        if mat.node_tree is None:
            return channels, samplers

        # Group fcurves by (node_name, input_index)
        grouped: dict[tuple[str, int], dict[int, "bpy.types.FCurve"]] = defaultdict(dict)
        for fcurve in _get_fcurves(action, mat.animation_data):
            match = _MAT_FCURVE_RE.match(fcurve.data_path)
            if match:
                node_name = match.group(1)
                input_idx = int(match.group(2))
                grouped[(node_name, input_idx)][fcurve.array_index] = fcurve

        for (node_name, input_idx), fc_dict in grouped.items():
            # Look up the socket name
            node = mat.node_tree.nodes.get(node_name)
            if node is None or node.type != "BSDF_PRINCIPLED":
                continue
            if input_idx >= len(node.inputs):
                continue

            socket_name = node.inputs[input_idx].name
            if socket_name not in _MATERIAL_SOCKET_MAP:
                continue

            pointer_suffix, data_type, num_components = _MATERIAL_SOCKET_MAP[socket_name]
            pointer = f"/materials/{mat_index}/{pointer_suffix}"

            # Collect keyframe times
            all_frames: set[float] = set()
            for fc in fc_dict.values():
                for kp in fc.keyframe_points:
                    all_frames.add(kp.co[0])

            if not all_frames:
                continue

            sorted_frames = sorted(all_frames)
            times = np.array([f / fps for f in sorted_frames], dtype=np.float32)

            # Evaluate values
            # Get default value for missing components
            default_val = node.inputs[input_idx].default_value
            if hasattr(default_val, "__len__"):
                defaults = list(default_val)[:num_components]
            else:
                defaults = [float(default_val)] * num_components

            values = np.empty((len(sorted_frames), num_components), dtype=np.float32)
            for i, frame in enumerate(sorted_frames):
                for c in range(num_components):
                    if c in fc_dict:
                        values[i, c] = fc_dict[c].evaluate(frame)
                    else:
                        values[i, c] = defaults[c]

            input_acc = self.buffer.add_accessor(
                times, ComponentType.FLOAT, DataType.SCALAR, include_bounds=True,
            )
            output_acc = self.buffer.add_accessor(
                values, ComponentType.FLOAT, data_type,
            )

            sampler_idx = len(samplers)
            samplers.append(AnimationSampler(input=input_acc, output=output_acc))
            channels.append(AnimationChannel(
                sampler=sampler_idx,
                target=AnimationChannelTarget(
                    path="pointer",
                    extensions={
                        "KHR_animation_pointer": {"pointer": pointer},
                    },
                ),
            ))
            self.extensions_used.add("KHR_animation_pointer")

        return channels, samplers
