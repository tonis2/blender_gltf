"""KHR_interactivity importer.

Reads `extensions.KHR_interactivity.graphs` from the root and, for each
object whose node references a graph index, rebuilds an Interactivity
NodeTree and binds it to `obj.gltf_interactivity`.

Round-trips the layout produced by `export.interactivity.InteractivityExporter`.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..interactivity_nodes import (
    EXT_INTERACTIVITY,
    FLOW_SOCKET_BL_IDNAME,
    POINTER_TEMPLATES,
    TREE_BL_IDNAME,
)

if TYPE_CHECKING:
    import bpy
    from ..gltf.types import Gltf, Node
    from ..importer import ImportSettings


def _build_pointer_matchers():
    """Derive pointer-path matchers from the shared POINTER_TEMPLATES.

    Each template path (with `{idx}`/`{sub}` placeholders) becomes an anchored
    regex. `{idx}` and `{sub}` capture integer components. The match yields the
    same (kind, idx, property, sub_idx) tuple the importer needs, with sub_idx
    populated only for templates carrying a `{sub}` placeholder (morph WEIGHT).
    """
    matchers = []
    for kind, props in POINTER_TEMPLATES.items():
        for prop, template in props.items():
            has_sub = "{sub}" in template
            pattern = "^" + re.escape(template) + "$"
            pattern = pattern.replace(re.escape("{idx}"), r"(\d+)")
            pattern = pattern.replace(re.escape("{sub}"), r"(\d+)")
            matchers.append((re.compile(pattern), kind, prop, has_sub))
    return matchers


_POINTER_MATCHERS = _build_pointer_matchers()


def _parse_pointer(p: str):
    """Parse a JSON pointer into (kind, idx, property, sub_idx) or None."""
    if not isinstance(p, str) or not p.startswith("/"):
        return None
    for regex, kind, prop, has_sub in _POINTER_MATCHERS:
        m = regex.match(p)
        if m is None:
            continue
        idx = int(m.group(1))
        sub_idx = int(m.group(2)) if has_sub else None
        return (kind, idx, prop, sub_idx)
    return None


def _find_obj_data_by_index(gltf, node_to_blender, idx: int, matcher, obj_type: str):
    """Find the Blender object-data datablock backing a gltf array element.

    `matcher(gltf_node)` returns True when that gltf node references the wanted
    gltf array index; `obj_type` is the expected Blender `obj.type`. Returns the
    matched datablock or None (no bpy.data fallback: alphabetical index ordering
    would bind unrelated datablocks when importing into a non-empty .blend).
    """
    if gltf.nodes is not None:
        for i, gn in enumerate(gltf.nodes):
            if not matcher(gn):
                continue
            obj = node_to_blender.get(i)
            if obj is not None and obj.data is not None and obj.type == obj_type:
                return obj.data
    return None


def _find_light_by_index(gltf, node_to_blender, light_idx: int):
    """Find the Blender Light datablock for KHR_lights_punctual lights[idx]."""
    def matcher(gn):
        ext = gn.extensions
        if ext is None:
            return False
        lp = ext.get("KHR_lights_punctual")
        return lp is not None and lp.get("light") == light_idx

    return _find_obj_data_by_index(gltf, node_to_blender, light_idx, matcher, "LIGHT")


def _find_camera_by_index(gltf, node_to_blender, camera_idx: int):
    """Find the Blender Camera datablock for gltf cameras[camera_idx]."""
    return _find_obj_data_by_index(
        gltf, node_to_blender, camera_idx,
        lambda gn: gn.camera == camera_idx, "CAMERA",
    )


class InteractivityImporter:
    def __init__(self, gltf: "Gltf", settings: "ImportSettings") -> None:
        self.gltf = gltf
        self.settings = settings
        self._graphs = self._extract_graphs()
        self._declarations = self._extract_declarations()
        # Lazy: graph index -> instantiated NodeTree, so two objects sharing
        # the same graph share the same tree.
        self._cached_trees: dict[int, "bpy.types.NodeTree"] = {}

    def has_interactivity(self) -> bool:
        if not self._graphs:
            return False
        if self.gltf.nodes is None:
            return False
        for n in self.gltf.nodes:
            if n.extensions and EXT_INTERACTIVITY in n.extensions:
                return True
        return False

    def import_node(
        self,
        context: "bpy.types.Context",
        obj: "bpy.types.Object",
        node: "Node",
    ) -> None:
        if node.extensions is None:
            return
        ext = node.extensions.get(EXT_INTERACTIVITY)
        if ext is None:
            return
        graph_idx = ext.get("graph")
        if not isinstance(graph_idx, int) or not (0 <= graph_idx < len(self._graphs)):
            return

        tree = self._cached_trees.get(graph_idx)
        if tree is None:
            tree = self._build_tree(context, graph_idx, obj.name)
            self._cached_trees[graph_idx] = tree
        obj.gltf_interactivity = tree

    # ------- Helpers ------------------------------------------------------

    def _extract_graphs(self) -> list[dict]:
        ext_root = self._root_ext()
        if ext_root is None:
            return []
        return list(ext_root.get("graphs", []))

    def _extract_declarations(self) -> list[dict]:
        ext_root = self._root_ext()
        if ext_root is None:
            return []
        return list(ext_root.get("declarations", []))

    def _root_ext(self) -> dict | None:
        ext = getattr(self.gltf, "extensions", None) or {}
        return ext.get(EXT_INTERACTIVITY)

    def _op_for_decl(self, decl_idx: int) -> str:
        if 0 <= decl_idx < len(self._declarations):
            return self._declarations[decl_idx].get("op", "")
        return ""

    def _build_tree(
        self,
        context: "bpy.types.Context",
        graph_idx: int,
        obj_name: str,
    ) -> "bpy.types.NodeTree":
        import bpy
        from ..interactivity_nodes import OP_TO_BLIDNAME

        graph = self._graphs[graph_idx]
        tree = bpy.data.node_groups.new(
            f"{obj_name} Interactivity", TREE_BL_IDNAME,
        )

        # Pass 1: create nodes.
        graph_nodes = graph.get("nodes", [])
        bl_nodes: list = []
        for i, raw in enumerate(graph_nodes):
            op = self._op_for_decl(raw.get("declaration", -1))
            blidname = OP_TO_BLIDNAME.get(op)
            if blidname is None:
                bl_nodes.append(None)
                continue
            n = tree.nodes.new(blidname)
            n.location = (i * 220, 0)
            self._restore_configuration(n, raw)
            self._restore_value_defaults(n, raw)
            bl_nodes.append(n)

        # Pass 2: link flows and value references.
        for i, raw in enumerate(graph_nodes):
            src = bl_nodes[i]
            if src is None:
                continue
            self._restore_flows(tree, src, raw, bl_nodes)
            self._restore_value_links(tree, src, raw, bl_nodes)

        return tree

    def _restore_configuration(self, n, raw: dict) -> None:
        for cfg in raw.get("configuration", []):
            cid = cfg.get("id")
            val = cfg.get("value")
            if cid == "pointer" and isinstance(val, list) and val:
                n.pointer = str(val[0])
                # Default to CUSTOM until fixup_pointers resolves a structured form.
                if hasattr(n, "target_kind"):
                    n.target_kind = "CUSTOM"

    # ------- Pointer parsing (post-pass, after scene import) --------------

    def fixup_pointers(
        self,
        node_to_blender: "dict[int, bpy.types.Object]",
        material_importer=None,
    ) -> None:
        for tree in self._cached_trees.values():
            for n in tree.nodes:
                if n.bl_idname != "GLTFNode_pointer_set":
                    continue
                self._apply_parsed_pointer(n, node_to_blender, material_importer)

    def _apply_parsed_pointer(
        self,
        n,
        node_to_blender: "dict[int, bpy.types.Object]",
        material_importer,
    ) -> None:
        parsed = _parse_pointer(n.pointer)
        if parsed is None:
            n.target_kind = "CUSTOM"
            return
        kind, idx, prop, sub_idx = parsed
        if kind == "OBJECT":
            obj = node_to_blender.get(idx)
            if obj is None:
                n.target_kind = "CUSTOM"
                return
            n.target_kind = "OBJECT"
            n.target_object = obj
            n.object_property = prop
            if prop == "WEIGHT" and sub_idx is not None:
                n.weight_index = sub_idx
        elif kind == "MATERIAL":
            mat = None
            if material_importer is not None:
                mat = material_importer.get_blender_material(idx)
            if mat is None:
                n.target_kind = "CUSTOM"
                return
            n.target_kind = "MATERIAL"
            n.target_material = mat
            n.material_property = prop
        elif kind == "LIGHT":
            light = _find_light_by_index(self.gltf, node_to_blender, idx)
            if light is None:
                n.target_kind = "CUSTOM"
                return
            n.target_kind = "LIGHT"
            n.target_light = light
            n.light_property = prop
        elif kind == "CAMERA":
            cam = _find_camera_by_index(self.gltf, node_to_blender, idx)
            if cam is None:
                n.target_kind = "CUSTOM"
                return
            n.target_kind = "CAMERA"
            n.target_camera = cam
            n.camera_property = prop
        else:
            n.target_kind = "CUSTOM"

    def _restore_value_defaults(self, n, raw: dict) -> None:
        for v in raw.get("values", []):
            if "value" not in v:
                continue
            sock = n.inputs.get(v.get("id", ""))
            if sock is None or sock.bl_idname == FLOW_SOCKET_BL_IDNAME:
                continue
            payload = v["value"]
            if not isinstance(payload, list) or not payload:
                continue
            try:
                if sock.bl_idname == "NodeSocketBool":
                    sock.default_value = bool(payload[0])
                elif sock.bl_idname == "NodeSocketInt":
                    sock.default_value = int(payload[0])
                else:
                    sock.default_value = float(payload[0])
            except (TypeError, ValueError):
                pass

    def _restore_flows(self, tree, src_node, raw: dict, bl_nodes: list) -> None:
        for flow in raw.get("flows", []):
            out_id = flow.get("id")
            tgt_idx = flow.get("node")
            tgt_socket = flow.get("socket")
            if out_id is None or tgt_idx is None or tgt_socket is None:
                continue
            if not (0 <= tgt_idx < len(bl_nodes)) or bl_nodes[tgt_idx] is None:
                continue
            out_sock = src_node.outputs.get(out_id)
            in_sock = bl_nodes[tgt_idx].inputs.get(tgt_socket)
            if out_sock is None or in_sock is None:
                continue
            tree.links.new(out_sock, in_sock)

    def _restore_value_links(self, tree, dst_node, raw: dict, bl_nodes: list) -> None:
        for v in raw.get("values", []):
            if "node" not in v:
                continue
            src_idx = v["node"]
            src_socket = v.get("socket")
            if not (0 <= src_idx < len(bl_nodes)) or bl_nodes[src_idx] is None:
                continue
            in_sock = dst_node.inputs.get(v.get("id", ""))
            out_sock = bl_nodes[src_idx].outputs.get(src_socket)
            if in_sock is None or out_sock is None:
                continue
            tree.links.new(out_sock, in_sock)
