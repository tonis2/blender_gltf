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

# properties.py — Layer property group

from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    StringProperty,
)
from bpy.types import PropertyGroup

from .constants import BLEND_MODES
from .utils import find_owner_layer, find_owner_node


def _on_layer_prop_changed(prop, context):
    """Rebuild internal blend chain when a layer property changes."""
    node = find_owner_node(prop)
    if node:
        node.rebuild_internals()


def _on_layer_factor_changed(prop, context):
    """Update factor nodes in place for opacity/enabled changes.

    These properties only affect the internal factor Math nodes (and the
    Color mix blend_type), so a full rebuild_internals() is avoided unless
    the targeted update fails."""
    node, layer_index = find_owner_layer(prop)
    if node is None:
        return
    if hasattr(node, "update_layer_factors") and node.update_layer_factors(layer_index):
        return
    node.rebuild_internals()


def _on_layer_name_changed(prop, context):
    """Sync per-layer panel headers (BSDFStackNode only) without rebuilding."""
    node = find_owner_node(prop)
    if node and hasattr(node, 'sync_panel_names'):
        node.sync_panel_names()


class StackLayerProperties(PropertyGroup):
    """Properties for a single stack layer."""

    layer_name: StringProperty(
        name="Layer Name",
        default="",
        description="Custom name for this layer",
        update=_on_layer_name_changed,
    )

    blend_mode: EnumProperty(
        name="Blend Mode",
        items=BLEND_MODES,
        default="MIX",
        description="How this layer blends with layers below",
        update=_on_layer_prop_changed,
    )

    opacity: FloatProperty(
        name="Opacity",
        default=1.0,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        description="Layer opacity",
        update=_on_layer_factor_changed,
    )

    enabled: BoolProperty(
        name="Enabled",
        default=True,
        description="Toggle this layer on/off",
        update=_on_layer_factor_changed,
    )
