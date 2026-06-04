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

"""BSDF Stack layer node — the authoring node for CUSTOM_materials_layers.

Adapted from the standalone "Stack" addon (NXSTYNATE, GPL-3.0-or-later). A single
``BSDFStackNode`` holds an ordered list of layers, each exposing Principled-BSDF-style
inputs blended into one internal Principled BSDF. The glTF exporter reads its layers
into the CUSTOM_materials_layers extension; the importer rebuilds it.
"""

import bpy

# Reload submodules on addon re-enable (the parent package reload only reloads
# this package object, not the modules below).
if "properties" in locals():
    import importlib
    properties = importlib.reload(properties)
    utils = importlib.reload(utils)
    constants = importlib.reload(constants)
    operators = importlib.reload(operators)
    menu = importlib.reload(menu)
    bsdf_node = importlib.reload(bsdf_node)

from . import properties, utils, constants, operators, menu, bsdf_node
from .properties import StackLayerProperties
from .operators import (
    STACK_OT_add_layer,
    STACK_OT_remove_layer,
    STACK_OT_move_layer,
)
from .bsdf_node import BSDFStackNode
from .menu import NODE_MT_stack_custom, stack_menu_draw

# Order matters: StackLayerProperties must register before BSDFStackNode, which
# references it via CollectionProperty(type=StackLayerProperties).
classes = (
    StackLayerProperties,
    STACK_OT_add_layer,
    STACK_OT_remove_layer,
    STACK_OT_move_layer,
    BSDFStackNode,
    NODE_MT_stack_custom,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.NODE_MT_add.append(stack_menu_draw)


def unregister():
    bpy.types.NODE_MT_add.remove(stack_menu_draw)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
