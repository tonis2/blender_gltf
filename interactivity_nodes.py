"""Custom node tree, sockets, and node types for KHR_interactivity authoring.

A `GLTFInteractivityTree` is a per-object behavior graph the user builds in
Blender's node editor (a new "Interactivity" tab). The exporter walks the graph
and emits `KHR_interactivity` JSON; the importer rebuilds the graph from JSON.

The starter set of node operations is intentionally small:
    event/onStart, event/onTick,
    flow/sequence, flow/branch,
    math/add, math/eq,
    pointer/set, animation/start
"""
from __future__ import annotations

import bpy
from bpy.props import EnumProperty, IntProperty, PointerProperty, StringProperty


EXT_INTERACTIVITY = "KHR_interactivity"
TREE_BL_IDNAME = "GLTFInteractivityTreeType"
FLOW_SOCKET_BL_IDNAME = "GLTFFlowSocketType"


# Predefined type indices. The exporter writes a fixed `types` array; node
# value sockets reference these indices when emitting `values` entries.
TYPE_FLOAT = 0
TYPE_BOOL = 1
TYPE_INT = 2

PREDEFINED_TYPES = [
    {"signature": "float"},
    {"signature": "bool"},
    {"signature": "int"},
]


# Single source of truth for the enum-property → JSON-pointer mapping shared
# by the exporter (formats pointers with the resolved gltf index) and the
# importer (derives its inverse matcher from the same templates).
# Placeholders: {idx} = gltf array index, {sub} = sub-index (morph weight).
POINTER_TEMPLATES: dict[str, dict[str, str]] = {
    "OBJECT": {
        "TRANSLATION_X": "/nodes/{idx}/translation/0",
        "TRANSLATION_Y": "/nodes/{idx}/translation/1",
        "TRANSLATION_Z": "/nodes/{idx}/translation/2",
        "ROTATION_X":    "/nodes/{idx}/rotation/0",
        "ROTATION_Y":    "/nodes/{idx}/rotation/1",
        "ROTATION_Z":    "/nodes/{idx}/rotation/2",
        "ROTATION_W":    "/nodes/{idx}/rotation/3",
        "SCALE_X":       "/nodes/{idx}/scale/0",
        "SCALE_Y":       "/nodes/{idx}/scale/1",
        "SCALE_Z":       "/nodes/{idx}/scale/2",
        "WEIGHT":        "/nodes/{idx}/weights/{sub}",
        "VISIBLE":       "/nodes/{idx}/extensions/KHR_node_visibility/visible",
    },
    "MATERIAL": {
        "BASE_COLOR_R":      "/materials/{idx}/pbrMetallicRoughness/baseColorFactor/0",
        "BASE_COLOR_G":      "/materials/{idx}/pbrMetallicRoughness/baseColorFactor/1",
        "BASE_COLOR_B":      "/materials/{idx}/pbrMetallicRoughness/baseColorFactor/2",
        "BASE_COLOR_A":      "/materials/{idx}/pbrMetallicRoughness/baseColorFactor/3",
        "METALLIC":          "/materials/{idx}/pbrMetallicRoughness/metallicFactor",
        "ROUGHNESS":         "/materials/{idx}/pbrMetallicRoughness/roughnessFactor",
        "EMISSIVE_R":        "/materials/{idx}/emissiveFactor/0",
        "EMISSIVE_G":        "/materials/{idx}/emissiveFactor/1",
        "EMISSIVE_B":        "/materials/{idx}/emissiveFactor/2",
        "ALPHA_CUTOFF":      "/materials/{idx}/alphaCutoff",
        "EMISSIVE_STRENGTH": "/materials/{idx}/extensions/KHR_materials_emissive_strength/emissiveStrength",
    },
    "LIGHT": {
        "INTENSITY":  "/extensions/KHR_lights_punctual/lights/{idx}/intensity",
        "COLOR_R":    "/extensions/KHR_lights_punctual/lights/{idx}/color/0",
        "COLOR_G":    "/extensions/KHR_lights_punctual/lights/{idx}/color/1",
        "COLOR_B":    "/extensions/KHR_lights_punctual/lights/{idx}/color/2",
        "RANGE":      "/extensions/KHR_lights_punctual/lights/{idx}/range",
        "INNER_CONE": "/extensions/KHR_lights_punctual/lights/{idx}/spot/innerConeAngle",
        "OUTER_CONE": "/extensions/KHR_lights_punctual/lights/{idx}/spot/outerConeAngle",
    },
    "CAMERA": {
        "YFOV":  "/cameras/{idx}/perspective/yfov",
        "ZNEAR": "/cameras/{idx}/perspective/znear",
        "ZFAR":  "/cameras/{idx}/perspective/zfar",
        "XMAG":  "/cameras/{idx}/orthographic/xmag",
        "YMAG":  "/cameras/{idx}/orthographic/ymag",
    },
}


# ---------------------------------------------------------------------------
# Tree
# ---------------------------------------------------------------------------

class GLTFInteractivityTree(bpy.types.NodeTree):
    bl_idname = TREE_BL_IDNAME
    bl_label = "Interactivity"
    bl_icon = "NODETREE"


# ---------------------------------------------------------------------------
# Sockets
# ---------------------------------------------------------------------------

class GLTFFlowSocket(bpy.types.NodeSocket):
    """Control-flow socket (white). Connecting flow_out → flow_in chains nodes."""
    bl_idname = FLOW_SOCKET_BL_IDNAME
    bl_label = "Flow"

    def draw(self, context, layout, node, text):
        layout.label(text=text)

    def draw_color(self, context, node):
        return (1.0, 1.0, 1.0, 1.0)


# ---------------------------------------------------------------------------
# Node base
# ---------------------------------------------------------------------------

class GLTFInteractivityNodeBase:
    """Mixin for all interactivity nodes.

    Subclasses set:
        gltf_op: str          — the KHR_interactivity operation id
        bl_idname / bl_label  — Blender registration metadata
    and override init_sockets(self) to build their socket layout.
    """
    gltf_op: str = ""
    bl_icon = "NODE"

    @classmethod
    def poll(cls, ntree):
        return ntree.bl_idname == TREE_BL_IDNAME

    def init(self, context):
        self.init_sockets()

    def init_sockets(self):
        pass


# ---------------------------------------------------------------------------
# Concrete node types
# ---------------------------------------------------------------------------

class GLTFNodeOnStart(GLTFInteractivityNodeBase, bpy.types.Node):
    """Fires once when the graph starts."""
    bl_idname = "GLTFNode_event_onStart"
    bl_label = "On Start"
    gltf_op = "event/onStart"

    def init_sockets(self):
        self.outputs.new(FLOW_SOCKET_BL_IDNAME, "out")


class GLTFNodeOnTick(GLTFInteractivityNodeBase, bpy.types.Node):
    """Fires every frame; outputs delta time."""
    bl_idname = "GLTFNode_event_onTick"
    bl_label = "On Tick"
    gltf_op = "event/onTick"

    def init_sockets(self):
        self.outputs.new(FLOW_SOCKET_BL_IDNAME, "out")
        self.outputs.new("NodeSocketFloat", "timeSinceLastTick")


class GLTFNodeSequence(GLTFInteractivityNodeBase, bpy.types.Node):
    """Runs flow outputs 0, 1, 2 in order."""
    bl_idname = "GLTFNode_flow_sequence"
    bl_label = "Sequence"
    gltf_op = "flow/sequence"

    def init_sockets(self):
        self.inputs.new(FLOW_SOCKET_BL_IDNAME, "in")
        self.outputs.new(FLOW_SOCKET_BL_IDNAME, "0")
        self.outputs.new(FLOW_SOCKET_BL_IDNAME, "1")
        self.outputs.new(FLOW_SOCKET_BL_IDNAME, "2")


class GLTFNodeBranch(GLTFInteractivityNodeBase, bpy.types.Node):
    """If condition is true, fires `true` flow; otherwise `false`."""
    bl_idname = "GLTFNode_flow_branch"
    bl_label = "Branch"
    gltf_op = "flow/branch"

    def init_sockets(self):
        self.inputs.new(FLOW_SOCKET_BL_IDNAME, "in")
        self.inputs.new("NodeSocketBool", "condition")
        self.outputs.new(FLOW_SOCKET_BL_IDNAME, "true")
        self.outputs.new(FLOW_SOCKET_BL_IDNAME, "false")


class GLTFNodeMathAdd(GLTFInteractivityNodeBase, bpy.types.Node):
    """value = a + b."""
    bl_idname = "GLTFNode_math_add"
    bl_label = "Add"
    gltf_op = "math/add"

    def init_sockets(self):
        self.inputs.new("NodeSocketFloat", "a")
        self.inputs.new("NodeSocketFloat", "b")
        self.outputs.new("NodeSocketFloat", "value")


class GLTFNodeMathEq(GLTFInteractivityNodeBase, bpy.types.Node):
    """value = (a == b)."""
    bl_idname = "GLTFNode_math_eq"
    bl_label = "Equal"
    gltf_op = "math/eq"

    def init_sockets(self):
        self.inputs.new("NodeSocketFloat", "a")
        self.inputs.new("NodeSocketFloat", "b")
        self.outputs.new("NodeSocketBool", "value")


_OBJECT_PROPS = [
    ("TRANSLATION_X", "Translation X", ""),
    ("TRANSLATION_Y", "Translation Y", ""),
    ("TRANSLATION_Z", "Translation Z", ""),
    ("ROTATION_X",    "Rotation X",    ""),
    ("ROTATION_Y",    "Rotation Y",    ""),
    ("ROTATION_Z",    "Rotation Z",    ""),
    ("ROTATION_W",    "Rotation W",    ""),
    ("SCALE_X",       "Scale X",       ""),
    ("SCALE_Y",       "Scale Y",       ""),
    ("SCALE_Z",       "Scale Z",       ""),
    ("WEIGHT",        "Morph Weight",  "Per-target morph weight (uses Index)"),
    ("VISIBLE",       "Visibility",    "KHR_node_visibility flag (boolean)"),
]

_MATERIAL_PROPS = [
    ("BASE_COLOR_R",      "Base Color R",      ""),
    ("BASE_COLOR_G",      "Base Color G",      ""),
    ("BASE_COLOR_B",      "Base Color B",      ""),
    ("BASE_COLOR_A",      "Base Color A",      ""),
    ("METALLIC",          "Metallic",          ""),
    ("ROUGHNESS",         "Roughness",         ""),
    ("EMISSIVE_R",        "Emissive R",        ""),
    ("EMISSIVE_G",        "Emissive G",        ""),
    ("EMISSIVE_B",        "Emissive B",        ""),
    ("ALPHA_CUTOFF",      "Alpha Cutoff",      ""),
    ("EMISSIVE_STRENGTH", "Emissive Strength", "KHR_materials_emissive_strength"),
]

_LIGHT_PROPS = [
    ("INTENSITY",  "Intensity",         ""),
    ("COLOR_R",    "Color R",           ""),
    ("COLOR_G",    "Color G",           ""),
    ("COLOR_B",    "Color B",           ""),
    ("RANGE",      "Range",             ""),
    ("INNER_CONE", "Inner Cone Angle",  "Spot only"),
    ("OUTER_CONE", "Outer Cone Angle",  "Spot only"),
]

_CAMERA_PROPS = [
    ("YFOV",  "Vertical FOV", "Perspective only"),
    ("ZNEAR", "Near Plane",   ""),
    ("ZFAR",  "Far Plane",    ""),
    ("XMAG",  "Ortho X Mag",  "Orthographic only"),
    ("YMAG",  "Ortho Y Mag",  "Orthographic only"),
]

_TARGET_KINDS = [
    ("OBJECT",   "Object",   "Write to a node's TRS / weights / visibility"),
    ("MATERIAL", "Material", "Write to a material factor"),
    ("LIGHT",    "Light",    "Write to a KHR_lights_punctual property"),
    ("CAMERA",   "Camera",   "Write to a camera property"),
    ("CUSTOM",   "Custom",   "Use a raw JSON pointer string"),
]


class GLTFNodePointerSet(GLTFInteractivityNodeBase, bpy.types.Node):
    """Writes `value` into the JSON pointer (e.g. /nodes/3/translation/0).

    The user picks a Blender datablock + property; the exporter resolves
    the gltf index at export time and emits a JSON pointer string. The
    raw `pointer` field is used in CUSTOM mode and as a fallback when
    resolution fails (e.g. the picked object isn't being exported).
    """
    bl_idname = "GLTFNode_pointer_set"
    bl_label = "Pointer Set"
    gltf_op = "pointer/set"

    target_kind: EnumProperty(
        name="Target",
        items=_TARGET_KINDS,
        default="OBJECT",
    )

    target_object:   PointerProperty(name="Object",   type=bpy.types.Object)
    target_material: PointerProperty(name="Material", type=bpy.types.Material)
    target_light:    PointerProperty(name="Light",    type=bpy.types.Light)
    target_camera:   PointerProperty(name="Camera",   type=bpy.types.Camera)

    object_property:   EnumProperty(name="Property", items=_OBJECT_PROPS,   default="TRANSLATION_X")
    material_property: EnumProperty(name="Property", items=_MATERIAL_PROPS, default="BASE_COLOR_R")
    light_property:    EnumProperty(name="Property", items=_LIGHT_PROPS,    default="INTENSITY")
    camera_property:   EnumProperty(name="Property", items=_CAMERA_PROPS,   default="YFOV")

    weight_index: IntProperty(name="Index", default=0, min=0)

    pointer: StringProperty(
        name="Pointer",
        description="Raw JSON pointer (used in Custom mode and as fallback)",
        default="/nodes/0/translation/0",
    )

    def init_sockets(self):
        self.inputs.new(FLOW_SOCKET_BL_IDNAME, "in")
        self.inputs.new("NodeSocketFloat", "value")
        self.outputs.new(FLOW_SOCKET_BL_IDNAME, "out")
        self.outputs.new(FLOW_SOCKET_BL_IDNAME, "err")

    def draw_buttons(self, context, layout):
        layout.prop(self, "target_kind", text="")
        kind = self.target_kind
        if kind == "OBJECT":
            layout.prop(self, "target_object", text="")
            layout.prop(self, "object_property", text="")
            if self.object_property == "WEIGHT":
                layout.prop(self, "weight_index")
        elif kind == "MATERIAL":
            layout.prop(self, "target_material", text="")
            layout.prop(self, "material_property", text="")
        elif kind == "LIGHT":
            layout.prop(self, "target_light", text="")
            layout.prop(self, "light_property", text="")
        elif kind == "CAMERA":
            layout.prop(self, "target_camera", text="")
            layout.prop(self, "camera_property", text="")
        else:  # CUSTOM
            layout.prop(self, "pointer", text="")


class GLTFNodeAnimationStart(GLTFInteractivityNodeBase, bpy.types.Node):
    """Starts the glTF animation at index `animation`."""
    bl_idname = "GLTFNode_animation_start"
    bl_label = "Animation Start"
    gltf_op = "animation/start"

    def init_sockets(self):
        self.inputs.new(FLOW_SOCKET_BL_IDNAME, "in")
        self.inputs.new("NodeSocketInt", "animation")
        self.outputs.new(FLOW_SOCKET_BL_IDNAME, "out")
        self.outputs.new(FLOW_SOCKET_BL_IDNAME, "err")


INTERACTIVITY_NODE_CLASSES = (
    GLTFNodeOnStart,
    GLTFNodeOnTick,
    GLTFNodeSequence,
    GLTFNodeBranch,
    GLTFNodeMathAdd,
    GLTFNodeMathEq,
    GLTFNodePointerSet,
    GLTFNodeAnimationStart,
)


# Op id <-> Blender node class. Rebuilt at register time so reload picks up
# any new entries.
OP_TO_BLIDNAME: dict[str, str] = {}
BLIDNAME_TO_CLASS: dict[str, type] = {}


def _rebuild_node_lookups():
    OP_TO_BLIDNAME.clear()
    BLIDNAME_TO_CLASS.clear()
    for cls in INTERACTIVITY_NODE_CLASSES:
        OP_TO_BLIDNAME[cls.gltf_op] = cls.bl_idname
        BLIDNAME_TO_CLASS[cls.bl_idname] = cls


# ---------------------------------------------------------------------------
# Add menu
# ---------------------------------------------------------------------------

class NODE_MT_gltf_interactivity_add(bpy.types.Menu):
    bl_idname = "NODE_MT_gltf_interactivity_add"
    bl_label = "Interactivity"

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return (
            space is not None
            and space.type == "NODE_EDITOR"
            and getattr(space, "tree_type", "") == TREE_BL_IDNAME
        )

    def draw(self, context):
        layout = self.layout
        groups = [
            ("Events",  ("GLTFNode_event_onStart", "GLTFNode_event_onTick")),
            ("Flow",    ("GLTFNode_flow_sequence", "GLTFNode_flow_branch")),
            ("Math",    ("GLTFNode_math_add", "GLTFNode_math_eq")),
            ("Actions", ("GLTFNode_pointer_set", "GLTFNode_animation_start")),
        ]
        for label, blidnames in groups:
            layout.label(text=label)
            for blidname in blidnames:
                cls = BLIDNAME_TO_CLASS[blidname]
                op = layout.operator("node.add_node", text=cls.bl_label)
                op.type = blidname
                op.use_transform = True
            layout.separator()


def menu_func_node_add(self, context):
    space = context.space_data
    if space is None or getattr(space, "tree_type", "") != TREE_BL_IDNAME:
        return
    self.layout.menu(NODE_MT_gltf_interactivity_add.bl_idname)


# ---------------------------------------------------------------------------
# Object panel: assign / pick a graph for the active object
# ---------------------------------------------------------------------------

def _interactivity_tree_poll(self, ntree):
    return ntree.bl_idname == TREE_BL_IDNAME


class OBJECT_OT_gltf_interactivity_new(bpy.types.Operator):
    """Create a new Interactivity graph and assign it to the active object."""
    bl_idname = "object.gltf_interactivity_new"
    bl_label = "New Interactivity Graph"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def execute(self, context):
        obj = context.active_object
        tree = bpy.data.node_groups.new(
            f"{obj.name} Interactivity", TREE_BL_IDNAME,
        )
        # Seed with an On Start node so the editor view has something to
        # anchor on — empty node trees default to an extreme zoom.
        seed = tree.nodes.new("GLTFNode_event_onStart")
        seed.location = (0, 0)
        tree["gltf_seeded"] = True
        obj.gltf_interactivity = tree

        # Frame any open Interactivity editors on the new tree.
        for area in context.screen.areas:
            if area.type != "NODE_EDITOR":
                continue
            if getattr(area.spaces.active, "tree_type", "") != TREE_BL_IDNAME:
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region is None:
                continue
            try:
                with context.temp_override(area=area, region=region, space_data=area.spaces.active):
                    bpy.ops.node.view_all()
            except RuntimeError:
                pass
        return {"FINISHED"}


class OBJECT_PT_gltf_interactivity(bpy.types.Panel):
    bl_label = "glTF Interactivity"
    bl_idname = "OBJECT_PT_gltf_interactivity"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        row = layout.row(align=True)
        row.prop(obj, "gltf_interactivity", text="Graph")
        row.operator(
            OBJECT_OT_gltf_interactivity_new.bl_idname, text="", icon="ADD",
        )


# ---------------------------------------------------------------------------
# Auto-seed empty trees
# ---------------------------------------------------------------------------
# Blender's NodeTree.init() isn't reliably fired across creation paths
# (data API, ops API, editor "+ New" button), so we seed via a depsgraph
# handler instead. The check is cheap: skip unless an empty Interactivity
# tree exists.

@bpy.app.handlers.persistent
def _seed_empty_interactivity_trees(scene, depsgraph=None):
    for ng in bpy.data.node_groups:
        if ng.bl_idname != TREE_BL_IDNAME:
            continue
        if ng.nodes:
            continue
        # Seed each tree at most once: a tree the user deliberately emptied
        # must not have an "On Start" node resurrected on the next update.
        if ng.get("gltf_seeded"):
            continue
        seed = ng.nodes.new("GLTFNode_event_onStart")
        seed.location = (0, 0)
        ng["gltf_seeded"] = True


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_REGISTRATION_ORDER = (
    GLTFInteractivityTree,
    GLTFFlowSocket,
    *INTERACTIVITY_NODE_CLASSES,
    NODE_MT_gltf_interactivity_add,
    OBJECT_OT_gltf_interactivity_new,
    OBJECT_PT_gltf_interactivity,
)


def register() -> None:
    for cls in _REGISTRATION_ORDER:
        bpy.utils.register_class(cls)

    _rebuild_node_lookups()

    bpy.types.Object.gltf_interactivity = PointerProperty(
        type=bpy.types.NodeTree,
        name="glTF Interactivity",
        description="Behavior graph attached to this object for KHR_interactivity export",
        poll=_interactivity_tree_poll,
    )
    bpy.types.NODE_MT_add.append(menu_func_node_add)
    if _seed_empty_interactivity_trees not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_seed_empty_interactivity_trees)


def unregister() -> None:
    if _seed_empty_interactivity_trees in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_seed_empty_interactivity_trees)
    bpy.types.NODE_MT_add.remove(menu_func_node_add)
    del bpy.types.Object.gltf_interactivity
    for cls in reversed(_REGISTRATION_ORDER):
        bpy.utils.unregister_class(cls)
