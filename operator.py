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


_EXPORT_PROPS = (
    "export_format",
    "export_normals",
    "export_texcoords",
    "export_materials",
    "export_colors",
    "export_animations",
    "export_morph_targets",
    "export_gpu_instancing",
    "export_skinning",
    "export_physics",
    "export_extras",
    "export_particles",
    "export_only_visible",
    "export_all_scenes",
    "export_camera_y_up",
    "image_format",
)


class GltfExportSceneSettings(bpy.types.PropertyGroup):
    """Export settings stored per scene, persisted with the .blend file."""

    export_format: EnumProperty(
        name="Format",
        items=[
            ("GLB", "glTF Binary (.glb)", ""),
            ("GLTF_SEPARATE", "glTF Separate (.gltf + .bin)", ""),
            ("GLTF_EMBEDDED", "glTF Embedded (.gltf)", ""),
        ],
        default="GLB",
    )
    export_normals: BoolProperty(name="Normals", default=True)
    export_texcoords: BoolProperty(name="UVs", default=True)
    export_materials: BoolProperty(name="Materials", default=True)
    export_colors: BoolProperty(name="Vertex Colors", default=True)
    export_animations: BoolProperty(name="Animations", default=True)
    export_morph_targets: BoolProperty(name="Shape Keys", default=True)
    export_gpu_instancing: BoolProperty(name="GPU Instancing", default=True)
    export_skinning: BoolProperty(name="Skinning", default=True)
    export_physics: BoolProperty(name="Physics", default=True)
    export_extras: BoolProperty(name="Custom Properties", default=True)
    export_particles: BoolProperty(name="Particles", default=True)
    export_only_visible: BoolProperty(name="Only Visible", default=False)
    export_all_scenes: BoolProperty(name="All Scenes", default=False)
    export_camera_y_up: BoolProperty(name="Cameras Y-Up", default=True)
    image_format: EnumProperty(
        name="Image Format",
        items=[
            ("AUTO", "Auto", ""),
            ("JPEG", "JPEG", ""),
            ("PNG", "PNG", ""),
        ],
        default="AUTO",
    )


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

    export_format: EnumProperty(
        name="Format",
        items=[
            ("GLB", "glTF Binary (.glb)", "Export as a single binary file"),
            ("GLTF_SEPARATE", "glTF Separate (.gltf + .bin)", "Export as separate JSON and binary files"),
            ("GLTF_EMBEDDED", "glTF Embedded (.gltf)", "Export as a single .gltf with binary data embedded as base64"),
        ],
        default="GLB",
    )

    export_normals: BoolProperty(
        name="Normals",
        description="Export vertex normals",
        default=True,
    )

    export_texcoords: BoolProperty(
        name="UVs",
        description="Export UV coordinates",
        default=True,
    )

    export_materials: BoolProperty(
        name="Materials",
        description="Export PBR materials",
        default=True,
    )

    export_colors: BoolProperty(
        name="Vertex Colors",
        description="Export vertex colors",
        default=True,
    )

    export_animations: BoolProperty(
        name="Animations",
        description="Export keyframe animations",
        default=True,
    )

    export_morph_targets: BoolProperty(
        name="Shape Keys",
        description="Export shape keys as morph targets",
        default=True,
    )

    export_gpu_instancing: BoolProperty(
        name="GPU Instancing",
        description="Export collection instances using EXT_mesh_gpu_instancing",
        default=True,
    )

    export_skinning: BoolProperty(
        name="Skinning",
        description="Export armatures and bone weights",
        default=True,
    )

    export_physics: BoolProperty(
        name="Physics",
        description="Export rigid bodies and collision shapes",
        default=True,
    )

    export_extras: BoolProperty(
        name="Custom Properties",
        description="Export object custom properties as glTF node extras",
        default=True,
    )

    export_particles: BoolProperty(
        name="Particles",
        description="Export particle system settings as CUSTOM_particle_emitter",
        default=True,
    )

    export_only_visible: BoolProperty(
        name="Only Visible",
        description="Only export objects that are visible in the viewport",
        default=False,
    )

    export_all_scenes: BoolProperty(
        name="All Scenes",
        description="Export all Blender scenes into a single glTF file",
        default=False,
    )

    export_camera_y_up: BoolProperty(
        name="Cameras Y-Up",
        description=(
            "Apply a -90° X-axis fix-up to camera nodes so their forward "
            "direction matches the glTF Y-up convention. Disable for tools "
            "that expect Blender's raw camera orientation"
        ),
        default=True,
    )

    image_format: EnumProperty(
        name="Image Format",
        description="Format for exported textures",
        items=[
            ("AUTO", "Auto", "Keep the original image format"),
            ("JPEG", "JPEG", "Export all textures as JPEG"),
            ("PNG", "PNG", "Export all textures as PNG"),
        ],
        default="AUTO",
    )

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
            format=self.export_format,
            export_normals=self.export_normals,
            export_texcoords=self.export_texcoords,
            export_materials=self.export_materials,
            export_colors=self.export_colors,
            export_animations=self.export_animations,
            export_morph_targets=self.export_morph_targets,
            export_gpu_instancing=self.export_gpu_instancing,
            export_skinning=self.export_skinning,
            export_physics=self.export_physics,
            export_extras=self.export_extras,
            export_particles=self.export_particles,
            export_only_visible=self.export_only_visible,
            export_all_scenes=self.export_all_scenes,
            export_camera_y_up=self.export_camera_y_up,
            image_format=self.image_format,
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

        header, body = layout.panel("GLTF_export_mesh", default_closed=True)
        header.label(text="Mesh")
        if body:
            body.prop(self, "export_normals")
            body.prop(self, "export_texcoords")
            body.prop(self, "export_colors")

        header, body = layout.panel("GLTF_export_material", default_closed=True)
        header.label(text="Material")
        if body:
            body.prop(self, "export_materials")
            body.prop(self, "image_format")

        header, body = layout.panel("GLTF_export_animation", default_closed=True)
        header.label(text="Animation")
        if body:
            body.prop(self, "export_animations")
            body.prop(self, "export_morph_targets")

        header, body = layout.panel("GLTF_export_skinning", default_closed=True)
        header.label(text="Skinning")
        if body:
            body.prop(self, "export_skinning")

        header, body = layout.panel("GLTF_export_instancing", default_closed=True)
        header.label(text="Instancing")
        if body:
            body.prop(self, "export_gpu_instancing")

        header, body = layout.panel("GLTF_export_cameras", default_closed=True)
        header.label(text="Cameras")
        if body:
            body.prop(self, "export_camera_y_up")

        header, body = layout.panel("GLTF_export_physics", default_closed=True)
        header.label(text="Physics")
        if body:
            body.prop(self, "export_physics")

        header, body = layout.panel("GLTF_export_particles", default_closed=True)
        header.label(text="Particles")
        if body:
            body.prop(self, "export_particles")

        header, body = layout.panel("GLTF_export_extras", default_closed=True)
        header.label(text="Extras")
        if body:
            body.prop(self, "export_extras")

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

        header, body = layout.panel("GLTF_import_mesh", default_closed=True)
        header.label(text="Mesh")
        if body:
            body.prop(self, "import_normals")
            body.prop(self, "import_texcoords")
            body.prop(self, "import_colors")

        header, body = layout.panel("GLTF_import_material", default_closed=True)
        header.label(text="Material")
        if body:
            body.prop(self, "import_materials")

        header, body = layout.panel("GLTF_import_animation", default_closed=True)
        header.label(text="Animation")
        if body:
            body.prop(self, "import_animations")
            body.prop(self, "import_morph_targets")

        header, body = layout.panel("GLTF_import_skinning", default_closed=True)
        header.label(text="Skinning")
        if body:
            body.prop(self, "import_skinning")

        header, body = layout.panel("GLTF_import_physics", default_closed=True)
        header.label(text="Physics")
        if body:
            body.prop(self, "import_physics")

        header, body = layout.panel("GLTF_import_particles", default_closed=True)
        header.label(text="Particles")
        if body:
            body.prop(self, "import_particles")


def menu_func_export(self, context):
    self.layout.operator(EXPORT_SCENE_OT_gltf.bl_idname, text="glTF 2.0 (.glb/.gltf) Custom")


def menu_func_import(self, context):
    self.layout.operator(IMPORT_SCENE_OT_gltf.bl_idname, text="glTF 2.0 (.glb/.gltf) Custom")


def register():
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
