# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 NXSTYNATE
#
# bsdf_node.py — BSDFStackNode (ShaderNodeCustomGroup)
#
# Sibling of StackNode: same per-layer UX (Add Layer button, blend mode,
# opacity, reorder, remove) but each layer exposes a Principled-BSDF-style
# set of inputs. Inputs are blended per-channel by the layer's mask and
# opacity, then fed into a single internal Principled BSDF whose output
# is exposed as a Shader output socket.
#
# Per-layer inputs live inside a NodeTreeInterfacePanel named after the
# layer ("Layer 0", "Layer 1", ...). Within each layer panel, secondary
# channels are grouped into nested sub-panels (PBR / Normal / Emission /
# Subsurface) that default to closed — mirroring Principled BSDF's own
# subsection layout.

import bpy
from bpy.props import CollectionProperty
from bpy.types import ShaderNodeCustomGroup

from .constants import (
    CHANNELS, N_CH, CH_TO_IDX, CHANNEL_BY_NAME, SUBSECTIONS, NORMAL_CHANNELS,
    channel_is_stated,
)
from .properties import StackLayerProperties
from .utils import get_node_id


def _mix_socket_indices(data_type):
    """Return (A_index, B_index, Result_index) for ShaderNodeMix sockets."""
    if data_type == "FLOAT":
        return 2, 3, 0
    if data_type == "VECTOR":
        return 4, 5, 1
    if data_type == "RGBA":
        return 6, 7, 2
    raise ValueError(data_type)


def _factor_node_name(layer_index, ch_name):
    """Stable internal name for a layer/channel factor Math node."""
    return f"L{layer_index} {ch_name} Factor"


def _mix_node_name(layer_index, ch_name):
    """Stable internal name for a layer/channel Mix node."""
    return f"L{layer_index} {ch_name} Mix"


def _layer_panel_name(layer, layer_index):
    # Prefix with a filled square so layer headers visually stand apart
    # from the nested PBR / Emission / Subsurface sub-panel headers.
    name = layer.layer_name or f"Layer {layer_index}"
    return f"■ {name}"


def _is_top_level_panel(item):
    """A layer panel sits at the interface root (parent is None or unnamed)."""
    if item.item_type != 'PANEL':
        return False
    return item.parent is None or item.parent.name == ''


# Deferred-rebuild queue. update() must NOT mutate the node tree inline:
# Blender may call it during depsgraph evaluation / material-preview shader
# compilation (on a localized copy of the tree), and mutating the tree there
# crashes Blender. Instead we record (node_tree, node_name) and flush the
# rebuilds from an app timer that runs in the main thread, outside evaluation.
_pending_rebuilds = []


def _flush_pending_rebuilds():
    global _pending_rebuilds
    todo, _pending_rebuilds = _pending_rebuilds, []
    for ntree, node_name in todo:
        try:
            n = ntree.nodes.get(node_name)
        except Exception:
            n = None  # tree was a transient/evaluated copy, now freed
        if n is not None:
            try:
                n.rebuild_internals()
            except Exception:
                pass
    return None  # one-shot; do not reschedule


def _request_rebuild(node):
    """Queue a deferred rebuild for ``node`` (safe to call from update())."""
    try:
        _pending_rebuilds.append((node.id_data, node.name))
        if not bpy.app.timers.is_registered(_flush_pending_rebuilds):
            bpy.app.timers.register(_flush_pending_rebuilds, first_interval=0.0)
    except Exception:
        pass


class BSDFStackNode(ShaderNodeCustomGroup):
    """Layer blending node with per-layer PBR inputs feeding one Principled BSDF."""
    bl_idname = "BSDFStackNodeType"
    bl_label = "BSDF Stack"
    bl_icon = 'NODE_MATERIAL'
    bl_width_default = 260

    layers: CollectionProperty(type=StackLayerProperties)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def init(self, context):
        self.node_tree = bpy.data.node_groups.new(
            f".bsdf_stack_{id(self)}", 'ShaderNodeTree',
        )
        self.node_tree.use_fake_user = True

        self.node_tree.interface.new_socket(
            name="BSDF", in_out='OUTPUT', socket_type='NodeSocketShader',
        )

        # First layer relies on property defaults (blend=MIX, opacity=1,
        # enabled=True). Empty layer_name uses the "Layer N" panel fallback.
        self.layers.add()
        # add_layer_to_group → rebuild_group → rebuild_internals.
        self.add_layer_to_group()

    def free(self):
        if self.node_tree:
            self.node_tree.use_fake_user = False
            bpy.data.node_groups.remove(self.node_tree)

    def copy(self, original):
        self.node_tree = original.node_tree.copy()
        self.node_tree.use_fake_user = True

        self.layers.clear()
        for src in original.layers:
            dst = self.layers.add()
            dst.layer_name  = src.layer_name
            dst.blend_mode  = src.blend_mode
            dst.opacity     = src.opacity
            dst.enabled     = src.enabled

    def update(self):
        """Schedule a rebuild when external Normal links change.

        Blender calls update() whenever the containing tree's links change.
        The normal-like channels wire their mix-chain based on each layer's
        socket ``is_linked`` state at build time (an unlinked layer falls back
        to the geometry normal). Because connecting a normal map does not
        otherwise trigger a rebuild, the map would be ignored until the next
        add/remove/move-layer. We detect a change in the set of linked normal
        sockets here, guarded by an ID-property so the very frequent update()
        calls don't queue redundant rebuilds. Other channels wire their group
        input through unconditionally, so they already react live and need no
        rebuild.

        IMPORTANT: this method must NOT modify the node tree directly.
        update() can fire during depsgraph evaluation / material-preview shader
        compilation (on a localized copy of the tree), where mutating the tree
        crashes Blender. The actual rebuild is deferred to an app timer via
        _request_rebuild().
        """
        if not self.node_tree:
            return
        # A tree saved before a channel was added still has the old number of
        # sockets per layer, so every _socket_for() below would read the wrong
        # one. Relayout first; the queued rebuild does it (see ensure_layout).
        if len(self.inputs) != len(self.layers) * N_CH:
            _request_rebuild(self)
            return
        try:
            mask = 0
            bit = 0
            for ch_name in NORMAL_CHANNELS:
                ci = CH_TO_IDX[ch_name]
                for i in range(len(self.layers)):
                    sock = self._socket_for(i, ci)
                    if sock is not None and sock.is_linked:
                        mask |= (1 << bit)
                    bit += 1
        except Exception:
            return
        if self.get("_normal_link_mask", -1) != mask:
            self["_normal_link_mask"] = mask
            _request_rebuild(self)

    # ------------------------------------------------------------------
    # Socket management
    # ------------------------------------------------------------------

    def _socket_for(self, layer_index, channel_index):
        """Look up a node input socket by (layer, channel) indices."""
        idx = layer_index * N_CH + channel_index
        if 0 <= idx < len(self.inputs):
            return self.inputs[idx]
        return None

    def _group_output_for(self, g_in, layer_index, channel_index):
        """Matching GROUP_INPUT output socket for (layer, channel)."""
        idx = layer_index * N_CH + channel_index
        if 0 <= idx < len(g_in.outputs):
            return g_in.outputs[idx]
        return None

    def ensure_layout(self):
        """Bring the socket interface in line with the current CHANNELS table.

        A .blend saved before a channel existed still holds the old number of
        sockets per layer, and every lookup in this node, the exporter and the
        importer addresses a socket as ``layer_index * N_CH + channel_index``.
        Until the interface is rebuilt those indices land on the wrong sockets:
        a roughness read as a metalness, a mask read as an alpha, and nothing
        raising anywhere. So this runs before anything reads them -- from the
        load_post handler in this package's __init__, and defensively from
        rebuild_internals() and update().

        The rebuild is lossless: _snapshot_state keys saved defaults and links
        by channel NAME, so existing wiring lands back on the same channels and
        the new ones come up at their defaults, which channel_is_stated then
        reads as "this layer has no opinion".

        Returns True if a rebuild was needed.
        """
        if self.node_tree is None:
            return False
        if len(self.inputs) == len(self.layers) * N_CH:
            return False
        self.rebuild_group(old_to_new=None)
        return True

    def add_layer_to_group(self):
        """Add sockets for a new layer.

        Delegates to rebuild_group() so the full panel/socket interface is
        rebuilt with the new layer's panel in the right place.
        """
        self.rebuild_group(old_to_new=None)

    def rebuild_group(self, old_to_new=None):
        """Rebuild the panel/socket interface after add/remove/reorder.

        Each layer becomes one top-level NodeTreeInterfacePanel; channels
        within the layer are split across nested sub-panels per SUBSECTIONS.

        Saved state (default values, external links, panel collapse states)
        is keyed by channel/sub-panel NAME so the restore step survives
        reorderings of CHANNELS in source.
        """
        parent_tree = self.id_data

        old_layer_count, old_stride = self._old_socket_layout()

        saved_sockets, old_layer_closed, old_subpanel_closed = \
            self._snapshot_state(parent_tree, old_layer_count, old_stride)
        new_to_old = self._resolve_layer_mapping(old_to_new, old_layer_count)
        self._clear_interface()
        self._rebuild_panels(new_to_old, old_layer_closed, old_subpanel_closed)
        self._restore_state(parent_tree, new_to_old, saved_sockets)

        # Layout is correct now, so the relayout guard in rebuild_internals()
        # must not fire again -- it would recurse straight back into here.
        self.rebuild_internals(_allow_relayout=False)

    def _old_socket_layout(self):
        """(layer count, sockets per layer) of the interface as it stands now.

        Read from the interface's own panel structure rather than assumed to be
        N_CH: one top-level panel is one layer, so a node saved by a build with
        fewer channels reports ITS stride, not today's. That is the difference
        between growing CHANNELS being a migration and being a corruption --
        snapshotting a 10-socket-per-layer node at a stride of 16 walks straight
        off the end of layer 0 and files layer 1's sockets under layer 0's name.
        """
        n_inputs = len(self.inputs)
        panels = [
            it for it in self.node_tree.interface.items_tree
            if _is_top_level_panel(it)
        ]
        count = len(panels)
        if count <= 0 or n_inputs % count:
            # Fresh node, or a layout we can't read: assume the current stride
            # and let the rebuild lay the interface out from scratch.
            return n_inputs // N_CH, N_CH
        return count, n_inputs // count

    def _snapshot_state(self, parent_tree, old_layer_count, old_stride=None):
        """Snapshot socket defaults/links and panel collapse states.

        Returns (saved_sockets, old_layer_closed, old_subpanel_closed) where
        saved_sockets maps (layer_index, channel_name) -> {'link_from', 'default'}.
        """
        nt = self.node_tree

        # Build the to_socket -> from_socket map once instead of scanning
        # parent_tree.links per input socket.
        link_from_by_socket = {}
        if parent_tree and hasattr(parent_tree, 'links'):
            for link in parent_tree.links:
                if link.to_node == self:
                    link_from_by_socket[link.to_socket.as_pointer()] = link.from_socket

        if old_stride is None:
            old_stride = N_CH

        saved_sockets = {}
        for li in range(old_layer_count):
            for offset in range(old_stride):
                idx = li * old_stride + offset
                if idx >= len(self.inputs):
                    continue
                inp = self.inputs[idx]
                link_from = link_from_by_socket.get(inp.as_pointer())
                try:
                    default = tuple(inp.default_value)
                except TypeError:
                    default = inp.default_value
                saved_sockets[(li, inp.name)] = {
                    'link_from': link_from, 'default': default,
                }

        # Collapse states per old layer: layer panel + each sub-panel.
        old_layer_panels = [it for it in nt.interface.items_tree if _is_top_level_panel(it)]
        old_layer_closed = [bool(lp.default_closed) for lp in old_layer_panels]
        old_subpanel_closed = []  # list[dict[subpanel_name -> bool]]
        for lp in old_layer_panels:
            sub_states = {}
            for child in lp.interface_items:
                if child.item_type == 'PANEL':
                    sub_states[child.name] = bool(child.default_closed)
            old_subpanel_closed.append(sub_states)

        return saved_sockets, old_layer_closed, old_subpanel_closed

    def _resolve_layer_mapping(self, old_to_new, old_layer_count):
        """Resolve old_to_new into a new-layer-index -> old-layer-index dict."""
        if old_to_new is None:
            return {i: i for i in range(min(old_layer_count, len(self.layers)))}
        if isinstance(old_to_new, list):
            pairs = enumerate(old_to_new)
        else:
            pairs = old_to_new.items()
        new_to_old = {}
        for old_i, new_i in pairs:
            if new_i is None:
                continue
            new_to_old[new_i] = old_i
        return new_to_old

    def _clear_interface(self):
        """Wipe all input sockets + panels (keep outputs)."""
        nt = self.node_tree
        # Remove sockets first, then panels (leaf-up keeps the API happy).
        to_remove_sockets = [
            item for item in list(nt.interface.items_tree)
            if item.item_type == 'SOCKET' and item.in_out == 'INPUT'
        ]
        for item in to_remove_sockets:
            try:
                nt.interface.remove(item)
            except Exception:
                pass
        to_remove_panels = [
            item for item in list(nt.interface.items_tree)
            if item.item_type == 'PANEL'
        ]
        # Remove deepest first so parents don't disappear under us.
        for item in reversed(to_remove_panels):
            try:
                nt.interface.remove(item)
            except Exception:
                pass

    def _rebuild_panels(self, new_to_old, old_layer_closed, old_subpanel_closed):
        """Create one layer panel + nested sub-panels (and sockets) per layer."""
        nt = self.node_tree
        for new_li, layer in enumerate(self.layers):
            old_li = new_to_old.get(new_li)
            layer_closed = (
                old_layer_closed[old_li]
                if old_li is not None and 0 <= old_li < len(old_layer_closed)
                else False
            )
            old_sub_states = (
                old_subpanel_closed[old_li]
                if old_li is not None and 0 <= old_li < len(old_subpanel_closed)
                else {}
            )

            layer_panel = nt.interface.new_panel(
                _layer_panel_name(layer, new_li),
                default_closed=layer_closed,
            )

            for sub_name, sub_channels in SUBSECTIONS:
                if sub_name is None:
                    # Direct children of the layer panel.
                    target = layer_panel
                else:
                    # Sub-panels default to closed (Principled-BSDF feel),
                    # but inherit prior collapse state if the user expanded
                    # one previously.
                    sub_closed = old_sub_states.get(sub_name, True)
                    sub_panel = nt.interface.new_panel(
                        sub_name, default_closed=sub_closed,
                    )
                    # new_panel always appends to the root; move under layer.
                    nt.interface.move_to_parent(
                        sub_panel, layer_panel, len(layer_panel.interface_items),
                    )
                    target = sub_panel

                for ch_name in sub_channels:
                    ch = CHANNEL_BY_NAME[ch_name]
                    sock = nt.interface.new_socket(
                        name=ch_name,
                        in_out='INPUT',
                        socket_type=ch[1],
                        parent=target,
                    )
                    if ch[5]:
                        sock.hide_value = True

    def _restore_state(self, parent_tree, new_to_old, saved_sockets):
        """Restore socket defaults + external links by channel name."""
        for new_li in range(len(self.layers)):
            old_li = new_to_old.get(new_li)
            base = new_li * N_CH
            for offset in range(N_CH):
                idx = base + offset
                if idx >= len(self.inputs):
                    continue
                inp = self.inputs[idx]
                ch_name = inp.name
                data = saved_sockets.get((old_li, ch_name)) if old_li is not None else None
                if data is not None:
                    try:
                        inp.default_value = data['default']
                    except Exception:
                        pass
                    if data['link_from'] and parent_tree:
                        try:
                            parent_tree.links.new(data['link_from'], inp)
                        except Exception:
                            pass
                else:
                    ch = CHANNEL_BY_NAME.get(ch_name)
                    if ch is not None and ch[2] is not None:
                        try:
                            inp.default_value = ch[2]
                        except Exception:
                            pass

    def sync_panel_names(self):
        """Rename existing layer panels to match current layer names. Cheap, no rebuild."""
        nt = self.node_tree
        panels = [it for it in nt.interface.items_tree if _is_top_level_panel(it)]
        for li, panel in enumerate(panels):
            if li >= len(self.layers):
                break
            new_name = _layer_panel_name(self.layers[li], li)
            if panel.name != new_name:
                panel.name = new_name

    # ------------------------------------------------------------------
    # Internal chain
    # ------------------------------------------------------------------

    def rebuild_internals(self, _allow_relayout=True):
        """Rebuild the internal per-channel mix chains and Principled BSDF."""
        nt = self.node_tree
        if nt is None:
            return
        # Interface saved by a build with a different channel count: fix the
        # sockets first, or every _socket_for() below reads the wrong one.
        # rebuild_group() ends by calling back here with the layout correct.
        if _allow_relayout and len(self.inputs) != len(self.layers) * N_CH:
            self.rebuild_group(old_to_new=None)
            return

        g_in = None
        g_out = None
        to_remove = []
        for n in nt.nodes:
            if n.type == 'GROUP_INPUT':
                g_in = n
            elif n.type == 'GROUP_OUTPUT':
                g_out = n
            else:
                to_remove.append(n)
        for n in to_remove:
            nt.nodes.remove(n)
        for link in list(nt.links):
            nt.links.remove(link)

        if g_in is None:
            g_in = nt.nodes.new('NodeGroupInput')
        g_in.location = (-1400, 0)

        if g_out is None:
            g_out = nt.nodes.new('NodeGroupOutput')
        g_out.location = (700, 0)

        bsdf = nt.nodes.new('ShaderNodeBsdfPrincipled')
        bsdf.location = (350, 0)

        geometry = None
        def _get_geometry():
            nonlocal geometry
            if geometry is None:
                geometry = nt.nodes.new('ShaderNodeNewGeometry')
                geometry.location = (-1200, -1400)
            return geometry

        mask_ci = CH_TO_IDX["Mask"]
        channel_outputs = {}

        for ch_idx, (ch_name, _sock_type, _default, _bsdf, mix_type, _hv) in enumerate(CHANNELS):
            if mix_type is None:
                continue

            prev_output = None
            y_base = -ch_idx * 200

            for i, layer in enumerate(self.layers):
                channel_out = self._group_output_for(g_in, i, ch_idx)
                mask_out    = self._group_output_for(g_in, i, mask_ci)
                if channel_out is None or mask_out is None:
                    continue

                factor = nt.nodes.new('ShaderNodeMath')
                factor.operation = 'MULTIPLY'
                factor.location = (-1000 + i * 220, y_base - 90)
                # Stable name: update_layer_factors() looks factor nodes up
                # by this name to adjust opacity/enabled without a rebuild.
                factor.name = _factor_node_name(i, ch_name)
                factor.label = factor.name
                factor.inputs[0].default_value = (
                    layer.opacity if layer.enabled else 0.0
                )
                nt.links.new(mask_out, factor.inputs[1])

                mix = nt.nodes.new('ShaderNodeMix')
                mix.data_type = mix_type
                mix.name = _mix_node_name(i, ch_name)
                mix.location = (-780 + i * 220, y_base)

                if mix_type == "RGBA":
                    mix.clamp_result = True
                if ch_name == "Color" and mix_type == "RGBA":
                    if prev_output is not None and layer.enabled:
                        mix.blend_type = layer.blend_mode
                        mix.label = f"L{i} {layer.blend_mode}"
                    else:
                        mix.blend_type = 'MIX'
                        mix.label = f"L{i} Base" if prev_output is None else f"L{i} MIX"

                a_idx, b_idx, out_idx = _mix_socket_indices(mix_type)

                nt.links.new(factor.outputs[0], mix.inputs[0])

                if prev_output is None:
                    base = CHANNELS[ch_idx][2]
                    if base is not None:
                        try:
                            mix.inputs[a_idx].default_value = base
                        except Exception:
                            pass
                else:
                    nt.links.new(prev_output, mix.inputs[a_idx])

                # A layer only touches a channel it actually STATES -- one it
                # has something wired into, or whose value it moved off the
                # default. An untouched upper layer must NOT stamp its socket
                # default over the layers below it: at a default (1.0) mask
                # that erases them, so a layer added to tint some dirt would
                # also pull their roughness to 0.5, force their metalness to 0
                # and black out their emission. Passing the accumulated value
                # into B makes B == A, and the mix a no-op, at any mask.
                #
                # Normal always worked this way; the rule is simply no longer
                # special-cased to it.
                stated = channel_is_stated(
                    self._socket_for(i, ch_idx), CHANNELS[ch_idx][2],
                )
                if stated:
                    nt.links.new(channel_out, mix.inputs[b_idx])
                elif prev_output is not None:
                    nt.links.new(prev_output, mix.inputs[b_idx])
                elif ch_name in NORMAL_CHANNELS:
                    # Base layer, no map → start from the true surface normal.
                    nt.links.new(
                        _get_geometry().outputs["Normal"],
                        mix.inputs[b_idx],
                    )
                else:
                    # Base layer → its socket default IS the material.
                    nt.links.new(channel_out, mix.inputs[b_idx])

                prev_output = mix.outputs[out_idx]

            channel_outputs[ch_name] = prev_output

        for ch_name, _sock_type, _default, bsdf_name, _mix, _hv in CHANNELS:
            if bsdf_name is None or ch_name in NORMAL_CHANNELS:
                continue
            out = channel_outputs.get(ch_name)
            if out is not None and bsdf_name in bsdf.inputs:
                nt.links.new(out, bsdf.inputs[bsdf_name])

        # Normal and Coat Normal take the same two exceptions. They reach the
        # BSDF only through a Normalize, because mixing two unit vectors
        # componentwise shortens the result; and only when some layer actually
        # supplies one, because an unconnected BSDF normal socket means "the
        # shading normal" and that is exactly what an all-default stack wants.
        for ch_name in NORMAL_CHANNELS:
            ci = CH_TO_IDX.get(ch_name)
            out = channel_outputs.get(ch_name)
            bsdf_name = CHANNEL_BY_NAME[ch_name][3]
            if ci is None or out is None or bsdf_name not in bsdf.inputs:
                continue
            linked = False
            for i in range(len(self.layers)):
                sock = self._socket_for(i, ci)
                if sock is not None and sock.is_linked:
                    linked = True
                    break
            if not linked:
                continue
            normalize = nt.nodes.new('ShaderNodeVectorMath')
            normalize.operation = 'NORMALIZE'
            normalize.location = (150, -ci * 200)
            nt.links.new(out, normalize.inputs[0])
            nt.links.new(normalize.outputs[0], bsdf.inputs[bsdf_name])

        nt.links.new(bsdf.outputs["BSDF"], g_out.inputs[0])
        nt.update_tag()

    def update_layer_factors(self, layer_index):
        """Targeted in-place update for an opacity/enabled change.

        Opacity/enabled only drive the factor Math nodes' first input (and,
        for the Color channel, the blend_type baked in by rebuild_internals
        while the layer was disabled). Updating those in place avoids the
        full rebuild on every slider drag.

        Returns True on success; False means the expected internal nodes
        were not found and the caller should fall back to a full rebuild.
        """
        nt = self.node_tree
        if nt is None or not (0 <= layer_index < len(self.layers)):
            return False
        layer = self.layers[layer_index]
        factor_value = layer.opacity if layer.enabled else 0.0

        updated = False
        for ch_name, _sock_type, _default, _bsdf, mix_type, _hv in CHANNELS:
            if mix_type is None:
                continue
            factor = nt.nodes.get(_factor_node_name(layer_index, ch_name))
            if factor is None:
                # Internal chain doesn't match expectations (e.g. tree built
                # by an older version without stable names) — full rebuild.
                return False
            factor.inputs[0].default_value = factor_value
            updated = True

        # Keep the Color mix's blend_type in sync with rebuild_internals():
        # a rebuild done while the layer was disabled bakes in 'MIX', which
        # must be restored to the layer's blend mode on re-enable. Layer 0
        # (the base layer) always uses 'MIX'.
        if layer_index > 0:
            mix = nt.nodes.get(_mix_node_name(layer_index, "Color"))
            if mix is None:
                return False
            if layer.enabled:
                mix.blend_type = layer.blend_mode
                mix.label = f"L{layer_index} {layer.blend_mode}"
            else:
                mix.blend_type = 'MIX'
                mix.label = f"L{layer_index} MIX"

        if updated:
            nt.update_tag()
        return updated

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    #
    # Per-layer sockets are drawn natively by Blender under each layer's
    # panel (and its nested PBR/Normal/Emission/Subsurface sub-panels) —
    # click the panel triangle on the node to expand/collapse. draw_buttons
    # handles only the per-layer settings (name, enabled, blend mode,
    # opacity, move, remove) plus the global "Add Layer" button.

    def draw_buttons(self, context, layout):
        nid = get_node_id(self)

        op = layout.operator(
            "stack_node.add_layer", text="Add Layer", icon='ADD',
        )
        op.group_name = nid

        layout.separator()

        num_layers = len(self.layers)
        for draw_i in range(num_layers):
            i = num_layers - 1 - draw_i
            layer = self.layers[i]

            box = layout.box()

            header = box.row(align=True)
            header.prop(
                layer, "enabled", text="",
                icon='CHECKBOX_HLT' if layer.enabled else 'CHECKBOX_DEHLT',
            )
            header.prop(layer, "layer_name", text="", placeholder=f"Layer {i}")

            op = header.operator(
                "stack_node.move_layer", text="", icon='TRIA_UP',
            )
            op.group_name = nid
            op.layer_index = i
            op.direction = 'DOWN'

            op = header.operator(
                "stack_node.move_layer", text="", icon='TRIA_DOWN',
            )
            op.group_name = nid
            op.layer_index = i
            op.direction = 'UP'

            if num_layers > 1:
                op = header.operator(
                    "stack_node.remove_layer", text="", icon='X',
                )
                op.group_name = nid
                op.layer_index = i

            if not layer.enabled:
                continue

            row = box.row(align=True)
            row.prop(layer, "blend_mode", text="")
            row = box.row(align=True)
            row.prop(layer, "opacity", text="Opacity", slider=True)

    def draw_buttons_ext(self, context, layout):
        self.draw_buttons(context, layout)
