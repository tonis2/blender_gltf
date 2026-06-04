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

# utils.py — Shared helpers for finding nodes

import bpy


def find_node_by_group(group_name):
    """Find a StackNode by its internal node group name.
    Each Stack instance creates a unique group (e.g. '.stack_140234567'),
    so this is an unambiguous lookup even across multiple materials."""
    for mat in bpy.data.materials:
        if mat.node_tree:
            for node in mat.node_tree.nodes:
                if hasattr(node, "node_tree") and node.node_tree:
                    if node.node_tree.name == group_name:
                        return node
    for tree in bpy.data.node_groups:
        for node in tree.nodes:
            if hasattr(node, "node_tree") and node.node_tree:
                if node.node_tree.name == group_name:
                    return node
    return None


def get_node_id(node):
    """Get the unique identifier for a StackNode.
    Uses the internal node group name which is unique per instance."""
    if node.node_tree:
        return node.node_tree.name
    return ""


def find_owner_node(prop):
    """Find the StackNode that owns a layer property."""
    ptr = prop.as_pointer()
    for mat in bpy.data.materials:
        if mat.node_tree:
            for node in mat.node_tree.nodes:
                if hasattr(node, "layers") and hasattr(node, "rebuild_internals"):
                    for layer in node.layers:
                        if layer.as_pointer() == ptr:
                            return node
    for tree in bpy.data.node_groups:
        for node in tree.nodes:
            if hasattr(node, "layers") and hasattr(node, "rebuild_internals"):
                for layer in node.layers:
                    if layer.as_pointer() == ptr:
                        return node
    return None
