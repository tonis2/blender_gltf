from __future__ import annotations

import re
from typing import TYPE_CHECKING

import numpy as np

from .converter import convert_location_array, convert_rotation_array, convert_scale_array

if TYPE_CHECKING:
    import bpy
    from ..gltf.types import Gltf, Animation
    from .buffer_reader import BufferReader
    from .material import MaterialImporter
    from ..importer import ImportSettings


class AnimationImporter:
    def __init__(
        self,
        gltf: "Gltf",
        buffer_reader: "BufferReader",
        node_to_blender: dict[int, "bpy.types.Object"],
        material_importer: "MaterialImporter",
        settings: "ImportSettings",
        bone_node_to_armature: dict[int, tuple["bpy.types.Object", str]] | None = None,
    ) -> None:
        self.gltf = gltf
        self.buffer_reader = buffer_reader
        self.node_to_blender = node_to_blender
        self.material_importer = material_importer
        self.settings = settings
        self.bone_node_to_armature = bone_node_to_armature or {}

    def import_all(self, context: "bpy.types.Context") -> None:
        if self.gltf.animations is None:
            return
        fps = context.scene.render.fps / context.scene.render.fps_base

        for gltf_anim in self.gltf.animations:
            self._import_animation(gltf_anim, fps)

    def _import_animation(self, gltf_anim: "Animation", fps: float) -> None:
        for channel in gltf_anim.channels:
            sampler = gltf_anim.samplers[channel.sampler]
            times = self.buffer_reader.read_accessor(sampler.input).flatten()
            values = self.buffer_reader.read_accessor(sampler.output)
            interp = sampler.interpolation or "LINEAR"
            target = channel.target
            path = target.path

            if path == "pointer":
                self._import_pointer_animation(target, times, values, interp, fps, gltf_anim.name)
            elif path == "weights":
                self._import_weight_animation(target, times, values, interp, fps, gltf_anim.name)
            elif path in ("translation", "rotation", "scale"):
                self._import_trs_animation(target, path, times, values, interp, fps, gltf_anim.name)

    def _import_trs_animation(
        self, target, gltf_path: str, times, values, interp: str, fps: float, anim_name: str | None,
    ) -> None:
        import bpy

        node_index = target.node

        # Check if this is a bone animation
        if node_index in self.bone_node_to_armature:
            self._import_bone_trs_animation(
                node_index, gltf_path, times, values, interp, fps, anim_name,
            )
            return

        obj = self.node_to_blender.get(node_index)
        if obj is None:
            return

        if gltf_path == "translation":
            data_path = "location"
            num_components = 3
        elif gltf_path == "rotation":
            data_path = "rotation_quaternion"
            num_components = 4
            obj.rotation_mode = "QUATERNION"
        elif gltf_path == "scale":
            data_path = "scale"
            num_components = 3
        else:
            return

        action = self._ensure_action(obj, anim_name or "Action")

        if interp == "CUBICSPLINE":
            n_keyframes = len(times)
            values = values.reshape(n_keyframes * 3, num_components)
            values = self._convert_values(values, gltf_path)

            for c in range(num_components):
                fcurve = self._create_fcurve(action, data_path, c, obj)
                fcurve.keyframe_points.add(n_keyframes)
                for i in range(n_keyframes):
                    frame = times[i] * fps
                    in_tan = values[i * 3 + 0, c]
                    val = values[i * 3 + 1, c]
                    out_tan = values[i * 3 + 2, c]
                    kp = fcurve.keyframe_points[i]
                    kp.co = (frame, val)
                    kp.interpolation = "BEZIER"
                    kp.handle_left = (frame - 1, val + in_tan * fps)
                    kp.handle_right = (frame + 1, val + out_tan * fps)
                    kp.handle_left_type = "FREE"
                    kp.handle_right_type = "FREE"
                fcurve.update()
        else:
            values = values.reshape(-1, num_components)
            values = self._convert_values(values, gltf_path)
            blender_interp = "CONSTANT" if interp == "STEP" else "LINEAR"

            for c in range(num_components):
                fcurve = self._create_fcurve(action, data_path, c, obj)
                fcurve.keyframe_points.add(len(times))
                for i in range(len(times)):
                    frame = times[i] * fps
                    kp = fcurve.keyframe_points[i]
                    kp.co = (frame, values[i, c])
                    kp.interpolation = blender_interp
                fcurve.update()

    def _import_bone_trs_animation(
        self,
        node_index: int,
        gltf_path: str,
        times,
        values,
        interp: str,
        fps: float,
        anim_name: str | None,
    ) -> None:
        """Import TRS animation for a bone node, converting absolute TRS to pose deltas."""
        import bpy
        import mathutils

        armature_obj, bone_name = self.bone_node_to_armature[node_index]
        bone = armature_obj.data.bones.get(bone_name)
        if bone is None:
            return

        # Compute rest local matrix (bone relative to parent)
        if bone.parent:
            rest_local = bone.parent.matrix_local.inverted() @ bone.matrix_local
        else:
            rest_local = bone.matrix_local.copy()
        rest_local_inv = rest_local.inverted()

        # Decompose rest local for per-channel delta computation
        rest_loc, rest_rot, rest_scl = rest_local.decompose()

        if gltf_path == "translation":
            data_path = f'pose.bones["{bone_name}"].location'
            num_components = 3
        elif gltf_path == "rotation":
            data_path = f'pose.bones["{bone_name}"].rotation_quaternion'
            num_components = 4
        elif gltf_path == "scale":
            data_path = f'pose.bones["{bone_name}"].scale'
            num_components = 3
        else:
            return

        # Set rotation mode on pose bone
        pose_bone = armature_obj.pose.bones.get(bone_name)
        if pose_bone and gltf_path == "rotation":
            pose_bone.rotation_mode = "QUATERNION"

        action = self._ensure_action(armature_obj, anim_name or "Action")

        values = values.reshape(-1, num_components if gltf_path != "rotation" else 4)
        values = self._convert_values(values, gltf_path)
        blender_interp = "CONSTANT" if interp == "STEP" else "LINEAR"

        # Convert absolute TRS to pose bone deltas
        if gltf_path == "translation":
            # For each keyframe: compose absolute translation into a matrix,
            # compute delta = rest_inv @ absolute_matrix, extract translation
            delta_out = np.empty((len(times), 3), dtype=np.float32)
            for i in range(len(times)):
                abs_loc = mathutils.Vector(values[i])
                # Build absolute local matrix with rest rotation and scale
                abs_mat = (
                    mathutils.Matrix.Translation(abs_loc)
                    @ rest_rot.to_matrix().to_4x4()
                    @ mathutils.Matrix.Diagonal((*rest_scl, 1.0))
                )
                delta_mat = rest_local_inv @ abs_mat
                d_loc, _, _ = delta_mat.decompose()
                delta_out[i] = [d_loc.x, d_loc.y, d_loc.z]
            out_components = 3
            out_values = delta_out

        elif gltf_path == "rotation":
            # delta_rot = rest_rot.inverted() @ abs_rot
            rest_rot_inv = rest_rot.inverted()
            delta_out = np.empty((len(times), 4), dtype=np.float32)
            for i in range(len(times)):
                abs_rot_val = mathutils.Quaternion(values[i])
                # Build absolute local matrix with rest translation and scale
                abs_mat = (
                    mathutils.Matrix.Translation(rest_loc)
                    @ abs_rot_val.to_matrix().to_4x4()
                    @ mathutils.Matrix.Diagonal((*rest_scl, 1.0))
                )
                delta_mat = rest_local_inv @ abs_mat
                _, d_rot, _ = delta_mat.decompose()
                delta_out[i] = [d_rot.w, d_rot.x, d_rot.y, d_rot.z]
            out_components = 4
            out_values = delta_out

        elif gltf_path == "scale":
            # delta_scale = abs_scale / rest_scale (component-wise)
            delta_out = np.empty((len(times), 3), dtype=np.float32)
            for i in range(len(times)):
                abs_scl_val = mathutils.Vector(values[i])
                abs_mat = (
                    mathutils.Matrix.Translation(rest_loc)
                    @ rest_rot.to_matrix().to_4x4()
                    @ mathutils.Matrix.Diagonal((*abs_scl_val, 1.0))
                )
                delta_mat = rest_local_inv @ abs_mat
                _, _, d_scl = delta_mat.decompose()
                delta_out[i] = [d_scl.x, d_scl.y, d_scl.z]
            out_components = 3
            out_values = delta_out
        else:
            return

        for c in range(out_components):
            fcurve = self._create_fcurve(action, data_path, c, armature_obj)
            fcurve.keyframe_points.add(len(times))
            for i in range(len(times)):
                frame = times[i] * fps
                kp = fcurve.keyframe_points[i]
                kp.co = (frame, out_values[i, c])
                kp.interpolation = blender_interp
            fcurve.update()

    def _import_weight_animation(
        self, target, times, values, interp: str, fps: float, anim_name: str | None,
    ) -> None:
        import bpy

        node_index = target.node
        obj = self.node_to_blender.get(node_index)
        if obj is None or obj.data is None:
            return

        shape_keys = obj.data.shape_keys
        if shape_keys is None:
            return

        key_blocks = shape_keys.key_blocks
        num_targets = len(key_blocks) - 1
        if num_targets <= 0:
            return

        values = values.flatten()
        blender_interp = "CONSTANT" if interp == "STEP" else "LINEAR"

        if shape_keys.animation_data is None:
            shape_keys.animation_data_create()
        action = bpy.data.actions.new(name=anim_name or "ShapeKeyAction")
        shape_keys.animation_data.action = action

        for t in range(num_targets):
            kb = key_blocks[t + 1]
            data_path = f'key_blocks["{kb.name}"].value'
            fcurve = self._create_fcurve(action, data_path, 0, shape_keys)
            fcurve.keyframe_points.add(len(times))
            for i in range(len(times)):
                frame = times[i] * fps
                weight = values[i * num_targets + t]
                kp = fcurve.keyframe_points[i]
                kp.co = (frame, weight)
                kp.interpolation = blender_interp
            fcurve.update()

    def _import_pointer_animation(
        self, target, times, values, interp: str, fps: float, anim_name: str | None,
    ) -> None:
        import bpy

        if not target.extensions or "KHR_animation_pointer" not in target.extensions:
            return

        pointer = target.extensions["KHR_animation_pointer"].get("pointer", "")
        match = re.match(r"/materials/(\d+)/(.*)", pointer)
        if not match:
            return

        mat_index = int(match.group(1))
        prop_path = match.group(2)

        mat = self.material_importer.get_blender_material(mat_index)
        if mat is None or mat.node_tree is None:
            return

        principled = None
        for node in mat.node_tree.nodes:
            if node.type == "BSDF_PRINCIPLED":
                principled = node
                break
        if principled is None:
            return

        socket_info = self._pointer_to_socket(prop_path, principled)
        if socket_info is None:
            return

        socket_name, input_idx, num_components = socket_info

        if mat.node_tree.animation_data is None:
            mat.node_tree.animation_data_create()
        action = bpy.data.actions.new(name=anim_name or f"{mat.name}_anim")
        mat.node_tree.animation_data.action = action

        values = values.reshape(-1, num_components)
        blender_interp = "CONSTANT" if interp == "STEP" else "LINEAR"
        bp_data_path = f'nodes["{principled.name}"].inputs[{input_idx}].default_value'

        for c in range(num_components):
            fcurve = self._create_fcurve(action, bp_data_path, c, mat.node_tree)
            fcurve.keyframe_points.add(len(times))
            for i in range(len(times)):
                frame = times[i] * fps
                kp = fcurve.keyframe_points[i]
                kp.co = (frame, values[i, c])
                kp.interpolation = blender_interp
            fcurve.update()

    def _pointer_to_socket(self, prop_path: str, principled) -> tuple[str, int, int] | None:
        mapping = {
            "pbrMetallicRoughness/baseColorFactor": ("Base Color", 4),
            "pbrMetallicRoughness/metallicFactor": ("Metallic", 1),
            "pbrMetallicRoughness/roughnessFactor": ("Roughness", 1),
            "emissiveFactor": ("Emission Color", 3),
        }
        if prop_path not in mapping:
            return None
        socket_name, num_components = mapping[prop_path]
        for i, inp in enumerate(principled.inputs):
            if inp.name == socket_name:
                return socket_name, i, num_components
        return None

    def _convert_values(self, values: np.ndarray, gltf_path: str) -> np.ndarray:
        if gltf_path == "translation":
            return convert_location_array(values)
        elif gltf_path == "rotation":
            return convert_rotation_array(values)
        elif gltf_path == "scale":
            return convert_scale_array(values)
        return values

    def _ensure_action(self, obj, action_name: str):
        import bpy

        if obj.animation_data is None:
            obj.animation_data_create()
        if obj.animation_data.action and obj.animation_data.action.name == action_name:
            return obj.animation_data.action
        action = bpy.data.actions.new(name=action_name)
        obj.animation_data.action = action
        return action

    def _create_fcurve(self, action, data_path: str, index: int, id_data=None):
        """Create an fcurve, handling both legacy and Blender 5.x layered actions."""
        # Try Blender 5.x: fcurve_ensure_for_datablock
        if id_data is not None and hasattr(action, "fcurve_ensure_for_datablock"):
            try:
                fc = action.fcurve_ensure_for_datablock(
                    id_data, data_path, index=index,
                )
                return fc
            except Exception:
                pass

        # Legacy fallback
        if hasattr(action, "fcurves") and hasattr(action.fcurves, "new"):
            try:
                return action.fcurves.new(data_path, index=index)
            except Exception:
                pass

        raise RuntimeError(
            f"Cannot create fcurve for {data_path}[{index}] on {action.name}"
        )
