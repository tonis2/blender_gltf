"""The character clip list (3D viewport sidebar, glTF tab › Clips): the
actions a character's own .glb carries, and the file it came from, for the
selected armature or the armature of the selected mesh. The exporter reads
them in export/animation.py."""

import bpy
from bpy.props import CollectionProperty, IntProperty, PointerProperty, StringProperty


class GltfClip(bpy.types.PropertyGroup):
    """One action exported as an animation of the armature's own file."""

    action: PointerProperty(
        name="Action", type=bpy.types.Action,
        description="Exported as a glTF animation named after this action",
    )
    name: StringProperty(
        name="Name", description="Name in the file; empty uses the action's name",
    )


class DATA_UL_gltf_clips(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        row = layout.row(align=True)
        row.prop(item, "action", text="", icon="ACTION")
        row.prop(item, "name", text="")


def skin_armature(obj):
    """The armature a skinned mesh deforms with, or None."""
    for modifier in getattr(obj, "modifiers", ()):
        if modifier.type == "ARMATURE" and modifier.object is not None:
            return modifier.object
    return None


def clip_owner(obj):
    """The armature whose clip list this object shows: the armature itself,
    or the one a skinned mesh follows, since clicking a character in the
    viewport usually selects its mesh."""
    if obj is None:
        return None
    if obj.type == "ARMATURE":
        return obj
    owner = skin_armature(obj)
    if owner is None and obj.parent is not None and obj.parent.type == "ARMATURE":
        owner = obj.parent
    return owner


class OBJECT_OT_gltf_add_clip(bpy.types.Operator):
    """Add an action to this armature's glTF clip list"""

    bl_idname = "gltf_custom.add_clip"
    bl_label = "Add Clip"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return clip_owner(context.object) is not None

    def execute(self, context):
        obj = clip_owner(context.object)
        anim = obj.animation_data
        active = anim.action if anim else None
        # The new row starts with the active action unless it is listed already.
        listed = any(clip.action == active for clip in obj.gltf_clips)
        clip = obj.gltf_clips.add()
        if active is not None and not listed:
            clip.action = active
        obj.gltf_clips_index = len(obj.gltf_clips) - 1
        return {"FINISHED"}


class OBJECT_OT_gltf_remove_clip(bpy.types.Operator):
    """Remove the selected action from this armature's glTF clip list"""

    bl_idname = "gltf_custom.remove_clip"
    bl_label = "Remove Clip"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = clip_owner(context.object)
        return obj is not None and len(obj.gltf_clips) > 0

    def execute(self, context):
        obj = clip_owner(context.object)
        index = min(max(obj.gltf_clips_index, 0), len(obj.gltf_clips) - 1)
        obj.gltf_clips.remove(index)
        obj.gltf_clips_index = max(0, min(index, len(obj.gltf_clips) - 1))
        return {"FINISHED"}


class VIEW3D_PT_gltf_clips(bpy.types.Panel):
    """Panel in the 3D viewport sidebar for the selected character's glTF clip list."""

    bl_label = "Clips"
    bl_idname = "VIEW3D_PT_gltf_clips"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "glTF"

    @classmethod
    def poll(cls, context):
        return clip_owner(context.object) is not None

    def draw(self, context):
        layout = self.layout
        obj = clip_owner(context.object)
        layout.prop(obj, "gltf_clip_source")
        layout.label(text=obj.name, icon="OUTLINER_OB_ARMATURE")
        row = layout.row()
        row.template_list(
            "DATA_UL_gltf_clips", "", obj, "gltf_clips", obj, "gltf_clips_index", rows=3,
        )
        col = row.column(align=True)
        col.operator("gltf_custom.add_clip", icon="ADD", text="")
        col.operator("gltf_custom.remove_clip", icon="REMOVE", text="")
        layout.label(text="Exported with the armature, each as its own animation.")


_CLASSES = (
    GltfClip,
    DATA_UL_gltf_clips,
    OBJECT_OT_gltf_add_clip,
    OBJECT_OT_gltf_remove_clip,
    VIEW3D_PT_gltf_clips,
)


def _clip_source_property():
    kwargs = dict(
        name="Clips from", subtype="FILE_PATH",
        description="The character's own .glb; its animations are exported as they are",
    )
    try:  # newer Blender warns on a // path unless the property allows it
        return StringProperty(options={"PATH_SUPPORTS_BLEND_RELATIVE"}, **kwargs)
    except TypeError:
        return StringProperty(**kwargs)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Object.gltf_clips = CollectionProperty(type=GltfClip)
    bpy.types.Object.gltf_clips_index = IntProperty()
    bpy.types.Object.gltf_clip_source = _clip_source_property()


def unregister():
    del bpy.types.Object.gltf_clip_source
    del bpy.types.Object.gltf_clips_index
    del bpy.types.Object.gltf_clips
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
