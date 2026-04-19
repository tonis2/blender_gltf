import bpy
from bpy.props import EnumProperty, BoolProperty, StringProperty
from bpy_extras.io_utils import ExportHelper

from .exporter import ExportSettings, GltfExporter


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
        box.label(text="Mesh")
        box.prop(self, "export_normals")
        box.prop(self, "export_texcoords")
        box.prop(self, "export_colors")

        box = layout.box()
        box.label(text="Material")
        box.prop(self, "export_materials")

        box = layout.box()
        box.label(text="Animation")
        box.prop(self, "export_animations")
        box.prop(self, "export_morph_targets")

        box = layout.box()
        box.label(text="Instancing")
        box.prop(self, "export_gpu_instancing")

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


def menu_func_export(self, context):
    self.layout.operator(EXPORT_SCENE_OT_gltf.bl_idname, text="glTF 2.0 (.glb/.gltf) Custom")


def register():
    bpy.utils.register_class(EXPORT_SCENE_OT_gltf)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    bpy.utils.unregister_class(EXPORT_SCENE_OT_gltf)
