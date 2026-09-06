import bpy
from bpy.props import EnumProperty, BoolProperty, StringProperty, FloatProperty, FloatVectorProperty, IntProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper

from . import ktx_lib
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

    # Hand-packed ORM(A) texture support. The metallicRoughness texture holds
    # Roughness in G and Metallic in B; its R and A channels are reinterpreted
    # per these settings so a single artist-packed image round-trips.
    packed_r_channel: EnumProperty(
        name="Packed R Channel",
        description=(
            "Meaning of the metallic/roughness texture's Red channel. Occlusion "
            "exports a spec occlusionTexture sharing the same image. (Depth/height "
            "needs no setting here: route R through a Bump node into Normal and it "
            "exports via the height/bump slot automatically)"
        ),
        items=[
            ("NONE", "None", "Red channel is unused"),
            ("OCCLUSION", "Ambient Occlusion", "Red channel is an AO map (glTF occlusionTexture)"),
        ],
        default="NONE",
    )
    packed_alpha: BoolProperty(
        name="Packed Alpha",
        description=(
            "The metallic/roughness texture's Alpha channel carries base alpha "
            "(CUSTOM_packed_texture), instead of alpha riding in the base color texture"
        ),
        default=False,
    )
    occlusion_strength: FloatProperty(
        name="Occlusion Strength",
        description="occlusionTexture strength when Packed R Channel is Ambient Occlusion",
        default=1.0,
        min=0.0,
        max=1.0,
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

        box = layout.box()
        box.label(text="Packed Texture (ORM/A)")
        box.prop(props, "packed_r_channel")
        if props.packed_r_channel == "OCCLUSION":
            box.prop(props, "occlusion_strength")
        box.prop(props, "packed_alpha")


class GltfWalkabilityProperties(bpy.types.PropertyGroup):
    """CUSTOM_walkability_mask: attach a painted walkability mask to a mesh.

    White (above threshold) marks where the player/NPCs can walk. The engine
    samples this mask through the mesh's UVs for grid pathfinding / movement.
    """

    enabled: BoolProperty(
        name="Export Walkability Mask",
        description="Attach a walkability mask to this mesh as CUSTOM_walkability_mask",
        default=False,
    )
    mask: bpy.props.PointerProperty(
        name="Mask",
        description="Image whose channel encodes walkable areas (white = walkable)",
        type=bpy.types.Image,
    )
    threshold: FloatProperty(
        name="Threshold",
        description="Walkable test cutoff against the sampled channel value",
        default=0.5,
        min=0.0,
        max=1.0,
    )
    channel: EnumProperty(
        name="Channel",
        description="Which image channel encodes walkability",
        items=[
            ("R", "Red", ""),
            ("G", "Green", ""),
            ("B", "Blue", ""),
            ("A", "Alpha", ""),
        ],
        default="R",
    )
    walkable_above: BoolProperty(
        name="Walkable Above Threshold",
        description=(
            "When on, areas where the channel value is above the threshold are "
            "walkable; turn off to invert (walkable below the threshold)"
        ),
        default=True,
    )


class OBJECT_PT_gltf_walkability(bpy.types.Panel):
    """Panel in object properties for the walkability mask extension."""
    bl_label = "glTF Walkability"
    bl_idname = "OBJECT_PT_gltf_walkability"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "object"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == "MESH"

    def draw(self, context):
        layout = self.layout
        props = context.active_object.gltf_walkability
        layout.prop(props, "enabled")
        col = layout.column()
        col.enabled = props.enabled
        col.prop(props, "mask")
        col.prop(props, "channel")
        col.prop(props, "threshold")
        col.prop(props, "walkable_above")


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
    "export_walkability": (BoolProperty, dict(
        name="Walkability",
        description="Export per-object walkability masks as CUSTOM_walkability_mask",
        default=True,
    )),
    "export_environment_map": (BoolProperty, dict(
        name="Environment Map",
        description=("Export the scene world's equirectangular environment as a "
                     "KTX2 cubemap plus irradiance SH, via KHR_environment_map"),
        default=True,
    )),
    "environment_map_size": (EnumProperty, dict(
        name="Cubemap Size",
        description="Face resolution of the exported environment cubemap",
        items=[
            ("AUTO", "Auto", "Quarter of the equirect width, rounded down to a power of two"),
            ("256", "256", "256x256 per face"),
            ("512", "512", "512x512 per face"),
            ("1024", "1024", "1024x1024 per face"),
            ("2048", "2048", "2048x2048 per face"),
        ],
        default="AUTO",
    )),
    "environment_map_codec": (EnumProperty, dict(
        name="Cubemap Codec",
        description="Texture format for the exported environment cubemap",
        items=[
            ("rgba8", "RGBA8", "Uncompressed, lossless, largest"),
            ("bc7", "BC7", "Block compressed, desktop GPUs"),
            ("uastc", "UASTC", "Basis Universal, transcodes to any GPU format"),
            ("etc1s", "ETC1S", "Basis Universal, smallest, lossiest"),
        ],
        default="rgba8",
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
            ("KTX2", "KTX2 (Basis)", "GPU-compressed KTX2 via the ktx "
             "library, downloaded on demand (KHR_texture_basisu)"),
        ],
        default="AUTO",
    )),
    "ktx_codec": (EnumProperty, dict(
        name="Color Codec",
        description="Codec for color/data KTX2 textures "
                    "(base color, emission, roughness, masks, ...)",
        items=[
            ("uastc", "UASTC", "Higher quality, larger files (Zstd-compressed)"),
            ("etc1s", "ETC1S", "Smallest files, lower quality (BasisLZ)"),
            ("bc7", "BC7", "Block compressed, desktop GPUs upload it without "
             "transcoding. Not Basis Universal, so a strict "
             "KHR_texture_basisu viewer will refuse it"),
        ],
        default="uastc",
    )),
    "ktx_quality": (IntProperty, dict(
        name="Color Quality",
        description="Encoding quality for color/data KTX2 textures "
                    "(Basis codecs only; BC7 is fixed-rate)",
        min=0, max=100, default=90, subtype="PERCENTAGE",
    )),
    "ktx_normal_codec": (EnumProperty, dict(
        name="Normal Codec",
        description="Codec for normal and height/bump maps, which need more "
                    "detail than color textures",
        items=[
            ("uastc", "UASTC", "Higher quality, larger files (Zstd-compressed)"),
            ("etc1s", "ETC1S", "Smallest files, lower quality (BasisLZ)"),
            ("bc7", "BC7", "Block compressed, desktop GPUs upload it without "
             "transcoding. Not Basis Universal, so a strict "
             "KHR_texture_basisu viewer will refuse it"),
        ],
        default="uastc",
    )),
    "ktx_normal_quality": (IntProperty, dict(
        name="Normal Quality",
        description="Encoding quality for normal and height/bump maps "
                    "(Basis codecs only; BC7 is fixed-rate)",
        min=0, max=100, default=100, subtype="PERCENTAGE",
    )),
    "ktx_effort": (IntProperty, dict(
        name="Effort",
        description="Encoder search effort for Basis KTX2 textures "
                    "(higher is slower but compresses better; BC7 "
                    "ignores it)",
        min=0, max=10, default=2,
    )),
    "ktx_mipmaps": (BoolProperty, dict(
        name="Mipmaps",
        description="Write a mip chain into each KTX2, about 33% more bytes. ",
        default=True,
    )),
    "bake_materials": (BoolProperty, dict(
        name="Bake Materials",
        description=(
            "Automatically bake any material whose base color, roughness, "
            "normal or emission is driven by procedural nodes (Noise, Voronoi, "
            "bumps, ...) to images, so it survives in glTF. Materials already "
            "made of plain image textures / constant factors are left untouched. "
            "A material's own 'Bake Textures on Export' checkbox forces baking "
            "regardless of this option"
        ),
        default=False,
    )),
    "bake_resolution": (EnumProperty, dict(
        name="Bake Resolution",
        description="Resolution of auto-baked texture maps",
        items=[
            ("512", "512 x 512", ""),
            ("1024", "1024 x 1024", ""),
            ("2048", "2048 x 2048", ""),
            ("4096", "4096 x 4096", ""),
        ],
        default="1024",
    )),
    "force_64bit": (BoolProperty, dict(
        name="Force 64-bit GLB",
        description=(
            "Always write the glTF 2.1 [DRAFT] version-3 (64-bit) GLB "
            "container, even below 4 GiB. Version 3 is used automatically when "
            "a GLB exceeds the 4 GiB limit of the version-2 container"
        ),
        default=False,
    )),
    "export_uids": (BoolProperty, dict(
        name="Unique IDs",
        description=(
            "Write a glTF 2.1 [DRAFT] unique 'uid' on every object node, "
            "reusing an object's gltf_uid custom property or generating a "
            "stable one. Gives the engine dependable per-object handles"
        ),
        default=True,
    )),
    "export_external_assets": (BoolProperty, dict(
        name="External Assets",
        description=(
            "Export collection-instance empties as glTF 2.1 [DRAFT] external "
            "asset references (files array). Each unique instanced collection "
            "becomes its own sub-asset"
        ),
        default=False,
    )),
    "external_assets_mode": (EnumProperty, dict(
        name="External Mode",
        description="How external sub-assets are stored",
        items=[
            ("PACKAGED", "Packaged", "Embed sub-assets inside this file (self-contained)"),
            ("REFERENCES", "References", "Write sub-assets as separate sibling .glb files"),
        ],
        default="PACKAGED",
    )),
}

_EXPORT_PROPS = tuple(_EXPORT_PROP_DEFS)


def _make_export_props() -> dict:
    """Build fresh property instances (definitions must not be shared across classes)."""
    return {name: ctor(**kwargs) for name, (ctor, kwargs) in _EXPORT_PROP_DEFS.items()}


# Collapsible panel layout for the export/import file browser sidebars:
# (panel_id, label, prop_names). Panel ids persist open/closed state.
def _draw_material_extras(body, owner):
    """KTX2 encoder settings (only while KTX2 is selected) + bake options."""
    if owner.image_format == "KTX2":
        col = body.column()
        for prop in ("ktx_codec", "ktx_quality",
                     "ktx_normal_codec", "ktx_normal_quality", "ktx_effort",
                     "ktx_mipmaps"):
            col.prop(owner, prop)
        if not ktx_lib.is_available():
            # Offer the KTX binaries download when they are missing.
            col.label(text="KTX binaries are not installed", icon="ERROR")
            col.operator("gltf_custom.download_ktx", icon="IMPORT")
    body.prop(owner, "bake_materials")
    body.prop(owner, "bake_resolution")


_EXPORT_PANELS = (
    ("GLTF_export_mesh", "Mesh", (
        "export_normals",
        "export_texcoords",
        "export_tangents",
        "export_colors",
        "export_quantization",
    )),
    ("GLTF_export_material", "Material", (
        "export_materials", "image_format",
    ), _draw_material_extras),
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
    ("GLTF_export_walkability", "Walkability", ("export_walkability",)),
    ("GLTF_export_environment", "Environment Map", (
        "export_environment_map",
        "environment_map_size",
        "environment_map_codec",
    )),
    ("GLTF_export_extras", "Extras", ("export_extras", "export_uids")),
    ("GLTF_export_external", "External Assets", (
        "export_external_assets",
        "external_assets_mode",
    )),
    ("GLTF_export_binary", "Binary", ("force_64bit",)),
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
    ("GLTF_import_walkability", "Walkability", ("import_walkability",)),
    ("GLTF_import_external", "External Assets", ("import_external_assets",)),
    ("GLTF_import_extras", "Extras", ("import_uids",)),
)


def _draw_panels(layout, owner, panels):
    """Draw collapsible panels from a (panel_id, label, prop_names[, extra])
    table; the optional extra is a callback drawing below the properties."""
    for panel_id, label, prop_names, *extra in panels:
        header, body = layout.panel(panel_id, default_closed=True)
        header.label(text=label)
        if body:
            for prop in prop_names:
                body.prop(owner, prop)
            for draw_extra in extra:
                draw_extra(body, owner)


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

        if settings.image_format == "KTX2" and not ktx_lib.is_available():
            # Ask to download the native lib instead of failing mid-export.
            bpy.ops.gltf_custom.download_ktx("INVOKE_DEFAULT")
            self.report({"ERROR"},
                        "KTX2 textures need the ktx native library — confirm "
                        "the download, then export again")
            return {"CANCELLED"}

        self._exporter = GltfExporter(context, settings)
        self._timer = None
        try:
            self._exporter.build_begin()
        except Exception as e:
            self.report({"ERROR"}, str(e))
            self._exporter.material_exporter.cleanup()
            return {"CANCELLED"}

        # KTX2 encodes run on a background thread; go modal so Blender stays
        # responsive while they finish. Scripted calls (timers, MCP) have no
        # window in their context, so borrow one from the window manager;
        # truly headless runs fall back to blocking — same behavior as before.
        tex = self._exporter.texture_exporter
        win = context.window
        if win is None:
            windows = list(getattr(context.window_manager, "windows", []))
            win = windows[0] if windows else None
        # Background mode may still expose a window (Blender 5.x) but runs no
        # event loop, so the modal timer would never fire — stay blocking.
        if not bpy.app.background and win is not None and not tex.ktx_pending_done():
            wm = context.window_manager
            try:
                wm.modal_handler_add(self)
            except RuntimeError:
                return self._finish(context)  # no UI loop to keep responsive
            self._timer = wm.event_timer_add(0.1, window=win)
            return {"RUNNING_MODAL"}

        return self._finish(context)

    def modal(self, context, event):
        tex = self._exporter.texture_exporter
        if event.type == "ESC":
            self._end_modal(context)
            tex.cancel_ktx()
            self._exporter.material_exporter.cleanup()
            self.report({"WARNING"}, "glTF export cancelled")
            return {"CANCELLED"}
        if event.type != "TIMER":
            # Let the user keep working while the encodes run.
            return {"PASS_THROUGH"}
        if not tex.ktx_pending_done():
            done, total = tex.ktx_progress()
            if context.workspace is not None:
                context.workspace.status_text_set(
                    f"glTF export: encoding KTX2 textures ({done}/{total} "
                    f"done, Esc to cancel)")
            return {"RUNNING_MODAL"}
        self._end_modal(context)
        return self._finish(context)

    def _end_modal(self, context):
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        if context.workspace is not None:
            context.workspace.status_text_set(None)

    def _finish(self, context):
        exporter = self._exporter
        try:
            gltf_dict, binary = exporter.build_finish()
            exporter.write_output(gltf_dict, binary)
            self.report({"INFO"}, f"Exported to {self.filepath}")
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        finally:
            # Tear down any temporary bake data even if the export errored.
            exporter.material_exporter.cleanup()

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

    import_walkability: BoolProperty(
        name="Walkability",
        description="Import CUSTOM_walkability_mask back onto mesh objects",
        default=True,
    )

    import_uids: BoolProperty(
        name="Unique IDs",
        description="Restore glTF 2.1 [DRAFT] node uids as gltf_uid custom properties",
        default=True,
    )

    import_external_assets: BoolProperty(
        name="External Assets",
        description=(
            "Instantiate glTF 2.1 [DRAFT] external/packaged asset references "
            "as Blender collection instances"
        ),
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
            import_walkability=self.import_walkability,
            import_uids=self.import_uids,
            import_external_assets=self.import_external_assets,
        )

        self._importer = GltfImporter(context, settings)
        self._timer = None
        try:
            self._importer.import_begin()
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}

        # KTX2 decodes run on a background thread; go modal so Blender stays
        # responsive while they finish. Same fallbacks as the exporter:
        # scripted calls have no window in their context, so borrow one from
        # the window manager, and background mode (which may still expose a
        # window but runs no event loop) stays blocking.
        win = context.window
        if win is None:
            windows = list(getattr(context.window_manager, "windows", []))
            win = windows[0] if windows else None
        if (not bpy.app.background and win is not None
                and not self._importer.ktx_pending_done()):
            wm = context.window_manager
            try:
                wm.modal_handler_add(self)
            except RuntimeError:
                return self._finish(context)  # no UI loop to keep responsive
            # Faster than the exporter's tick, because this one does work rather
            # than only reporting it: every tick lands whatever the decode workers
            # have finished, and at 0.1s that landing rate — not the decoding —
            # was what the import ran at. pump_ktx bounds its own main-thread
            # time, so a short interval costs responsiveness nothing.
            self._timer = wm.event_timer_add(1.0 / 60.0, window=win)
            return {"RUNNING_MODAL"}

        return self._finish(context)

    def modal(self, context, event):
        if event.type == "ESC":
            self._end_modal(context)
            self._importer.cancel_ktx()
            self.report({"WARNING"}, "glTF import cancelled")
            return {"CANCELLED"}
        if event.type != "TIMER":
            # Let the user keep working while the decodes run.
            return {"PASS_THROUGH"}
        done, total = self._importer.pump_ktx()
        if not self._importer.ktx_pending_done():
            if context.workspace is not None:
                context.workspace.status_text_set(
                    f"glTF import: decoding KTX2 textures ({done}/{total} "
                    f"done, Esc to cancel)")
            return {"RUNNING_MODAL"}
        self._end_modal(context)
        return self._finish(context)

    def _end_modal(self, context):
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        if context.workspace is not None:
            context.workspace.status_text_set(None)

    def _finish(self, context):
        try:
            self._importer.import_finish()
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Imported from {self.filepath}")
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
    bpy.utils.register_class(GltfWalkabilityProperties)
    bpy.types.Object.gltf_walkability = bpy.props.PointerProperty(type=GltfWalkabilityProperties)
    bpy.utils.register_class(OBJECT_PT_gltf_walkability)
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
    bpy.utils.unregister_class(OBJECT_PT_gltf_walkability)
    del bpy.types.Object.gltf_walkability
    bpy.utils.unregister_class(GltfWalkabilityProperties)
    bpy.utils.unregister_class(PHYSICS_PT_khr_physics)
    del bpy.types.Object.khr_physics
    bpy.utils.unregister_class(KHR_PhysicsProperties)
    bpy.utils.unregister_class(MATERIAL_PT_gltf_properties)
    del bpy.types.Material.gltf_props
    bpy.utils.unregister_class(GltfMaterialProperties)
    bpy.utils.unregister_class(DATA_PT_gltf_audio)
    del bpy.types.Speaker.gltf_audio
    bpy.utils.unregister_class(GltfAudioProperties)
