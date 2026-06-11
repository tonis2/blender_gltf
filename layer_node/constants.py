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

# constants.py — Blend modes and the per-layer channel table
#
# This module is intentionally bpy-free: the glTF export/import material
# modules import the channel table from here without a Blender runtime.

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

# (name, socket_type, default, bsdf_input_name, mix_data_type, hide_value)
# - bsdf_input_name=None: channel is control-only (not piped into BSDF)
# - mix_data_type=None:   channel is not blended (Mask)
# - hide_value=True:      socket exposes only a connection dot (no inline editor)
#
# Order matters: this defines node.inputs ordering per layer (sockets are
# laid out depth-first across the layer panel + its sub-panels, see
# SUBSECTIONS below).
CHANNELS = [
    ("Color",             "NodeSocketColor",  (1.0, 1.0, 1.0, 1.0), "Base Color",        "RGBA",   False),
    ("Mask",              "NodeSocketFloat",  1.0,                  None,                None,     False),
    ("Normal",            "NodeSocketVector", (0.0, 0.0, 1.0),      "Normal",            "VECTOR", True),
    ("Roughness",         "NodeSocketFloat",  0.5,                  "Roughness",         "FLOAT",  False),
    ("Metallic",          "NodeSocketFloat",  0.0,                  "Metallic",          "FLOAT",  False),
    ("Alpha",             "NodeSocketFloat",  1.0,                  "Alpha",             "FLOAT",  False),
    ("Emission Color",    "NodeSocketColor",  (0.0, 0.0, 0.0, 1.0), "Emission Color",    "RGBA",   False),
    ("Emission Strength", "NodeSocketFloat",  0.0,                  "Emission Strength", "FLOAT",  False),
    ("Subsurface Weight", "NodeSocketFloat",  0.0,                  "Subsurface Weight", "FLOAT",  False),
    ("Subsurface Radius", "NodeSocketVector", (1.0, 0.2, 0.1),      "Subsurface Radius", "VECTOR", False),
]

N_CH = len(CHANNELS)
CH_TO_IDX = {c[0]: i for i, c in enumerate(CHANNELS)}
CHANNEL_BY_NAME = {c[0]: c for c in CHANNELS}

# Sub-section grouping inside each layer panel.
# (panel_name_or_None, [channel_names]).  None means "directly under the
# layer panel"; otherwise the channels live in a nested sub-panel of the
# given name that defaults to closed.
SUBSECTIONS = [
    (None,         ["Color", "Mask", "Normal"]),
    ("PBR",        ["Roughness", "Metallic", "Alpha"]),
    ("Emission",   ["Emission Color", "Emission Strength"]),
    ("Subsurface", ["Subsurface Weight", "Subsurface Radius"]),
]
