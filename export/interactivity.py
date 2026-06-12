"""KHR_interactivity exporter.

Walks each object's `gltf_interactivity` NodeTree and emits a graph in the
root-level `KHR_interactivity` extension. The per-object node carries a
reference `{ "graph": <index> }` into that graphs array.

Layout we emit (loosely following the KHR_interactivity draft):

    root.extensions.KHR_interactivity = {
        "types":        [ {"signature": "float"}, {"signature": "bool"}, ... ],
        "declarations": [ {"op": "math/add"}, {"op": "flow/branch"}, ... ],
        "graphs":       [ { "nodes": [ ... ] }, ... ]
    }

    nodes[i] = {
        "declaration": <decl index>,
        "configuration": [ {"id": "pointer", "value": [<str>]} ],
        "values":        [ {"id": "a", "value": [1.0], "type": 0}
                           or {"id": "a", "node": <srcIdx>, "socket": "value"} ],
        "flows":         [ {"id": "out", "node": <tgtIdx>, "socket": "in"} ],
    }
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..interactivity_nodes import (
    EXT_INTERACTIVITY,
    FLOW_SOCKET_BL_IDNAME,
    POINTER_TEMPLATES,
    PREDEFINED_TYPES,
    TREE_BL_IDNAME,
    TYPE_BOOL,
    TYPE_FLOAT,
    TYPE_INT,
)

if TYPE_CHECKING:
    import bpy
    from ..exporter import ExportSettings


# Map a Blender socket bl_idname to (typeIndex, value-coercer).
_SOCKET_TYPE: dict[str, tuple[int, callable]] = {
    "NodeSocketFloat":      (TYPE_FLOAT, lambda v: [float(v)]),
    "NodeSocketFloatFactor":(TYPE_FLOAT, lambda v: [float(v)]),
    "NodeSocketBool":       (TYPE_BOOL,  lambda v: [bool(v)]),
    "NodeSocketInt":        (TYPE_INT,   lambda v: [int(v)]),
}


class InteractivityExporter:
    def __init__(self, settings: "ExportSettings") -> None:
        self.settings = settings
        self.extensions_used: set[str] = set()
        self._graphs: list[dict | None] = []
        # Dedup: same NodeTree → reuse the graph index across objects.
        self._tree_to_index: dict[str, int] = {}
        self._declarations: list[dict] = []
        self._op_to_decl: dict[str, int] = {}
        # Graph build is deferred until finalize() so pointer/set picks can
        # resolve objects that may be exported after this graph's owning
        # object.
        self._pending: list[tuple[int, "bpy.types.NodeTree"]] = []
        self.scene_exporter = None       # set by GltfExporter post-construction
        self.material_exporter = None    # set by GltfExporter post-construction

    # ------- Per-object hook used by the scene exporter -------------------

    def gather_node(self, obj: "bpy.types.Object") -> dict | None:
        tree = getattr(obj, "gltf_interactivity", None)
        if tree is None or tree.bl_idname != TREE_BL_IDNAME:
            return None
        if not tree.nodes:
            return None

        graph_idx = self._tree_to_index.get(tree.name)
        if graph_idx is None:
            graph_idx = len(self._graphs)
            self._tree_to_index[tree.name] = graph_idx
            self._graphs.append(None)
            self._pending.append((graph_idx, tree))

        self.extensions_used.add(EXT_INTERACTIVITY)
        return {EXT_INTERACTIVITY: {"graph": graph_idx}}

    # ------- Root extension assembly --------------------------------------

    def finalize(self) -> None:
        for graph_idx, tree in self._pending:
            self._graphs[graph_idx] = self._build_graph(tree)
        self._pending = []

    def get_root_extension(self) -> dict | None:
        self.finalize()
        if not self._graphs:
            return None
        return {
            EXT_INTERACTIVITY: {
                "types":        list(PREDEFINED_TYPES),
                "declarations": list(self._declarations),
                "graphs":       self._graphs,
            }
        }

    # ------- Graph build --------------------------------------------------

    def _decl_for_op(self, op: str) -> int:
        idx = self._op_to_decl.get(op)
        if idx is None:
            idx = len(self._declarations)
            self._declarations.append({"op": op})
            self._op_to_decl[op] = idx
        return idx

    def _build_graph(self, tree) -> dict:
        # Stable ordering: by node name, since Blender doesn't preserve
        # creation order across edits reliably.
        nodes = sorted(tree.nodes, key=lambda n: n.name)
        node_index = {n.name: i for i, n in enumerate(nodes)}

        return {"nodes": [self._serialize_node(n, node_index) for n in nodes]}

    def _serialize_node(self, n, node_index: dict[str, int]) -> dict:
        op = getattr(n, "gltf_op", "")
        out: dict = {"declaration": self._decl_for_op(op)}

        config = self._serialize_configuration(n)
        if config:
            out["configuration"] = config

        values = self._serialize_values(n, node_index)
        if values:
            out["values"] = values

        flows = self._serialize_flows(n, node_index)
        if flows:
            out["flows"] = flows

        return out

    def _serialize_configuration(self, n) -> list[dict]:
        config: list[dict] = []
        if n.bl_idname == "GLTFNode_pointer_set":
            ptr = self._resolve_pointer(n) or n.pointer
            config.append({"id": "pointer", "value": [ptr]})
        return config

    # ------- Pointer resolution -------------------------------------------

    _OBJ_TRS = {
        "TRANSLATION_X": ("translation", 0), "TRANSLATION_Y": ("translation", 1),
        "TRANSLATION_Z": ("translation", 2),
        "ROTATION_X":    ("rotation", 0),    "ROTATION_Y":    ("rotation", 1),
        "ROTATION_Z":    ("rotation", 2),    "ROTATION_W":    ("rotation", 3),
        "SCALE_X":       ("scale", 0),       "SCALE_Y":       ("scale", 1),
        "SCALE_Z":       ("scale", 2),
    }
    _MAT_BASE_COLOR = {
        "BASE_COLOR_R": 0, "BASE_COLOR_G": 1, "BASE_COLOR_B": 2, "BASE_COLOR_A": 3,
    }
    _MAT_EMISSIVE = {"EMISSIVE_R": 0, "EMISSIVE_G": 1, "EMISSIVE_B": 2}
    _LIGHT_COLOR = {"COLOR_R": 0, "COLOR_G": 1, "COLOR_B": 2}

    def _resolve_pointer(self, n) -> str | None:
        kind = n.target_kind
        if kind == "CUSTOM":
            return n.pointer
        if kind == "OBJECT":
            return self._resolve_object_pointer(n)
        if kind == "MATERIAL":
            return self._resolve_material_pointer(n)
        if kind == "LIGHT":
            return self._resolve_light_pointer(n)
        if kind == "CAMERA":
            return self._resolve_camera_pointer(n)
        return None

    def _resolve_object_pointer(self, n) -> str | None:
        if self.scene_exporter is None or n.target_object is None:
            return None
        idx = self.scene_exporter.object_to_node_index.get(n.target_object.name)
        if idx is None:
            return None
        prop = n.object_property
        trs = self._OBJ_TRS.get(prop)
        if trs is not None:
            field, comp = trs
            return f"/nodes/{idx}/{field}/{comp}"
        if prop == "WEIGHT":
            return f"/nodes/{idx}/weights/{n.weight_index}"
        if prop == "VISIBLE":
            return f"/nodes/{idx}/extensions/KHR_node_visibility/visible"
        return None

    def _resolve_material_pointer(self, n) -> str | None:
        if self.material_exporter is None or n.target_material is None:
            return None
        idx = self.material_exporter.material_index_by_name.get(n.target_material.name)
        if idx is None:
            return None
        prop = n.material_property
        if prop in self._MAT_BASE_COLOR:
            return f"/materials/{idx}/pbrMetallicRoughness/baseColorFactor/{self._MAT_BASE_COLOR[prop]}"
        if prop == "METALLIC":
            return f"/materials/{idx}/pbrMetallicRoughness/metallicFactor"
        if prop == "ROUGHNESS":
            return f"/materials/{idx}/pbrMetallicRoughness/roughnessFactor"
        if prop in self._MAT_EMISSIVE:
            return f"/materials/{idx}/emissiveFactor/{self._MAT_EMISSIVE[prop]}"
        if prop == "ALPHA_CUTOFF":
            return f"/materials/{idx}/alphaCutoff"
        if prop == "EMISSIVE_STRENGTH":
            return f"/materials/{idx}/extensions/KHR_materials_emissive_strength/emissiveStrength"
        return None

    def _resolve_light_pointer(self, n) -> str | None:
        if self.scene_exporter is None or n.target_light is None:
            return None
        idx = self.scene_exporter.light_index_by_name.get(n.target_light.name)
        if idx is None:
            return None
        prop = n.light_property
        base = f"/extensions/KHR_lights_punctual/lights/{idx}"
        if prop == "INTENSITY":
            return f"{base}/intensity"
        if prop in self._LIGHT_COLOR:
            return f"{base}/color/{self._LIGHT_COLOR[prop]}"
        if prop == "RANGE":
            return f"{base}/range"
        if prop == "INNER_CONE":
            return f"{base}/spot/innerConeAngle"
        if prop == "OUTER_CONE":
            return f"{base}/spot/outerConeAngle"
        return None

    def _resolve_camera_pointer(self, n) -> str | None:
        if self.scene_exporter is None or n.target_camera is None:
            return None
        idx = self.scene_exporter.camera_index_by_name.get(n.target_camera.name)
        if idx is None:
            return None
        prop = n.camera_property
        if prop == "YFOV":
            return f"/cameras/{idx}/perspective/yfov"
        if prop == "ZNEAR":
            return f"/cameras/{idx}/perspective/znear"
        if prop == "ZFAR":
            return f"/cameras/{idx}/perspective/zfar"
        if prop == "XMAG":
            return f"/cameras/{idx}/orthographic/xmag"
        if prop == "YMAG":
            return f"/cameras/{idx}/orthographic/ymag"
        return None

    def _serialize_values(self, n, node_index: dict[str, int]) -> list[dict]:
        values: list[dict] = []
        for sock in n.inputs:
            if sock.bl_idname == FLOW_SOCKET_BL_IDNAME:
                continue
            entry: dict = {"id": sock.name}
            if sock.is_linked:
                link = sock.links[0]
                entry["node"] = node_index[link.from_node.name]
                entry["socket"] = link.from_socket.name
            else:
                type_info = _SOCKET_TYPE.get(sock.bl_idname)
                if type_info is None:
                    continue
                type_idx, coerce = type_info
                entry["type"] = type_idx
                entry["value"] = coerce(sock.default_value)
            values.append(entry)
        return values

    def _serialize_flows(self, n, node_index: dict[str, int]) -> list[dict]:
        flows: list[dict] = []
        for sock in n.outputs:
            if sock.bl_idname != FLOW_SOCKET_BL_IDNAME:
                continue
            if not sock.is_linked:
                continue
            link = sock.links[0]
            flows.append({
                "id":     sock.name,
                "node":   node_index[link.to_node.name],
                "socket": link.to_socket.name,
            })
        return flows
