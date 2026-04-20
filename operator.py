import bpy
from bpy.props import EnumProperty, BoolProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper

from .exporter import ExportSettings, GltfExporter
from .importer import ImportSettings, GltfImporter


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

    # Section toggles (not exported, just for UI)
    show_mesh: BoolProperty(name="Mesh", default=True, options={"HIDDEN"})
    show_material: BoolProperty(name="Material", default=True, options={"HIDDEN"})
    show_animation: BoolProperty(name="Animation", default=True, options={"HIDDEN"})
    show_skinning: BoolProperty(name="Skinning", default=True, options={"HIDDEN"})
    show_instancing: BoolProperty(name="Instancing", default=True, options={"HIDDEN"})
    show_physics: BoolProperty(name="Physics", default=True, options={"HIDDEN"})

    def execute(self, context):
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
        layout.prop(self, "export_format")

        box = layout.box()
        row = box.row()
        row.prop(self, "show_mesh",
                 icon="DISCLOSURE_TRI_DOWN" if self.show_mesh else "DISCLOSURE_TRI_RIGHT",
                 emboss=False)
        if self.show_mesh:
            box.prop(self, "export_normals")
            box.prop(self, "export_texcoords")
            box.prop(self, "export_colors")

        box = layout.box()
        row = box.row()
        row.prop(self, "show_material",
                 icon="DISCLOSURE_TRI_DOWN" if self.show_material else "DISCLOSURE_TRI_RIGHT",
                 emboss=False)
        if self.show_material:
            box.prop(self, "export_materials")

        box = layout.box()
        row = box.row()
        row.prop(self, "show_animation",
                 icon="DISCLOSURE_TRI_DOWN" if self.show_animation else "DISCLOSURE_TRI_RIGHT",
                 emboss=False)
        if self.show_animation:
            box.prop(self, "export_animations")
            box.prop(self, "export_morph_targets")

        box = layout.box()
        row = box.row()
        row.prop(self, "show_skinning",
                 icon="DISCLOSURE_TRI_DOWN" if self.show_skinning else "DISCLOSURE_TRI_RIGHT",
                 emboss=False)
        if self.show_skinning:
            box.prop(self, "export_skinning")

        box = layout.box()
        row = box.row()
        row.prop(self, "show_instancing",
                 icon="DISCLOSURE_TRI_DOWN" if self.show_instancing else "DISCLOSURE_TRI_RIGHT",
                 emboss=False)
        if self.show_instancing:
            box.prop(self, "export_gpu_instancing")

        box = layout.box()
        row = box.row()
        row.prop(self, "show_physics",
                 icon="DISCLOSURE_TRI_DOWN" if self.show_physics else "DISCLOSURE_TRI_RIGHT",
                 emboss=False)
        if self.show_physics:
            box.prop(self, "export_physics")

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

    # Section toggles (not imported, just for UI)
    show_mesh: BoolProperty(name="Mesh", default=True, options={"HIDDEN"})
    show_material: BoolProperty(name="Material", default=True, options={"HIDDEN"})
    show_animation: BoolProperty(name="Animation", default=True, options={"HIDDEN"})
    show_skinning: BoolProperty(name="Skinning", default=True, options={"HIDDEN"})
    show_physics: BoolProperty(name="Physics", default=True, options={"HIDDEN"})

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

        box = layout.box()
        row = box.row()
        row.prop(self, "show_mesh",
                 icon="DISCLOSURE_TRI_DOWN" if self.show_mesh else "DISCLOSURE_TRI_RIGHT",
                 emboss=False)
        if self.show_mesh:
            box.prop(self, "import_normals")
            box.prop(self, "import_texcoords")
            box.prop(self, "import_colors")

        box = layout.box()
        row = box.row()
        row.prop(self, "show_material",
                 icon="DISCLOSURE_TRI_DOWN" if self.show_material else "DISCLOSURE_TRI_RIGHT",
                 emboss=False)
        if self.show_material:
            box.prop(self, "import_materials")

        box = layout.box()
        row = box.row()
        row.prop(self, "show_animation",
                 icon="DISCLOSURE_TRI_DOWN" if self.show_animation else "DISCLOSURE_TRI_RIGHT",
                 emboss=False)
        if self.show_animation:
            box.prop(self, "import_animations")
            box.prop(self, "import_morph_targets")

        box = layout.box()
        row = box.row()
        row.prop(self, "show_skinning",
                 icon="DISCLOSURE_TRI_DOWN" if self.show_skinning else "DISCLOSURE_TRI_RIGHT",
                 emboss=False)
        if self.show_skinning:
            box.prop(self, "import_skinning")

        box = layout.box()
        row = box.row()
        row.prop(self, "show_physics",
                 icon="DISCLOSURE_TRI_DOWN" if self.show_physics else "DISCLOSURE_TRI_RIGHT",
                 emboss=False)
        if self.show_physics:
            box.prop(self, "import_physics")


def menu_func_export(self, context):
    self.layout.operator(EXPORT_SCENE_OT_gltf.bl_idname, text="glTF 2.0 (.glb/.gltf) Custom")


def menu_func_import(self, context):
    self.layout.operator(IMPORT_SCENE_OT_gltf.bl_idname, text="glTF 2.0 (.glb/.gltf) Custom")


def register():
    bpy.utils.register_class(EXPORT_SCENE_OT_gltf)
    bpy.utils.register_class(IMPORT_SCENE_OT_gltf)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    bpy.utils.unregister_class(IMPORT_SCENE_OT_gltf)
    bpy.utils.unregister_class(EXPORT_SCENE_OT_gltf)
