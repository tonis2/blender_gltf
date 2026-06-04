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

# menu.py — Shader Editor Add menu integration

from bpy.types import Menu


class NODE_MT_stack_custom(Menu):
    bl_idname = "NODE_MT_stack_custom"
    bl_label = "Custom"

    def draw(self, context):
        self.layout.operator(
            "node.add_node", text="BSDF Stack",
        ).type = "BSDFStackNodeType"


def stack_menu_draw(self, context):
    if context.space_data.tree_type == 'ShaderNodeTree':
        self.layout.separator()
        self.layout.menu("NODE_MT_stack_custom", icon='NODE_COMPOSITING')
