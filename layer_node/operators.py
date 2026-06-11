# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 NXSTYNATE
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

# operators.py — Layer manipulation operators

from bpy.props import EnumProperty, IntProperty, StringProperty
from bpy.types import Operator

from .utils import find_node_by_group


class STACK_OT_add_layer(Operator):
    """Add a new layer to the stack node"""
    bl_idname = "stack_node.add_layer"
    bl_label = "Add Layer"
    bl_options = {'REGISTER', 'UNDO'}

    group_name: StringProperty()

    def execute(self, context):
        node = find_node_by_group(self.group_name)
        if node is None:
            self.report({'ERROR'}, "Node not found")
            return {'CANCELLED'}

        layer = node.layers.add()
        idx = len(node.layers) - 1
        # blend_mode/opacity/enabled keep their property defaults. The name
        # is set first: its update callback only syncs panel headers (cheap,
        # no rebuild), and the single full rebuild happens below.
        layer.layer_name = f"Layer {idx}"

        # add_layer_to_group → rebuild_group → rebuild_internals.
        node.add_layer_to_group()
        return {'FINISHED'}


class STACK_OT_remove_layer(Operator):
    """Remove a layer from the stack node"""
    bl_idname = "stack_node.remove_layer"
    bl_label = "Remove Layer"
    bl_options = {'REGISTER', 'UNDO'}

    group_name: StringProperty()
    layer_index: IntProperty()

    def execute(self, context):
        node = find_node_by_group(self.group_name)
        if node is None:
            self.report({'ERROR'}, "Node not found")
            return {'CANCELLED'}

        if len(node.layers) <= 1:
            self.report({'WARNING'}, "Cannot remove the last layer")
            return {'CANCELLED'}

        removed = self.layer_index
        num = len(node.layers)

        old_to_new = {}
        for old_i in range(num):
            if old_i < removed:
                old_to_new[old_i] = old_i
            elif old_i == removed:
                old_to_new[old_i] = None
            else:
                old_to_new[old_i] = old_i - 1

        node.layers.remove(removed)

        node.rebuild_group(old_to_new=old_to_new)
        return {'FINISHED'}


class STACK_OT_move_layer(Operator):
    """Move a layer up or down in the stack"""
    bl_idname = "stack_node.move_layer"
    bl_label = "Move Layer"
    bl_options = {'REGISTER', 'UNDO'}

    group_name: StringProperty()
    layer_index: IntProperty()
    direction: EnumProperty(items=[("UP", "Up", ""), ("DOWN", "Down", "")])

    def execute(self, context):
        node = find_node_by_group(self.group_name)
        if node is None:
            self.report({'ERROR'}, "Node not found")
            return {'CANCELLED'}

        idx = self.layer_index
        num = len(node.layers)

        if self.direction == 'UP' and idx > 0:
            new_idx = idx - 1
        elif self.direction == 'DOWN' and idx < num - 1:
            new_idx = idx + 1
        else:
            return {'CANCELLED'}

        old_to_new = list(range(num))
        old_to_new[idx] = new_idx
        old_to_new[new_idx] = idx

        node.layers.move(idx, new_idx)

        node.rebuild_group(old_to_new=old_to_new)
        return {'FINISHED'}
