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


def _iter_candidate_nodes():
    """Yield every node of every material node tree and node group."""
    for mat in bpy.data.materials:
        if mat.node_tree:
            yield from mat.node_tree.nodes
    for tree in bpy.data.node_groups:
        yield from tree.nodes


def find_node_by_group(group_name):
    """Find a StackNode by its internal node group name.
    Each Stack instance creates a unique group (e.g. '.stack_140234567'),
    so this is an unambiguous lookup even across multiple materials."""
    for node in _iter_candidate_nodes():
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


def find_owner_layer(prop):
    """Find the StackNode and layer index owning a layer property.

    Returns (node, layer_index), or (None, None) if not found."""
    ptr = prop.as_pointer()
    for node in _iter_candidate_nodes():
        if hasattr(node, "layers") and hasattr(node, "rebuild_internals"):
            for i, layer in enumerate(node.layers):
                if layer.as_pointer() == ptr:
                    return node, i
    return None, None


def find_owner_node(prop):
    """Find the StackNode that owns a layer property."""
    node, _ = find_owner_layer(prop)
    return node
