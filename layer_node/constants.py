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

# constants.py — Blend modes, the per-layer channel table, and the socket
# addressing every reader of a BSDFStackNode shares.
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
    # Coat and Sheen mirror the Principled sockets of the same names, including
    # their defaults (Coat Roughness 0.03, Sheen Roughness 0.5), so a layer that
    # has not been touched reads as untouched by channel_is_stated below.
    # Coat IOR and Coat Tint are deliberately absent: glTF's clearcoat has no
    # slot for either, so a socket for them would only promise what no exported
    # file can carry.
    ("Coat Weight",       "NodeSocketFloat",  0.0,                  "Coat Weight",       "FLOAT",  False),
    ("Coat Roughness",    "NodeSocketFloat",  0.03,                 "Coat Roughness",    "FLOAT",  False),
    ("Coat Normal",       "NodeSocketVector", (0.0, 0.0, 1.0),      "Coat Normal",       "VECTOR", True),
    ("Sheen Weight",      "NodeSocketFloat",  0.0,                  "Sheen Weight",      "FLOAT",  False),
    ("Sheen Tint",        "NodeSocketColor",  (1.0, 1.0, 1.0, 1.0), "Sheen Tint",        "RGBA",   False),
    ("Sheen Roughness",   "NodeSocketFloat",  0.5,                  "Sheen Roughness",   "FLOAT",  False),
]

N_CH = len(CHANNELS)
CH_TO_IDX = {c[0]: i for i, c in enumerate(CHANNELS)}
CHANNEL_BY_NAME = {c[0]: c for c in CHANNELS}

# Channels holding a tangent-space normal. They are the exceptions in the mix
# chain: an unstated base layer starts from the true geometry normal rather than
# from a socket default, and the blended result is renormalized before it
# reaches the BSDF instead of being wired straight in.
NORMAL_CHANNELS = ("Normal", "Coat Normal")


def channel_is_stated(socket, default):
    """Whether a layer says anything at all on this channel.

    A linked socket always states something. An unlinked one states something
    only once it has been moved off the channel's default: an untouched layer
    has no opinion, and blending its default over the layers below it is not a
    no-op. That is the difference between adding a layer to tint some dirt and
    adding one that also drags roughness to 0.5, forces metalness to 0 and
    blacks out every emission underneath.

    The Normal channel always followed this rule; every channel follows it now.
    It is also the rule the glTF side needs. `CUSTOM_materials_layers` has no
    spelling for "unstated", so a reader takes an all-default pbr block to mean
    the layer left the surface alone -- and an exporter that cannot produce one
    cannot express a colour-only layer.

    Duck-typed on purpose: this module stays bpy-free so the export and import
    material modules can share it.
    """
    if socket is None or default is None:
        return False
    if getattr(socket, "is_linked", False):
        return True
    try:
        value = socket.default_value
    except AttributeError:
        return False
    try:
        seq = tuple(value)
    except TypeError:
        try:
            return abs(float(value) - float(default)) > 1e-6
        except (TypeError, ValueError):
            return False
    ref = tuple(default) if hasattr(default, "__len__") else (default,)
    return any(abs(float(a) - float(b)) > 1e-6 for a, b in zip(seq, ref))

# Sub-section grouping inside each layer panel.
# (panel_name_or_None, [channel_names]).  None means "directly under the
# layer panel"; otherwise the channels live in a nested sub-panel of the
# given name that defaults to closed.
SUBSECTIONS = [
    (None,         ["Color", "Mask", "Normal"]),
    ("PBR",        ["Roughness", "Metallic", "Alpha"]),
    ("Emission",   ["Emission Color", "Emission Strength"]),
    ("Subsurface", ["Subsurface Weight", "Subsurface Radius"]),
    ("Coat",       ["Coat Weight", "Coat Roughness", "Coat Normal"]),
    ("Sheen",      ["Sheen Weight", "Sheen Tint", "Sheen Roughness"]),
]

# The two tables above are one table seen twice, and nothing enforces that at
# the point of edit: CHANNELS fixes the socket INDEX a channel is addressed by
# (CH_TO_IDX, and layer_index * N_CH + index everywhere else), while SUBSECTIONS
# fixes the order the sockets are actually CREATED in. Let them disagree and
# every lookup silently reads the neighbouring channel -- a roughness that comes
# back as a metalness, with nothing raising. Catch it at import instead.
_SUBSECTION_ORDER = [name for _panel, names in SUBSECTIONS for name in names]
if _SUBSECTION_ORDER != [c[0] for c in CHANNELS]:
    raise RuntimeError(
        "CHANNELS and SUBSECTIONS disagree; socket indices would be wrong.\n"
        f"  CHANNELS:    {[c[0] for c in CHANNELS]}\n"
        f"  SUBSECTIONS: {_SUBSECTION_ORDER}"
    )


# Principled BSDF socket name -> BSDF Stack channel name.
#
# One layer of a stack IS a Principled material, so the glTF exporter reads a
# layer through the same _gather_* functions a plain Principled goes through and
# the importer writes one through the same _apply_* functions. Both need this
# map, and it lives here rather than in either of them because a channel added
# on one side and forgotten on the other is exactly the silent drop the two
# halves keep being rebuilt to avoid.
#
# Absent names (IOR, Transmission, Coat IOR/Tint) have no channel on the stack.
# A socket view returns None for them, which every gatherer already reads as
# "this Blender has no such socket" and handles by emitting nothing.
STACK_AS_PRINCIPLED = {
    "Base Color": "Color",
    "Metallic": "Metallic",
    "Roughness": "Roughness",
    "Normal": "Normal",
    "Alpha": "Alpha",
    "Emission Color": "Emission Color",
    "Emission Strength": "Emission Strength",
    "Subsurface Weight": "Subsurface Weight",
    "Subsurface Radius": "Subsurface Radius",
    # Coat and Sheen carry the same name on both sides; listed anyway, because
    # an absent key means "no such socket".
    "Coat Weight": "Coat Weight",
    "Coat Roughness": "Coat Roughness",
    "Coat Normal": "Coat Normal",
    "Sheen Weight": "Sheen Weight",
    "Sheen Tint": "Sheen Tint",
    "Sheen Roughness": "Sheen Roughness",
}


def socket_index(layer_index, channel_name):
    """Index of a (layer, channel) socket in a BSDFStackNode's flat input list.

    Sockets are laid out layer by layer, N_CH of them each, in CHANNELS order.
    The formula is written once here because every place that gets it wrong
    fails the same silent way: a valid socket of the wrong channel, read as if
    it were the right one.
    """
    return layer_index * N_CH + CH_TO_IDX[channel_name]


class PrincipledSocketView:
    """A BSDF Stack layer's sockets, addressed by Principled BSDF socket name.

    Enough of a `node.inputs` to satisfy the glTF material code, which reaches
    a shader node only through `.get`, `[]` and `in`. Wrapping a layer in one
    lets a stack travel the exact code path a plain Principled travels -- the
    point being that a one-layer stack is not *like* a plain material, it is
    one, and has to export and import as one.
    """

    __slots__ = ("_node", "_index")

    def __init__(self, node, index):
        self._node = node
        self._index = index

    def get(self, name, default=None):
        channel = STACK_AS_PRINCIPLED.get(name)
        if channel is None:
            return default
        idx = socket_index(self._index, channel)
        if 0 <= idx < len(self._node.inputs):
            return self._node.inputs[idx]
        return default

    def __getitem__(self, name):
        socket = self.get(name)
        if socket is None:
            raise KeyError(name)
        return socket

    def __contains__(self, name):
        return self.get(name) is not None
