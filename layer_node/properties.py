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
    IntProperty,
    StringProperty,
)
from bpy.types import PropertyGroup

from .constants import BLEND_MODES
from .utils import find_owner_node


def _on_layer_prop_changed(prop, context):
    """Rebuild internal blend chain when a layer property changes."""
    node = find_owner_node(prop)
    if node:
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
        update=lambda self, ctx: _on_layer_name_changed(self, ctx),
    )

    blend_mode: EnumProperty(
        name="Blend Mode",
        items=BLEND_MODES,
        default="MIX",
        description="How this layer blends with layers below",
        update=lambda self, ctx: _on_layer_prop_changed(self, ctx),
    )

    opacity: FloatProperty(
        name="Opacity",
        default=1.0,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        description="Layer opacity",
        update=lambda self, ctx: _on_layer_prop_changed(self, ctx),
    )

    enabled: BoolProperty(
        name="Enabled",
        default=True,
        description="Toggle this layer on/off",
        update=lambda self, ctx: _on_layer_prop_changed(self, ctx),
    )

    collapsed: BoolProperty(
        name="Collapsed",
        default=False,
        description="Collapse this layer's controls",
    )

    layer_index: IntProperty(name="Layer Index", default=0)
