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

# constants.py — Blend modes and defaults

BLEND_MODES = [
    ("MIX", "Normal", "Standard alpha-over blending"),
    ("MULTIPLY", "Multiply", "Multiply colors (darkens)"),
    ("ADD", "Add", "Additive blending (brightens)"),
    ("SUBTRACT", "Subtract", "Subtract top from bottom"),
    ("SCREEN", "Screen", "Screen blending (lightens)"),
    ("OVERLAY", "Overlay", "Overlay blending (contrast)"),
    ("SOFT_LIGHT", "Soft Light", "Soft light blending"),
    ("DIFFERENCE", "Difference", "Absolute difference"),
    ("DARKEN", "Darken", "Keep the darker value"),
    ("LIGHTEN", "Lighten", "Keep the lighter value"),
]

DEFAULT_COLOR = (1.0, 1.0, 1.0, 1.0)
DEFAULT_MASK = 1.0
