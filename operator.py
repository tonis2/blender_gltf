import bpy
from bpy.props import EnumProperty, BoolProperty, StringProperty, FloatProperty, FloatVectorProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper

from .exporter import ExportSettings, GltfExporter
from .importer import ImportSettings, GltfImporter


COMBINE_MODES = [
    ("AVERAGE", "Average", "Average of the two values"),
    ("MINIMUM", "Minimum", "Smaller of the two values"),
    ("MAXIMUM", "Maximum", "Larger of the two values"),
    ("MULTIPLY", "Multiply", "Product of the two values"),
]


class KHR_PhysicsProperties(bpy.types.PropertyGroup):
    """Custom properties for KHR_physics_rigid_bodies export."""

    # Motion
    linear_velocity: FloatVectorProperty(
        name="Linear Velocity",
        size=3,
        default=(0.0, 0.0, 0.0),
    )
    angular_velocity: FloatVectorProperty(
        name="Angular Velocity",
        size=3,
        default=(0.0, 0.0, 0.0),
    )
    gravity_factor: FloatProperty(
        name="Gravity Factor",
        default=1.0,
    )

    # Collisions
    is_trigger: BoolProperty(
        name="Is Trigger",
        description="Export as a trigger volume instead of a solid collider",
        default=False,
    )
    friction_combine: EnumProperty(
        name="Friction Combine mode",
        items=COMBINE_MODES,
        default="AVERAGE",
    )
    restitution_combine: EnumProperty(
        name="Restitution Combine mode",
        items=COMBINE_MODES,
        default="AVERAGE",
    )


class GltfAudioProperties(bpy.types.PropertyGroup):
    """KHR_audio_emitter source properties without a Blender-native equivalent."""

    auto_play: BoolProperty(
        name="Auto Play",
        description="Start playback as soon as the scene loads",
        default=True,
    )
    loop: BoolProperty(
        name="Loop",
        description="Loop the audio source",
        default=True,
    )


class DATA_PT_gltf_audio(bpy.types.Panel):
    """Panel in speaker data properties for glTF audio settings."""
    bl_label = "glTF Audio"
    bl_idname = "DATA_PT_gltf_audio"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "data"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == "SPEAKER"

    def draw(self, context):
        layout = self.layout
        props = context.active_object.data.gltf_audio
        layout.prop(props, "auto_play")
        layout.prop(props, "loop")


class GltfMaterialProperties(bpy.types.PropertyGroup):
    """glTF material extension properties."""

    unlit: BoolProperty(
        name="Unlit",
        description="Export as KHR_materials_unlit (no lighting applied)",
        default=False,
    )


class MATERIAL_PT_gltf_properties(bpy.types.Panel):
    """Panel in material properties for glTF extension settings."""
    bl_label = "glTF Extensions"
    bl_idname = "MATERIAL_PT_gltf_properties"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "material"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.active_material is not None

    def draw(self, context):
        layout = self.layout
        props = context.active_object.active_material.gltf_props
        layout.prop(props, "unlit")


class PHYSICS_PT_khr_physics(bpy.types.Panel):
    """Panel in the physics properties for KHR physics extension settings."""
    bl_label = "KHR Physics Extensions"
    bl_idname = "PHYSICS_PT_khr_physics"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "physics"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.rigid_body is not None

    def draw(self, context):
        layout = self.layout
        props = context.active_object.khr_physics

        # Motion
        box = layout.box()
        box.label(text="Motion")
        col = box.column()
        col.prop(props, "linear_velocity")
        col.prop(props, "angular_velocity")
        col.prop(props, "gravity_factor")

        # Collisions
        box = layout.box()
        box.label(text="Collisions")
        col = box.column()
        col.prop(props, "is_trigger")
        col.prop(props, "friction_combine")
        col.prop(props, "restitution_combine")


# Single source of truth for every export option shared between the operator
# and the per-scene settings group. bpy property definitions are one-shot
# descriptors, so each class gets fresh instances built from these
# (constructor, kwargs) pairs — see the __annotations__ injection below.
_EXPORT_PROP_DEFS: dict[str, tuple] = {
    "export_format": (EnumProperty, dict(
        name="Format",
        items=[
            ("GLB", "glTF Binary (.glb)", "Export as a single binary file"),
            ("GLTF_SEPARATE", "glTF Separate (.gltf + .bin)", "Export as separate JSON and binary files"),
            ("GLTF_EMBEDDED", "glTF Embedded (.gltf)", "Export as a single .gltf with binary data embedded as base64"),
        ],
        default="GLB",
    )),
    "export_normals": (BoolProperty, dict(
        name="Normals",
        description="Export vertex normals",
        default=True,
    )),
    "export_texcoords": (BoolProperty, dict(
        name="UVs",
        description="Export UV coordinates",
        default=True,
    )),
    "export_tangents": (BoolProperty, dict(
        name="Tangents",
        description="Export MikkTSpace vertex tangents (requires Normals and UVs)",
        default=False,
    )),
    "export_quantization": (BoolProperty, dict(
        name="Quantize Attributes",
        description=(
            "Compress vertex data with KHR_mesh_quantization (16-bit "
            "positions/UVs, 8-bit normals/tangents). Smaller files and "
            "less GPU memory; slight precision loss on very glossy surfaces"
        ),
        default=False,
    )),
    "export_materials": (BoolProperty, dict(
        name="Materials",
        description="Export PBR materials",
        default=True,
    )),
    "export_colors": (BoolProperty, dict(
        name="Vertex Colors",
        description="Export vertex colors",
        default=True,
    )),
    "export_animations": (BoolProperty, dict(
        name="Animations",
        description="Export keyframe animations",
        default=True,
    )),
    "export_animation_events": (BoolProperty, dict(
        name="Animation Events",
        description="Export action pose markers as timed events (CUSTOM_animation_events)",
        default=True,
    )),
    "export_morph_targets": (BoolProperty, dict(
        name="Shape Keys",
        description="Export shape keys as morph targets",
        default=True,
    )),
    "export_gpu_instancing": (BoolProperty, dict(
        name="GPU Instancing",
        description="Export collection instances using EXT_mesh_gpu_instancing",
        default=True,
    )),
    "export_lods": (BoolProperty, dict(
        name="LODs (MSFT_lod)",
        description=(
            "Group sibling objects named Base_LOD0, Base_LOD1, ... into "
            "MSFT_lod levels of detail on the LOD0 node"
        ),
        default=True,
    )),
    "export_skinning": (BoolProperty, dict(
        name="Skinning",
        description="Export armatures and bone weights",
        default=True,
    )),
    "export_physics": (BoolProperty, dict(
        name="Physics",
        description="Export rigid bodies and collision shapes",
        default=True,
    )),
    "export_extras": (BoolProperty, dict(
        name="Custom Properties",
        description="Export object custom properties as glTF node extras",
        default=True,
    )),
    "export_particles": (BoolProperty, dict(
        name="Particles",
        description="Export particle system settings as CUSTOM_particle_emitter",
        default=True,
    )),
    "export_interactivity": (BoolProperty, dict(
        name="Interactivity",
        description="Export per-object behavior graphs as KHR_interactivity",
        default=True,
    )),
    "export_audio": (BoolProperty, dict(
        name="Audio",
        description="Export speaker objects as KHR_audio_emitter",
        default=True,
    )),
    "export_only_visible": (BoolProperty, dict(
        name="Only Visible",
        description="Only export objects that are visible in the viewport",
        default=False,
    )),
    "export_all_scenes": (BoolProperty, dict(
        name="All Scenes",
        description="Export all Blender scenes into a single glTF file",
        default=False,
    )),
    "export_camera_y_up": (BoolProperty, dict(
        name="Cameras Y-Up",
        description=(
            "Apply a -90° X-axis fix-up to camera nodes so their forward "
            "direction matches the glTF Y-up convention. Disable for tools "
            "that expect Blender's raw camera orientation"
        ),
        default=True,
    )),
    "image_format": (EnumProperty, dict(
        name="Image Format",
        description="Format for exported textures",
        items=[
            ("AUTO", "Auto", "Keep the original image format"),
            ("JPEG", "JPEG", "Export all textures as JPEG"),
            ("PNG", "PNG", "Export all textures as PNG"),
        ],
        default="AUTO",
    )),
}

_EXPORT_PROPS = tuple(_EXPORT_PROP_DEFS)


def _make_export_props() -> dict:
    """Build fresh property instances (definitions must not be shared across classes)."""
    return {name: ctor(**kwargs) for name, (ctor, kwargs) in _EXPORT_PROP_DEFS.items()}


# Collapsible panel layout for the export/import file browser sidebars:
# (panel_id, label, prop_names). Panel ids persist open/closed state.
_EXPORT_PANELS = (
    ("GLTF_export_mesh", "Mesh", (
        "export_normals",
        "export_texcoords",
        "export_tangents",
        "export_colors",
        "export_quantization",
    )),
    ("GLTF_export_material", "Material", ("export_materials", "image_format")),
    ("GLTF_export_animation", "Animation", (
        "export_animations",
        "export_animation_events",
        "export_morph_targets",
    )),
    ("GLTF_export_skinning", "Skinning", ("export_skinning",)),
    ("GLTF_export_instancing", "Instancing", ("export_gpu_instancing",)),
    ("GLTF_export_lod", "LOD", ("export_lods",)),
    ("GLTF_export_cameras", "Cameras", ("export_camera_y_up",)),
    ("GLTF_export_physics", "Physics", ("export_physics",)),
    ("GLTF_export_particles", "Particles", ("export_particles",)),
    ("GLTF_export_interactivity", "Interactivity", ("export_interactivity",)),
    ("GLTF_export_audio", "Audio", ("export_audio",)),
    ("GLTF_export_extras", "Extras", ("export_extras",)),
)

_IMPORT_PANELS = (
    ("GLTF_import_mesh", "Mesh", ("import_normals", "import_texcoords", "import_colors")),
    ("GLTF_import_material", "Material", ("import_materials",)),
    ("GLTF_import_animation", "Animation", ("import_animations", "import_morph_targets")),
    ("GLTF_import_skinning", "Skinning", ("import_skinning",)),
    ("GLTF_import_physics", "Physics", ("import_physics",)),
    ("GLTF_import_particles", "Particles", ("import_particles",)),
    ("GLTF_import_interactivity", "Interactivity", ("import_interactivity",)),
    ("GLTF_import_audio", "Audio", ("import_audio",)),
)


def _draw_panels(layout, owner, panels):
    """Draw collapsible panels from a (panel_id, label, prop_names) table."""
    for panel_id, label, prop_names in panels:
        header, body = layout.panel(panel_id, default_closed=True)
        header.label(text=label)
        if body:
            for prop in prop_names:
                body.prop(owner, prop)


class GltfExportSceneSettings(bpy.types.PropertyGroup):
    """Export settings stored per scene, persisted with the .blend file.

    Export properties are injected from _EXPORT_PROP_DEFS below.
    """


class EXPORT_SCENE_OT_gltf(bpy.types.Operator, ExportHelper):
    """Export scene as glTF 2.0"""
    bl_idname = "export_scene.gltf_custom"
    bl_label = "Export glTF 2.0"
    bl_options = {"PRESET"}

    filename_ext = ".glb"

    filter_glob: StringProperty(
        default="*.glb;*.gltf",
        options={"HIDDEN"},
    )

    # Export properties are injected from _EXPORT_PROP_DEFS below.

    def invoke(self, context, event):
        # Load saved settings from the scene
        saved = context.scene.gltf_export_settings
        for prop in _EXPORT_PROPS:
            setattr(self, prop, getattr(saved, prop))
        return super().invoke(context, event)

    def execute(self, context):
        # Save settings back to the scene so they persist with the .blend
        saved = context.scene.gltf_export_settings
        for prop in _EXPORT_PROPS:
            setattr(saved, prop, getattr(self, prop))

        settings = ExportSettings(
            filepath=self.filepath,
            **{prop: getattr(self, prop) for prop in _EXPORT_PROPS},
        )

        try:
            exporter = GltfExporter(context, settings)
            exporter.export()
            self.report({"INFO"}, f"Exported to {self.filepath}")
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}

        return {"FINISHED"}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        layout.prop(self, "export_format")
        layout.prop(self, "export_only_visible")
        layout.prop(self, "export_all_scenes")

        _draw_panels(layout, self, _EXPORT_PANELS)

    def check(self, context):
        # Update file extension based on format
        old_ext = self.filename_ext
        if self.export_format == "GLB":
            self.filename_ext = ".glb"
        else:
            self.filename_ext = ".gltf"

        if self.filename_ext != old_ext:
            import os
            filepath = self.filepath
            if filepath:
                base, _ = os.path.splitext(filepath)
                self.filepath = base + self.filename_ext
                return True
        return False


# Inject the shared export properties into both classes. Must run at module
# level, before register(); each class gets its own fresh instances.
for _cls in (GltfExportSceneSettings, EXPORT_SCENE_OT_gltf):
    _cls.__annotations__ = {
        **vars(_cls).get("__annotations__", {}),
        **_make_export_props(),
    }
del _cls


class IMPORT_SCENE_OT_gltf(bpy.types.Operator, ImportHelper):
    """Import a glTF 2.0 file"""
    bl_idname = "import_scene.gltf_custom"
    bl_label = "Import glTF 2.0"
    bl_options = {"PRESET", "UNDO"}

    filename_ext = ".glb"

    filter_glob: StringProperty(
        default="*.glb;*.gltf",
        options={"HIDDEN"},
    )

    import_normals: BoolProperty(
        name="Normals",
        description="Import vertex normals",
        default=True,
    )

    import_texcoords: BoolProperty(
        name="UVs",
        description="Import UV coordinates",
        default=True,
    )

    import_materials: BoolProperty(
        name="Materials",
        description="Import PBR materials",
        default=True,
    )

    import_colors: BoolProperty(
        name="Vertex Colors",
        description="Import vertex colors",
        default=True,
    )

    import_animations: BoolProperty(
        name="Animations",
        description="Import keyframe animations",
        default=True,
    )

    import_morph_targets: BoolProperty(
        name="Shape Keys",
        description="Import morph targets as shape keys",
        default=True,
    )

    import_skinning: BoolProperty(
        name="Skinning",
        description="Import armatures and bone weights",
        default=True,
    )

    import_physics: BoolProperty(
        name="Physics",
        description="Import rigid bodies and collision shapes",
        default=True,
    )

    import_particles: BoolProperty(
        name="Particles",
        description="Import particle system settings",
        default=True,
    )

    import_interactivity: BoolProperty(
        name="Interactivity",
        description="Import KHR_interactivity behavior graphs as Blender node trees",
        default=True,
    )

    import_audio: BoolProperty(
        name="Audio",
        description="Import KHR_audio_emitter as speaker objects",
        default=True,
    )

    def execute(self, context):
        settings = ImportSettings(
            filepath=self.filepath,
            import_normals=self.import_normals,
            import_texcoords=self.import_texcoords,
            import_materials=self.import_materials,
            import_colors=self.import_colors,
            import_animations=self.import_animations,
            import_morph_targets=self.import_morph_targets,
            import_skinning=self.import_skinning,
            import_physics=self.import_physics,
            import_particles=self.import_particles,
            import_interactivity=self.import_interactivity,
            import_audio=self.import_audio,
        )

        try:
            importer = GltfImporter(context, settings)
            importer.import_file()
            self.report({"INFO"}, f"Imported from {self.filepath}")
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}

        return {"FINISHED"}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        _draw_panels(layout, self, _IMPORT_PANELS)


def menu_func_export(self, context):
    self.layout.operator(EXPORT_SCENE_OT_gltf.bl_idname, text="glTF 2.0 (.glb/.gltf) Custom")


def menu_func_import(self, context):
    self.layout.operator(IMPORT_SCENE_OT_gltf.bl_idname, text="glTF 2.0 (.glb/.gltf) Custom")


def register():
    bpy.utils.register_class(GltfAudioProperties)
    bpy.types.Speaker.gltf_audio = bpy.props.PointerProperty(type=GltfAudioProperties)
    bpy.utils.register_class(DATA_PT_gltf_audio)
    bpy.utils.register_class(GltfMaterialProperties)
    bpy.types.Material.gltf_props = bpy.props.PointerProperty(type=GltfMaterialProperties)
    bpy.utils.register_class(MATERIAL_PT_gltf_properties)
    bpy.utils.register_class(KHR_PhysicsProperties)
    bpy.types.Object.khr_physics = bpy.props.PointerProperty(type=KHR_PhysicsProperties)
    bpy.utils.register_class(PHYSICS_PT_khr_physics)
    bpy.utils.register_class(GltfExportSceneSettings)
    bpy.types.Scene.gltf_export_settings = bpy.props.PointerProperty(type=GltfExportSceneSettings)
    bpy.utils.register_class(EXPORT_SCENE_OT_gltf)
    bpy.utils.register_class(IMPORT_SCENE_OT_gltf)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    bpy.utils.unregister_class(IMPORT_SCENE_OT_gltf)
    bpy.utils.unregister_class(EXPORT_SCENE_OT_gltf)
    del bpy.types.Scene.gltf_export_settings
    bpy.utils.unregister_class(GltfExportSceneSettings)
    bpy.utils.unregister_class(PHYSICS_PT_khr_physics)
    del bpy.types.Object.khr_physics
    bpy.utils.unregister_class(KHR_PhysicsProperties)
    bpy.utils.unregister_class(MATERIAL_PT_gltf_properties)
    del bpy.types.Material.gltf_props
    bpy.utils.unregister_class(GltfMaterialProperties)
    bpy.utils.unregister_class(DATA_PT_gltf_audio)
    del bpy.types.Speaker.gltf_audio
    bpy.utils.unregister_class(GltfAudioProperties)
