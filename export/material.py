from __future__ import annotations

from typing import TYPE_CHECKING

from ..gltf.types import Material, MaterialPBRMetallicRoughness, NormalTextureInfo
from ..layer_node.constants import N_CH, CH_TO_IDX, BLEND_MODES
from .texture import TextureExporter

if TYPE_CHECKING:
    import bpy
    from ..exporter import ExportSettings


EXT_MATERIALS_UNLIT = "KHR_materials_unlit"
EXT_MATERIALS_LAYERS = "CUSTOM_materials_layers"
EXT_EMISSIVE_STRENGTH = "KHR_materials_emissive_strength"
EXT_MATERIALS_CLEARCOAT = "KHR_materials_clearcoat"
EXT_MATERIALS_SHEEN = "KHR_materials_sheen"
BSDF_STACK_NODE_IDNAME = "BSDFStackNodeType"
# All Blender ShaderNodeMix blend types the BSDFStackNode exposes per layer.
_VALID_BLEND_MODES = {m[0] for m in BLEND_MODES}
_VALID_MASK_CHANNELS = {"R", "G", "B", "A"}


class MaterialExporter:
    def __init__(self, texture_exporter: TextureExporter, settings: "ExportSettings") -> None:
        self.texture_exporter = texture_exporter
        self.settings = settings
        self.materials: list[Material] = []
        self.material_index_by_name: dict[str, int] = {}
        self.extensions_used: set[str] = set()

        # Export-time bake bookkeeping (see _gather_baked / cleanup).
        self._bake_index_cache: dict[tuple, int] = {}   # (mat, obj) -> gltf index
        self._bake_targets: dict[str, object] = {}      # source mat name -> target node
        self._temp_images: list = []
        self._temp_materials: list = []
        self._temp_uvs: list = []                        # (mesh, uv_layer_name)
        self._temp_nodes: list = []                      # (node_tree, node)
        self._hidden_restore: list = []                  # (obj, hide_viewport)
        self._temp_linked: list = []                     # (obj, collection)
        self._bake_saved: dict | None = None

    def gather(
        self, blender_material: "bpy.types.Material", obj: "bpy.types.Object | None" = None
    ) -> int | None:
        """Export a Blender material. Returns material index or None.

        `obj` is the object the material is gathered for; it is required for the
        bake path (baking needs the object's UVs). Callers without an object
        simply skip baking.
        """
        if blender_material is None:
            return None
        if obj is not None and self._should_bake(blender_material):
            return self._gather_baked(blender_material, obj)
        return self._gather_unbaked(blender_material)

    def _add_material(self, material: "Material") -> int:
        """Append a Material to the export list and return its index."""
        index = len(self.materials)
        self.materials.append(material)
        return index

    def _gather_unbaked(self, blender_material) -> int:
        """Extract and register a material, de-duplicated by name."""
        index = self.material_index_by_name.get(blender_material.name)
        if index is not None:
            return index
        index = self._add_material(self._extract(blender_material))
        self.material_index_by_name[blender_material.name] = index
        return index

    def _should_bake(self, blender_material: "bpy.types.Material") -> bool:
        """Bake when the global 'Bake Materials' export option is on and the
        material actually needs it (a channel is procedural and can't be
        represented directly)."""
        if not getattr(self.settings, "bake_materials", False):
            return False
        return self._material_needs_bake(blender_material)

    # Nodes that resolve to a constant glTF factor (no bake needed) even though
    # they're "linked": plain RGB/Value/Combine constants.
    _CONSTANT_NODE_TYPES = {"RGB", "VALUE", "COMBINE_COLOR", "COMBINE_RGB", "COMBXYZ"}

    def _socket_needs_bake(self, socket) -> bool:
        """True if `socket` is driven by procedural nodes that glTF can't hold.
        A socket resolving to an Image Texture (directly exportable) or to a
        plain constant node (exportable as a factor) does NOT need baking."""
        if socket is None or not socket.is_linked:
            return False
        if self._walk_to_image(socket) is not None:
            return False  # representable as a texture as-is
        src_type = getattr(socket.links[0].from_node, "type", "")
        if src_type in self._CONSTANT_NODE_TYPES:
            return False  # representable as a constant factor
        return True

    def _material_needs_bake(self, blender_material: "bpy.types.Material") -> bool:
        """Heuristic: does any glTF-relevant Principled channel depend on
        procedural nodes (so it would be lost without baking)?"""
        principled = self._find_principled_bsdf(blender_material)
        if principled is None:
            return False  # custom/surface-group materials are not auto-baked
        for name in ("Base Color", "Metallic", "Roughness", "Normal", "Emission Color"):
            if self._socket_needs_bake(principled.inputs.get(name)):
                return True
        return False

    def _gather_baked(self, blender_material, obj) -> int | None:
        """Bake `blender_material`'s procedural channels using `obj`'s UVs, then
        export the result. One texture set per (material, object): objects sharing
        a procedural material have independent UV layouts.
        """
        cache_key = (blender_material.name, obj.name)
        if cache_key in self._bake_index_cache:
            return self._bake_index_cache[cache_key]

        baked_mat = self._bake_material(blender_material, obj)
        if baked_mat is None:
            # Bake failed -> fall back to a plain (un-baked) export so the
            # material still appears, just without its procedural detail.
            return self._gather_unbaked(blender_material)

        material = self._extract(baked_mat)
        material.name = blender_material.name  # keep the user-facing name
        index = self._add_material(material)
        self._bake_index_cache[cache_key] = index
        return index

    # ------------------------------------------------------------------
    # Export-time per-channel texture baking ("Bake Materials")
    # ------------------------------------------------------------------

    @staticmethod
    def _get_override() -> dict:
        """A VIEW_3D context override so bpy.ops (unwrap/bake) poll correctly
        when driven from the exporter rather than a 3D viewport."""
        import bpy
        for w in bpy.context.window_manager.windows:
            for a in w.screen.areas:
                if a.type == "VIEW_3D":
                    region = next((r for r in a.regions if r.type == "WINDOW"), None)
                    return {"window": w, "screen": w.screen, "area": a, "region": region}
        return {}

    def _bake_resolution(self, blender_material) -> int:
        """Resolution for the bake, from the global export 'Bake Resolution'."""
        val = getattr(self.settings, "bake_resolution", "1024")
        try:
            return int(val)
        except (TypeError, ValueError):
            return 1024

    def _begin_bake_session(self) -> None:
        import bpy
        sc = bpy.context.scene
        if self._bake_saved is None:
            self._bake_saved = {
                "engine": sc.render.engine,
                "samples": getattr(getattr(sc, "cycles", None), "samples", None),
                "active": bpy.context.view_layer.objects.active,
                "selected": [o for o in bpy.context.view_layer.objects if o.select_get()],
            }
        sc.render.engine = "CYCLES"
        try:
            sc.cycles.samples = 1   # albedo/roughness/normal are deterministic
        except Exception:
            pass
        rb = sc.render.bake
        rb.use_clear = True
        rb.margin = 6

    def _ensure_uv(self, obj) -> None:
        """Make obj baking-ready: a UV map active, object visible & selectable.
        Creates a temporary UV layer (Smart UV Project) when the mesh has none.
        """
        import bpy
        me = obj.data
        # Objects reached via hierarchy traversal may not be in the active view
        # layer (so bpy.ops can't select them); link them in for the bake.
        if obj.name not in bpy.context.view_layer.objects:
            coll = bpy.context.scene.collection
            coll.objects.link(obj)
            self._temp_linked.append((obj, coll))
            bpy.context.view_layer.update()
        if obj.hide_viewport:
            self._hidden_restore.append((obj, obj.hide_viewport))
            obj.hide_viewport = False
        if me.uv_layers:
            me.uv_layers.active = me.uv_layers[0]
            return
        ov = self._get_override()
        with bpy.context.temp_override(**ov):
            bpy.ops.object.select_all(action="DESELECT")
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="SELECT")
            bpy.ops.uv.smart_project(angle_limit=1.15, island_margin=0.02)
            bpy.ops.object.mode_set(mode="OBJECT")
        if me.uv_layers.active is not None:
            self._temp_uvs.append((me, me.uv_layers.active.name))

    def _bake_pass(self, obj, source_mat, target, name, res, bake_type, non_color):
        import bpy
        img = bpy.data.images.get(name)
        if img is not None:
            bpy.data.images.remove(img)
        img = bpy.data.images.new(name, res, res, alpha=False)
        img.colorspace_settings.name = "Non-Color" if non_color else "sRGB"
        self._temp_images.append(img)
        target.image = img

        sc = bpy.context.scene
        rb = sc.render.bake
        ov = self._get_override()
        with bpy.context.temp_override(**ov):
            bpy.ops.object.select_all(action="DESELECT")
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj
            if bake_type == "DIFFUSE":
                rb.use_pass_direct = False
                rb.use_pass_indirect = False
                rb.use_pass_color = True
            bpy.ops.object.bake(type=bake_type)
        return img

    def _bake_material(self, source_mat, obj):
        """Bake only the *procedural* channels of source_mat for `obj` and return
        a copy material whose procedural channels point at freshly baked images
        while its image-backed channels keep the original textures untouched.
        Returns None on failure (caller falls back to an un-baked export).

        Per-channel: Base Color / Normal / Emission each bake to their own image
        only when that channel can't be represented directly (procedural). Metallic
        and Roughness share one glTF slot, so when either is procedural both are
        sampled and packed into a single metallic-roughness image (G=roughness,
        B=metallic). Channels that already resolve to an image (or a constant) are
        left exactly as authored, so a material that only has, say, a procedural
        metallic mask keeps its original high-res albedo/normal maps.
        """
        import bpy
        if source_mat.node_tree is None:
            return None
        ps = self._find_principled_bsdf(source_mat)
        if ps is None:
            return None  # custom / surface-group materials: nothing to bake here

        def _want(ch):
            return self._socket_needs_bake(ps.inputs.get(ch))

        bake_base = _want("Base Color")
        bake_norm = _want("Normal")
        bake_emit = _want("Emission Color")
        bake_mr = _want("Metallic") or _want("Roughness")
        if not (bake_base or bake_norm or bake_emit or bake_mr):
            return source_mat  # nothing procedural -> export the source as-is

        res = self._bake_resolution(source_mat)
        tag = f"{source_mat.name}__{obj.name}".replace(" ", "_").replace(".", "_")
        nt = source_mat.node_tree
        out_node = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL"), None)
        saved_surface = None
        temp_emit = None
        try:
            self._begin_bake_session()
            self._ensure_uv(obj)

            target = self._bake_targets.get(source_mat.name)
            if target is None or target.name not in nt.nodes:
                target = nt.nodes.new("ShaderNodeTexImage")
                target.location = (-1400, 700)
                self._bake_targets[source_mat.name] = target
                self._temp_nodes.append((nt, target))
            nt.nodes.active = target

            img_base = img_norm = img_emit = img_mr = None

            if bake_base:
                img_base = self._bake_pass(obj, source_mat, target, f"{tag}_basecolor", res, "DIFFUSE", False)
            if bake_norm:
                img_norm = self._bake_pass(obj, source_mat, target, f"{tag}_normal", res, "NORMAL", True)
            if bake_emit:
                img_emit = self._bake_pass(obj, source_mat, target, f"{tag}_emit", res, "EMIT", False)

            if bake_mr:
                import numpy as np
                img_rough = self._bake_pass(obj, source_mat, target, f"{tag}_rough", res, "ROUGHNESS", True)

                # Metallic has no native bake pass: route the Metallic input
                # through a temporary Emission shader and bake the EMIT pass.
                msock = ps.inputs.get("Metallic")
                if msock is not None and msock.is_linked and out_node is not None:
                    surf = out_node.inputs["Surface"]
                    saved_surface = surf.links[0].from_socket if surf.is_linked else None
                    temp_emit = nt.nodes.new("ShaderNodeEmission")
                    temp_emit.location = (300, 700)
                    nt.links.new(msock.links[0].from_socket, temp_emit.inputs["Color"])
                    nt.links.new(temp_emit.outputs["Emission"], surf)
                    img_metal = self._bake_pass(obj, source_mat, target, f"{tag}_metal", res, "EMIT", True)
                    if saved_surface is not None:
                        nt.links.new(saved_surface, surf)
                    nt.nodes.remove(temp_emit)
                    temp_emit = None
                    metal_px = np.empty(res * res * 4, dtype=np.float32)
                    img_metal.pixels.foreach_get(metal_px)
                    metal_b = metal_px[0::4]
                else:
                    mv = self._get_socket_default(ps, "Metallic")
                    metal_b = float(mv) if mv is not None else 0.0

                rough_px = np.empty(res * res * 4, dtype=np.float32)
                img_rough.pixels.foreach_get(rough_px)
                mr_px = np.empty(res * res * 4, dtype=np.float32)
                mr_px[0::4] = 0.0
                mr_px[1::4] = rough_px[0::4]   # G = roughness
                mr_px[2::4] = metal_b          # B = metallic
                mr_px[3::4] = 1.0
                img_mr = bpy.data.images.new(f"{tag}_mr", res, res, alpha=False)
                img_mr.colorspace_settings.name = "Non-Color"
                img_mr.pixels.foreach_set(mr_px)
                img_mr.update()
                self._temp_images.append(img_mr)

            # Output material: a copy of the source (so every image-backed channel
            # survives verbatim) with the procedural channels rewired to the bakes.
            work = source_mat.copy()
            work.name = f"__gltf_baked__{tag}"
            self._temp_materials.append(work)
            wnt = work.node_tree
            pw = self._find_principled_bsdf(work)

            def _img_node(image, x, y, non_color):
                node = wnt.nodes.new("ShaderNodeTexImage")
                node.image = image
                node.location = (x, y)
                if non_color:
                    image.colorspace_settings.name = "Non-Color"
                return node

            if pw is not None:
                if img_base is not None:
                    n = _img_node(img_base, -800, 300, False)
                    wnt.links.new(n.outputs["Color"], pw.inputs["Base Color"])
                if img_norm is not None:
                    n = _img_node(img_norm, -1000, -300, True)
                    nm = wnt.nodes.new("ShaderNodeNormalMap"); nm.location = (-600, -300)
                    wnt.links.new(n.outputs["Color"], nm.inputs["Color"])
                    wnt.links.new(nm.outputs["Normal"], pw.inputs["Normal"])
                if img_emit is not None:
                    n = _img_node(img_emit, -800, -600, False)
                    if "Emission Color" in pw.inputs:
                        wnt.links.new(n.outputs["Color"], pw.inputs["Emission Color"])
                    if "Emission Strength" in pw.inputs and not pw.inputs["Emission Strength"].is_linked:
                        pw.inputs["Emission Strength"].default_value = 1.0
                if img_mr is not None:
                    n = _img_node(img_mr, -800, 0, True)
                    wnt.links.new(n.outputs["Color"], pw.inputs["Metallic"])
                    wnt.links.new(n.outputs["Color"], pw.inputs["Roughness"])

            return work
        except Exception as e:  # noqa: BLE001 - never let a bake abort the export
            print(f"glTF export: bake failed for '{source_mat.name}' on '{obj.name}': {e}")
            try:
                if temp_emit is not None and out_node is not None:
                    if saved_surface is not None:
                        nt.links.new(saved_surface, out_node.inputs["Surface"])
                    nt.nodes.remove(temp_emit)
            except Exception:
                pass
            return None

    def cleanup(self) -> None:
        """Remove all temporary bake data and restore render settings. Safe to
        call multiple times; a no-op when nothing was baked."""
        import bpy
        for nt, node in self._temp_nodes:
            try:
                nt.nodes.remove(node)
            except Exception:
                pass
        self._temp_nodes.clear()
        self._bake_targets.clear()

        for me, uv_name in self._temp_uvs:
            try:
                uv = me.uv_layers.get(uv_name)
                if uv is not None:
                    me.uv_layers.remove(uv)
            except Exception:
                pass
        self._temp_uvs.clear()

        for m in self._temp_materials:
            try:
                bpy.data.materials.remove(m)
            except Exception:
                pass
        self._temp_materials.clear()

        for img in self._temp_images:
            try:
                bpy.data.images.remove(img)
            except Exception:
                pass
        self._temp_images.clear()

        for obj, hidden in self._hidden_restore:
            try:
                obj.hide_viewport = hidden
            except Exception:
                pass
        self._hidden_restore.clear()

        for obj, coll in self._temp_linked:
            try:
                coll.objects.unlink(obj)
            except Exception:
                pass
        self._temp_linked.clear()

        if self._bake_saved is not None:
            sc = bpy.context.scene
            try:
                sc.render.engine = self._bake_saved["engine"]
            except Exception:
                pass
            if self._bake_saved.get("samples") is not None:
                try:
                    sc.cycles.samples = self._bake_saved["samples"]
                except Exception:
                    pass
            try:
                vl_objs = bpy.context.view_layer.objects
                for o in vl_objs:
                    o.select_set(o in self._bake_saved.get("selected", []))
                vl_objs.active = self._bake_saved.get("active")
            except Exception:
                pass
            self._bake_saved = None

    def _extract(self, blender_material: "bpy.types.Material") -> Material:
        pbr = None
        normal_texture = None
        emissive_texture = None
        emissive_factor = None
        emissive_strength = None
        alpha_mode = None
        alpha_cutoff = None
        double_sided = None
        layers = None
        base_extra = None
        principled = None

        stack_node = self._find_bsdf_stack_node(blender_material)
        if stack_node is not None:
            # BSDFStackNode drives Material Output.Surface. Its Principled BSDF
            # is internal to the node group, so _find_principled_bsdf won't see
            # it. Layer 0 becomes the base material; layers 1..N become the
            # CUSTOM_materials_layers extension.
            (
                pbr, normal_texture, emissive_texture, emissive_factor,
                alpha_mode, alpha_cutoff, layers, base_extra,
            ) = self._extract_from_bsdf_stack(blender_material, stack_node)
        else:
            principled = self._find_principled_bsdf(blender_material)

            if principled is not None:
                pbr = self._gather_pbr(principled)
                normal_texture = self._gather_normal(principled)
                emissive_texture, emissive_factor, emissive_strength = (
                    self._gather_emission(principled)
                )
                alpha_mode, alpha_cutoff = self._gather_alpha(blender_material, principled)

                # A Bump node feeding Normal may carry a Height (displacement)
                # map that core glTF can't hold -> extension.base.
                h, b = self._bump_info_from_socket(principled.inputs.get("Normal"))
                if h is not None or b is not None:
                    base_extra = {}
                    if h is not None:
                        base_extra["heightTexture"] = h
                    if b is not None:
                        base_extra["bump"] = b
            else:
                # No Principled BSDF: try to recover base color + alpha from a
                # custom shader group plugged directly into Material Output.Surface
                # (e.g. tree-leaf shaders).
                pbr, alpha_mode, alpha_cutoff = self._gather_from_surface_group(blender_material)

        if blender_material.use_backface_culling is False:
            double_sided = True

        # KHR_materials_unlit
        extensions = None
        gltf_props = getattr(blender_material, "gltf_props", None)
        if gltf_props and gltf_props.unlit:
            extensions = {EXT_MATERIALS_UNLIT: {}}
            self.extensions_used.add(EXT_MATERIALS_UNLIT)

        # CUSTOM_materials_layers
        if layers or base_extra:
            if extensions is None:
                extensions = {}
            ext_dict: dict = {}
            if layers:
                ext_dict["layers"] = layers
            if base_extra:
                ext_dict["base"] = base_extra
            extensions[EXT_MATERIALS_LAYERS] = ext_dict
            self.extensions_used.add(EXT_MATERIALS_LAYERS)

        # KHR_materials_emissive_strength: emission beyond [0,1] is carried by
        # the extension while emissiveFactor stays clamped.
        if emissive_strength is not None:
            if extensions is None:
                extensions = {}
            extensions[EXT_EMISSIVE_STRENGTH] = {
                "emissiveStrength": emissive_strength,
            }
            self.extensions_used.add(EXT_EMISSIVE_STRENGTH)

        # KHR_materials_clearcoat / KHR_materials_sheen: the Principled BSDF's
        # Coat and Sheen layers. Only emitted for the standard Principled path
        # (the BSDFStack path routes its coat/sheen through CUSTOM_materials_layers).
        if principled is not None:
            clearcoat = self._gather_clearcoat(principled)
            if clearcoat is not None:
                if extensions is None:
                    extensions = {}
                extensions[EXT_MATERIALS_CLEARCOAT] = clearcoat
                self.extensions_used.add(EXT_MATERIALS_CLEARCOAT)

            sheen = self._gather_sheen(principled)
            if sheen is not None:
                if extensions is None:
                    extensions = {}
                extensions[EXT_MATERIALS_SHEEN] = sheen
                self.extensions_used.add(EXT_MATERIALS_SHEEN)

        return Material(
            name=blender_material.name,
            pbr_metallic_roughness=pbr,
            normal_texture=normal_texture,
            emissive_texture=emissive_texture,
            emissive_factor=emissive_factor,
            alpha_mode=alpha_mode,
            alpha_cutoff=alpha_cutoff,
            double_sided=double_sided,
            extensions=extensions,
        )

    def _find_principled_bsdf(
        self, blender_material: "bpy.types.Material"
    ) -> "bpy.types.ShaderNodeBsdfPrincipled | None":
        if not blender_material.use_nodes or blender_material.node_tree is None:
            return None

        for node in blender_material.node_tree.nodes:
            if node.type == "BSDF_PRINCIPLED":
                return node
        return None

    def _get_socket_default(self, node: "bpy.types.ShaderNode", name: str):
        """Get the default value of a socket input."""
        socket = node.inputs.get(name)
        if socket is None:
            return None
        return socket.default_value

    def _get_connected_image_node(
        self, node: "bpy.types.ShaderNode", socket_name: str
    ) -> "bpy.types.ShaderNodeTexImage | None":
        """Resolve `node.inputs[socket_name]` to an upstream Image Texture,
        following pass-through nodes (Reroute, Normal Map, Group boundaries).
        """
        socket = node.inputs.get(socket_name)
        return self._walk_to_image(socket)

    def _walk_to_image(
        self, socket, _group_stack=None, _visited=None, _depth=0,
    ) -> "bpy.types.ShaderNodeTexImage | None":
        """Generic socket walker that returns the first Image Texture node
        reachable upstream through Reroute / Normal Map / node-group I/O.
        `_group_stack` tracks GROUP nodes we've descended into so we can hop
        back out via GROUP_INPUT.
        """
        if socket is None or _depth > 16:
            return None
        if not socket.is_linked:
            return None
        if _visited is None:
            _visited = set()

        link = socket.links[0]
        upstream = link.from_node
        from_socket = link.from_socket
        key = (id(upstream), getattr(from_socket, "identifier", from_socket.name))
        if key in _visited:
            return None
        _visited.add(key)

        t = upstream.type
        if t == "TEX_IMAGE":
            return upstream
        if t == "REROUTE":
            return self._walk_to_image(
                upstream.inputs[0] if upstream.inputs else None,
                _group_stack, _visited, _depth + 1,
            )
        if t == "NORMAL_MAP":
            return self._walk_to_image(
                upstream.inputs.get("Color"),
                _group_stack, _visited, _depth + 1,
            )
        if t == "BUMP":
            # A Bump node combines a Height (bump/displacement) map with an
            # optional tangent-space Normal map. The tangent-space normal lives
            # on the `Normal` input; the Height map is captured separately as
            # heightTexture (see _layer_bump_info). For normalTexture purposes,
            # follow the Normal input.
            return self._walk_to_image(
                upstream.inputs.get("Normal"),
                _group_stack, _visited, _depth + 1,
            )
        if t == "GROUP":
            tree = getattr(upstream, "node_tree", None)
            if tree is None:
                return None
            gout = next((n for n in tree.nodes if n.type == "GROUP_OUTPUT"), None)
            if gout is None:
                return None
            inner = gout.inputs.get(from_socket.name)
            if inner is None:
                # Fall back to matching by output index
                for i, o in enumerate(upstream.outputs):
                    if o is from_socket and i < len(gout.inputs):
                        inner = gout.inputs[i]
                        break
            if inner is None:
                return None
            return self._walk_to_image(
                inner, (_group_stack or ()) + (upstream,), _visited, _depth + 1,
            )
        if t == "GROUP_INPUT":
            if not _group_stack:
                return None
            parent = _group_stack[-1]
            parent_in = parent.inputs.get(from_socket.name)
            if parent_in is None:
                return None
            return self._walk_to_image(
                parent_in, _group_stack[:-1], _visited, _depth + 1,
            )
        if t in ("MIX", "MIX_RGB"):
            # Best-effort: return whichever color input traces to an image.
            # Skip the Factor/Mask input. Try inputs in declared order; first
            # hit wins. Used for Ucupaint clamps and unbaked layer chains.
            for inp in upstream.inputs:
                n = inp.name.lower()
                if n in ("fac", "factor"):
                    continue
                # MIX node has typed sockets; only follow color-ish ones
                if hasattr(inp, "type") and inp.type not in ("RGBA", "VECTOR"):
                    continue
                if not inp.is_linked:
                    continue
                hit = self._walk_to_image(
                    inp, _group_stack, _visited, _depth + 1,
                )
                if hit is not None:
                    return hit
        return None

    def _gather_pbr(
        self, principled: "bpy.types.ShaderNodeBsdfPrincipled"
    ) -> MaterialPBRMetallicRoughness:
        # Base color
        base_color_socket = principled.inputs.get("Base Color")
        base_color_factor, base_color_texture = self._read_color_socket(base_color_socket)

        # Metallic / Roughness scalar factors. When a socket is linked Blender
        # ignores its default_value, so the glTF factor must stay at the spec
        # default of 1.0 (write None). Otherwise the stale socket default would
        # be multiplied into the texture on a spec-correct import — e.g. halving
        # a roughness map that sits at a 0.5 default.
        metallic_socket = principled.inputs.get("Metallic")
        if metallic_socket is not None and metallic_socket.is_linked:
            metallic_factor = None
        else:
            metallic = self._get_socket_default(principled, "Metallic")
            metallic_factor = float(metallic) if metallic is not None else None

        roughness_socket = principled.inputs.get("Roughness")
        if roughness_socket is not None and roughness_socket.is_linked:
            roughness_factor = None
        else:
            roughness = self._get_socket_default(principled, "Roughness")
            roughness_factor = float(roughness) if roughness is not None else None

        # Metallic/Roughness texture (if connected)
        mr_texture = None
        mr_node = self._get_connected_image_node(principled, "Metallic")
        if mr_node is None:
            mr_node = self._get_connected_image_node(principled, "Roughness")
        if mr_node:
            mr_texture = self.texture_exporter.gather_texture_info(mr_node)

        return MaterialPBRMetallicRoughness(
            base_color_factor=base_color_factor,
            base_color_texture=base_color_texture,
            metallic_factor=metallic_factor,
            roughness_factor=roughness_factor,
            metallic_roughness_texture=mr_texture,
        )

    def _gather_normal(
        self, principled: "bpy.types.ShaderNodeBsdfPrincipled"
    ) -> NormalTextureInfo | None:
        image_node = self._get_connected_image_node(principled, "Normal")
        if image_node is None:
            return None

        tex_info = self.texture_exporter.gather_texture_info(image_node)
        if tex_info is None:
            return None

        # Get normal strength from Normal Map node
        scale = None
        normal_socket = principled.inputs.get("Normal")
        if normal_socket and normal_socket.is_linked:
            normal_map_node = normal_socket.links[0].from_node
            if normal_map_node.type == "NORMAL_MAP":
                strength = normal_map_node.inputs.get("Strength")
                if strength and strength.default_value != 1.0:
                    scale = float(strength.default_value)

        return NormalTextureInfo(
            index=tex_info.index,
            tex_coord=tex_info.tex_coord,
            scale=scale,
            extensions=tex_info.extensions,
        )

    def _gather_clearcoat(
        self, principled: "bpy.types.ShaderNodeBsdfPrincipled"
    ) -> dict | None:
        """KHR_materials_clearcoat from the Principled BSDF Coat inputs.

        Maps Coat Weight -> clearcoatFactor/clearcoatTexture (R),
        Coat Roughness -> clearcoatRoughnessFactor/clearcoatRoughnessTexture (G),
        Coat Normal -> clearcoatNormalTexture. glTF clearcoat has no IOR or tint,
        so Coat IOR / Coat Tint are intentionally dropped.
        """
        weight_socket = principled.inputs.get("Coat Weight")
        if weight_socket is None:
            return None  # Blender too old to expose a Coat layer

        weight_val = self._get_socket_default(principled, "Coat Weight")
        weight_val = float(weight_val) if weight_val is not None else 0.0

        weight_node = self._get_connected_image_node(principled, "Coat Weight")
        rough_node = self._get_connected_image_node(principled, "Coat Roughness")
        normal_node = self._get_connected_image_node(principled, "Coat Normal")

        # Inactive coat: zero weight, nothing linked -> no extension.
        if (
            weight_val == 0.0 and not weight_socket.is_linked
            and weight_node is None and rough_node is None and normal_node is None
        ):
            return None

        ext: dict = {}
        # A linked socket's default_value is ignored by Blender, so the glTF
        # factor must stay at 1.0 to avoid scaling the texture down on import.
        if weight_socket.is_linked:
            ext["clearcoatFactor"] = 1.0
        elif weight_val != 0.0:  # spec default is 0.0
            ext["clearcoatFactor"] = weight_val

        rough_socket = principled.inputs.get("Coat Roughness")
        if rough_socket is not None:
            if rough_socket.is_linked:
                ext["clearcoatRoughnessFactor"] = 1.0
            else:
                rv = self._get_socket_default(principled, "Coat Roughness")
                if rv is not None and float(rv) != 0.0:
                    ext["clearcoatRoughnessFactor"] = float(rv)

        if weight_node is not None:
            ti = self.texture_exporter.gather_texture_info(weight_node)
            if ti is not None:
                ext["clearcoatTexture"] = ti
        if rough_node is not None:
            ti = self.texture_exporter.gather_texture_info(rough_node)
            if ti is not None:
                ext["clearcoatRoughnessTexture"] = ti
        if normal_node is not None:
            nti = self._gather_normal_for_socket(principled, "Coat Normal")
            if nti is not None:
                ext["clearcoatNormalTexture"] = nti

        return ext or None

    def _gather_sheen(
        self, principled: "bpy.types.ShaderNodeBsdfPrincipled"
    ) -> dict | None:
        """KHR_materials_sheen from the Principled BSDF Sheen inputs.

        glTF stores a sheen *color*; Blender splits it into Sheen Weight
        (scalar) x Sheen Tint (color), so sheenColorFactor = tint * weight.
        Sheen Roughness -> sheenRoughnessFactor (texture uses the A channel).
        """
        weight_socket = principled.inputs.get("Sheen Weight")
        if weight_socket is None:
            return None

        weight_val = self._get_socket_default(principled, "Sheen Weight")
        weight_val = float(weight_val) if weight_val is not None else 0.0

        color_node = self._get_connected_image_node(principled, "Sheen Tint")
        rough_node = self._get_connected_image_node(principled, "Sheen Roughness")

        if (
            weight_val == 0.0 and not weight_socket.is_linked
            and color_node is None and rough_node is None
        ):
            return None

        ext: dict = {}
        # Weight scales the tint into the color factor. A linked weight socket's
        # default is ignored, so fold a unit weight and let the texture drive it.
        wscale = 1.0 if weight_socket.is_linked else weight_val
        tint = self._get_socket_default(principled, "Sheen Tint")
        if tint is not None and hasattr(tint, "__len__") and len(tint) >= 3:
            color = [float(tint[0]) * wscale, float(tint[1]) * wscale, float(tint[2]) * wscale]
        else:
            # Scalar/!color tint (older Blender) or none: greyscale by weight.
            color = [wscale, wscale, wscale]
        if color != [0.0, 0.0, 0.0]:  # spec default is [0,0,0]
            ext["sheenColorFactor"] = color

        rough_socket = principled.inputs.get("Sheen Roughness")
        if rough_socket is not None:
            if rough_socket.is_linked:
                ext["sheenRoughnessFactor"] = 1.0
            else:
                rv = self._get_socket_default(principled, "Sheen Roughness")
                if rv is not None and float(rv) != 0.0:
                    ext["sheenRoughnessFactor"] = float(rv)

        if color_node is not None:
            ti = self.texture_exporter.gather_texture_info(color_node)
            if ti is not None:
                ext["sheenColorTexture"] = ti
        if rough_node is not None:
            ti = self.texture_exporter.gather_texture_info(rough_node)
            if ti is not None:
                ext["sheenRoughnessTexture"] = ti

        return ext or None

    def _gather_normal_for_socket(
        self, node: "bpy.types.ShaderNode", socket_name: str
    ) -> NormalTextureInfo | None:
        """NormalTextureInfo for an arbitrary normal socket (e.g. "Coat Normal"),
        reading the Normal Map node's Strength as the glTF scale."""
        image_node = self._get_connected_image_node(node, socket_name)
        if image_node is None:
            return None
        tex_info = self.texture_exporter.gather_texture_info(image_node)
        if tex_info is None:
            return None
        scale = None
        socket = node.inputs.get(socket_name)
        if socket and socket.is_linked:
            src = socket.links[0].from_node
            if src.type == "NORMAL_MAP":
                strength = src.inputs.get("Strength")
                if strength and strength.default_value != 1.0:
                    scale = float(strength.default_value)
        return NormalTextureInfo(
            index=tex_info.index,
            tex_coord=tex_info.tex_coord,
            scale=scale,
            extensions=tex_info.extensions,
        )

    def _gather_emission(
        self, principled: "bpy.types.ShaderNodeBsdfPrincipled"
    ) -> tuple["TextureInfo | None", list[float] | None, float | None]:
        """Returns (emissive_texture, emissive_factor, emissive_strength).
        `emissive_strength` is set (> 1.0) when the material needs the
        KHR_materials_emissive_strength extension."""
        emission_strength = self._get_socket_default(principled, "Emission Strength")
        strength = float(emission_strength) if emission_strength is not None else 1.0

        # Textured emission: the texture carries the color; the socket default
        # is stale once linked, so only the strength scales the factor.
        image_node = self._get_connected_image_node(principled, "Emission Color")
        if image_node is not None:
            emissive_texture = self.texture_exporter.gather_texture_info(image_node)
            if emissive_texture is not None:
                if strength == 0.0:
                    return None, None, None
                factor = [min(strength, 1.0)] * 3
                return (
                    emissive_texture,
                    factor,
                    strength if strength > 1.0 else None,
                )

        emission_color = self._get_socket_default(principled, "Emission Color")
        if emission_color is None:
            return None, None, None

        # Check if emission is effectively zero
        r, g, b = float(emission_color[0]), float(emission_color[1]), float(emission_color[2])
        if (r * strength == 0 and g * strength == 0 and b * strength == 0):
            return None, None, None

        emissive_factor = [r * strength, g * strength, b * strength]
        peak = max(emissive_factor)
        if peak > 1.0:
            # emissiveFactor must stay within [0,1]; move the scale into the
            # KHR_materials_emissive_strength extension.
            emissive_factor = [c / peak for c in emissive_factor]
            return None, emissive_factor, peak

        return None, emissive_factor, None

    @staticmethod
    def _alpha_socket_used(socket) -> bool:
        """Whether an Alpha socket actually affects the material: linked, or
        carrying a non-1.0 constant."""
        if socket is None:
            return False
        if socket.is_linked:
            return True
        try:
            return float(socket.default_value) < 1.0
        except (TypeError, ValueError):
            return False

    def _alpha_mode_for_material(
        self,
        blender_material: "bpy.types.Material",
        alpha_socket,
    ) -> tuple[str | None, float | None]:
        """Resolve (alpha_mode, alpha_cutoff) from the material's render method
        and the shader's Alpha socket. Returns (None, None) for OPAQUE.

        Blender 4.2+ only exposes surface_render_method ("DITHERED"/"BLENDED"),
        so whether alpha is used at all must come from the Alpha socket itself.
        """
        if not self._alpha_socket_used(alpha_socket):
            return None, None  # OPAQUE

        render_method = getattr(blender_material, "surface_render_method", None)
        if render_method is not None:
            if render_method == "BLENDED":
                return "BLEND", None
            # "DITHERED" maps closest to MASK
            threshold = getattr(blender_material, "alpha_threshold", 0.5)
            # 0.5 is the glTF spec default for alphaCutoff
            cutoff = float(threshold) if threshold != 0.5 else None
            return "MASK", cutoff

        # Legacy fallback (Blender < 4.2): blend_method
        blend_method = getattr(blender_material, "blend_method", "OPAQUE")
        if blend_method == "OPAQUE":
            return None, None
        if blend_method in ("CLIP", "HASHED"):
            threshold = getattr(blender_material, "alpha_threshold", 0.5)
            cutoff = float(threshold) if threshold != 0.5 else None
            return "MASK", cutoff
        return "BLEND", None

    def _gather_alpha(
        self,
        blender_material: "bpy.types.Material",
        principled: "bpy.types.ShaderNodeBsdfPrincipled",
    ) -> tuple[str | None, float | None]:
        return self._alpha_mode_for_material(
            blender_material, principled.inputs.get("Alpha"),
        )

    # Common input names that custom shader groups use for the diffuse/base
    # color and alpha sockets. Ordered by preference.
    _GROUP_BASE_COLOR_INPUTS = ("Base Color", "BaseColor", "Color", "Diffuse", "Albedo")
    _GROUP_ALPHA_INPUTS = ("Alpha", "Opacity")
    _GROUP_NORMAL_INPUTS = ("Normal", "Normal Map")

    def _gather_from_surface_group(
        self, blender_material: "bpy.types.Material",
    ) -> tuple["MaterialPBRMetallicRoughness | None", str | None, float | None]:
        """Fallback for materials with no Principled BSDF: walk
        Material Output.Surface, and if it's a custom shader group, try to
        recover (base color factor + texture, alpha mode) from common input
        names (Diffuse, Color, Alpha, …).
        Returns (pbr or None, alpha_mode, alpha_cutoff).
        """
        if not blender_material.use_nodes or blender_material.node_tree is None:
            return None, None, None

        out_node = next(
            (n for n in blender_material.node_tree.nodes if n.type == "OUTPUT_MATERIAL"),
            None,
        )
        if out_node is None:
            return None, None, None
        surface = out_node.inputs.get("Surface")
        if surface is None or not surface.is_linked:
            return None, None, None

        group_node = surface.links[0].from_node
        if group_node.type != "GROUP" or getattr(group_node, "node_tree", None) is None:
            return None, None, None

        bc_socket = None
        for name in self._GROUP_BASE_COLOR_INPUTS:
            s = group_node.inputs.get(name)
            if s is not None:
                bc_socket = s
                break
        bc_factor, bc_tex = self._read_color_socket(bc_socket) if bc_socket else (None, None)

        # Alpha: only emit a mode if the group exposes an alpha socket.
        alpha_mode = None
        alpha_cutoff = None
        for name in self._GROUP_ALPHA_INPUTS:
            a = group_node.inputs.get(name)
            if a is None:
                continue
            alpha_mode, alpha_cutoff = self._alpha_mode_for_material(blender_material, a)
            break

        if bc_factor is None and bc_tex is None:
            return None, alpha_mode, alpha_cutoff

        pbr = MaterialPBRMetallicRoughness(
            base_color_factor=bc_factor,
            base_color_texture=bc_tex,
        )
        return pbr, alpha_mode, alpha_cutoff

    # ------------------------------------------------------------------
    # CUSTOM_materials_layers — BSDFStackNode authoring path
    # ------------------------------------------------------------------

    def _find_bsdf_stack_node(self, blender_material):
        """Return the BSDFStackNode feeding Material Output.Surface, if any."""
        if not blender_material.use_nodes or blender_material.node_tree is None:
            return None
        out = next(
            (n for n in blender_material.node_tree.nodes
             if n.type == "OUTPUT_MATERIAL"),
            None,
        )
        if out is None:
            return None
        surface = out.inputs.get("Surface")
        if surface is None or not surface.is_linked:
            return None
        node = surface.links[0].from_node
        if getattr(node, "bl_idname", "") == BSDF_STACK_NODE_IDNAME:
            return node
        return None

    def _layer_socket(self, node, layer_index, channel_name):
        """Look up a BSDFStackNode input socket by (layer, channel name)."""
        idx = layer_index * N_CH + CH_TO_IDX[channel_name]
        if 0 <= idx < len(node.inputs):
            return node.inputs[idx]
        return None

    def _layer_image_tex(self, node, layer_index, channel_name):
        """TextureInfo for the image feeding a layer's channel socket, or None."""
        socket = self._layer_socket(node, layer_index, channel_name)
        img = self._walk_to_image(socket) if socket is not None else None
        if img is None:
            return None
        return self.texture_exporter.gather_texture_info(img)

    def _layer_float(self, node, layer_index, channel_name):
        socket = self._layer_socket(node, layer_index, channel_name)
        if socket is None:
            return None
        try:
            return float(socket.default_value)
        except (TypeError, ValueError):
            return None

    def _layer_scalar_factor(self, node, layer_index, channel_name):
        """Scalar factor for a metallic/roughness-style channel: the socket's
        default_value, or None when the socket is linked. A linked socket's
        default is ignored by Blender, so the glTF factor must stay at its 1.0
        default to avoid double-applying it on top of the texture at import.
        """
        socket = self._layer_socket(node, layer_index, channel_name)
        if socket is None or socket.is_linked:
            return None
        try:
            return float(socket.default_value)
        except (TypeError, ValueError):
            return None

    def _layer_color_rgb(self, node, layer_index, channel_name):
        socket = self._layer_socket(node, layer_index, channel_name)
        if socket is None:
            return None
        v = socket.default_value
        return [float(v[0]), float(v[1]), float(v[2])]

    def _layer_pbr_dict(self, node, i):
        """Build a pbrMetallicRoughness dict for layer i (factors + textures)."""
        pbr: dict = {}
        rgb = self._layer_color_rgb(node, i, "Color")
        alpha = self._layer_float(node, i, "Alpha")
        a = alpha if alpha is not None else 1.0
        if rgb is not None and (rgb != [1.0, 1.0, 1.0] or a != 1.0):
            pbr["baseColorFactor"] = [rgb[0], rgb[1], rgb[2], a]
        bc_tex = self._layer_image_tex(node, i, "Color")
        if bc_tex is not None:
            pbr["baseColorTexture"] = bc_tex
        m = self._layer_scalar_factor(node, i, "Metallic")
        if m is not None and m != 1.0:
            pbr["metallicFactor"] = m
        r = self._layer_scalar_factor(node, i, "Roughness")
        if r is not None and r != 1.0:
            pbr["roughnessFactor"] = r
        mr_tex = (
            self._layer_image_tex(node, i, "Metallic")
            or self._layer_image_tex(node, i, "Roughness")
        )
        if mr_tex is not None:
            pbr["metallicRoughnessTexture"] = mr_tex
        return pbr or None

    def _layer_normal_info(self, node, i):
        """NormalTextureInfo for layer i's Normal channel, or None."""
        socket = self._layer_socket(node, i, "Normal")
        if socket is None or not socket.is_linked:
            return None
        img = self._walk_to_image(socket)
        if img is None:
            return None
        ti = self.texture_exporter.gather_texture_info(img)
        if ti is None:
            return None
        scale = self._normal_map_scale(socket)
        return NormalTextureInfo(
            index=ti.index, tex_coord=ti.tex_coord,
            scale=scale, extensions=ti.extensions,
        )

    def _normal_map_scale(self, socket):
        """Return the Normal Map node's Strength (!= 1.0) feeding `socket`, or
        None. Looks through a Bump node (Normal input) if present.
        """
        if socket is None or not socket.is_linked:
            return None
        src = socket.links[0].from_node
        if src.type == "BUMP":
            inner = src.inputs.get("Normal")
            if inner is not None and inner.is_linked:
                src = inner.links[0].from_node
            else:
                return None
        if src.type == "NORMAL_MAP":
            strength = src.inputs.get("Strength")
            if strength is not None and strength.default_value != 1.0:
                return float(strength.default_value)
        return None

    def _layer_bump_info(self, node, i):
        """If layer i's Normal socket is driven by a Bump node carrying a Height
        (depth/displacement) map, return (height_texture_info, bump_dict);
        otherwise (None, None). `bump_dict` = {strength, distance}.
        """
        return self._bump_info_from_socket(self._layer_socket(node, i, "Normal"))

    def _bump_info_from_socket(self, socket):
        """If `socket` is driven by a Bump node carrying a Height (depth/
        displacement) map, return (height_texture_info, bump_dict); otherwise
        (None, None). `bump_dict` = {strength, distance}.
        """
        if socket is None or not socket.is_linked:
            return None, None
        src = socket.links[0].from_node
        if src.type != "BUMP":
            return None, None
        height_socket = src.inputs.get("Height")
        himg = self._walk_to_image(height_socket) if height_socket is not None else None
        if himg is None:
            # A Bump node with no Height map carries no depth data to preserve.
            return None, None
        height_ti = self.texture_exporter.gather_texture_info(himg)
        if height_ti is None:
            return None, None
        bump = {}
        strength = src.inputs.get("Strength")
        if strength is not None:
            bump["strength"] = float(strength.default_value)
        distance = src.inputs.get("Distance")
        if distance is not None:
            bump["distance"] = float(distance.default_value)
        return height_ti, (bump or None)

    def _layer_emission(self, node, i):
        """Return (emissive_factor list|None, TextureInfo|None) for layer i."""
        rgb = self._layer_color_rgb(node, i, "Emission Color")
        strength = self._layer_float(node, i, "Emission Strength")
        if rgb is None:
            return None, None
        s = strength if strength is not None else 1.0
        factor = [rgb[0] * s, rgb[1] * s, rgb[2] * s]
        tex = self._layer_image_tex(node, i, "Emission Color")
        if factor == [0.0, 0.0, 0.0] and tex is None:
            return None, None
        return factor, tex

    def _layer_subsurface(self, node, i):
        """Return {weight, radius} dict for layer i, or None when inactive."""
        weight = self._layer_float(node, i, "Subsurface Weight")
        if weight is None or weight == 0.0:
            return None
        out = {"weight": weight}
        radius = self._layer_color_rgb(node, i, "Subsurface Radius")
        if radius is not None:
            out["radius"] = radius
        return out

    def _extract_from_bsdf_stack(self, blender_material, node):
        """Map a BSDFStackNode to (pbr, normal, emis_tex, emis_factor,
        alpha_mode, alpha_cutoff, layers, base_extra). Layer 0 -> base material;
        layers 1..N -> the CUSTOM_materials_layers `layers` array. `base_extra`
        carries layer-0 data with no core glTF slot (heightTexture/bump) and
        becomes extension.base.
        """
        n_layers = len(node.layers)

        # ---- Layer 0 -> base material ----
        rgb = self._layer_color_rgb(node, 0, "Color") or [1.0, 1.0, 1.0]
        alpha = self._layer_float(node, 0, "Alpha")
        a = alpha if alpha is not None else 1.0
        base_factor = [rgb[0], rgb[1], rgb[2], a]
        metallic = self._layer_scalar_factor(node, 0, "Metallic")
        roughness = self._layer_scalar_factor(node, 0, "Roughness")
        pbr = MaterialPBRMetallicRoughness(
            base_color_factor=(
                base_factor if base_factor != [1.0, 1.0, 1.0, 1.0] else None
            ),
            base_color_texture=self._layer_image_tex(node, 0, "Color"),
            metallic_factor=metallic,
            roughness_factor=roughness,
            metallic_roughness_texture=(
                self._layer_image_tex(node, 0, "Metallic")
                or self._layer_image_tex(node, 0, "Roughness")
            ),
        )
        normal_texture = self._layer_normal_info(node, 0)
        emissive_factor, emissive_texture = self._layer_emission(node, 0)
        alpha_mode, alpha_cutoff = self._alpha_mode_for_material(
            blender_material, self._layer_socket(node, 0, "Alpha"),
        )

        # Base height/bump has no core glTF slot -> extension.base.
        base_extra: dict = {}
        base_height, base_bump = self._layer_bump_info(node, 0)
        if base_height is not None:
            base_extra["heightTexture"] = base_height
        if base_bump is not None:
            base_extra["bump"] = base_bump

        # ---- Layers 1..N -> extension ----
        layers: list[dict] = []
        for i in range(1, n_layers):
            layer = self._gather_bsdf_layer(node, i)
            if layer is not None:
                layers.append(layer)

        return (
            pbr, normal_texture, emissive_texture, emissive_factor,
            alpha_mode, alpha_cutoff, (layers or None), (base_extra or None),
        )

    def _gather_bsdf_layer(self, node, i):
        """Build one extension layer dict from BSDFStackNode layer i (i >= 1)."""
        layer: dict = {}
        props = node.layers[i] if i < len(node.layers) else None

        if props is not None and props.layer_name:
            layer["name"] = props.layer_name

        pbr = self._layer_pbr_dict(node, i)
        if pbr:
            layer["pbrMetallicRoughness"] = pbr

        normal = self._layer_normal_info(node, i)
        if normal is not None:
            layer["normalTexture"] = normal

        height_ti, bump = self._layer_bump_info(node, i)
        if height_ti is not None:
            layer["heightTexture"] = height_ti
        if bump is not None:
            layer["bump"] = bump

        emissive_factor, emissive_texture = self._layer_emission(node, i)
        if emissive_factor is not None:
            layer["emissiveFactor"] = emissive_factor
        if emissive_texture is not None:
            layer["emissiveTexture"] = emissive_texture

        subsurface = self._layer_subsurface(node, i)
        if subsurface is not None:
            layer["subsurface"] = subsurface

        # Mask (optional): omit for a full-opacity unmasked layer.
        mask = self._gather_layer_mask(self._layer_socket(node, i, "Mask"))
        if mask is not None:
            layer["mask"] = mask

        if props is not None:
            bm = str(props.blend_mode).upper()
            if bm in _VALID_BLEND_MODES and bm != "MIX":
                layer["blendMode"] = bm
            if props.opacity != 1.0:
                layer["opacity"] = float(props.opacity)
            if not props.enabled:
                layer["enabled"] = False

        return layer

    def _read_color_socket(self, socket):
        """Resolve a color socket to (factor, TextureInfo).

        Handles the common upstream cases: Image Texture (texture wins, factor
        is the socket's local default), RGB node (read its output value as the
        factor), or unlinked (read socket default).

        When a socket is linked through pass-through nodes (Reroute, Group)
        but no image texture is reachable, the upstream output's
        `default_value` is often a stale evaluated value (commonly black for
        unevaluated group outputs). The Principled-side socket's
        `default_value` is a more reliable user-visible factor, so prefer it.
        """
        if socket is None:
            return None, None

        image_node = self._walk_to_image(socket)
        if image_node is not None:
            v = socket.default_value
            tex_info = self.texture_exporter.gather_texture_info(image_node)
            return [v[0], v[1], v[2], v[3]], tex_info

        if socket.is_linked:
            from_socket = socket.links[0].from_socket
            from_node = socket.links[0].from_node
            # For RGB-style upstreams (RGB, Value->Combine, Color attribute),
            # the output default is meaningful. For Group / Reroute / shader
            # nodes, prefer the Principled-side default.
            if getattr(from_node, "type", None) in {"RGB", "VALUE", "COMBINE_COLOR", "COMBINE_RGB"}:
                v = getattr(from_socket, "default_value", None)
                if v is not None and hasattr(v, "__len__") and len(v) >= 3:
                    a = v[3] if len(v) >= 4 else 1.0
                    return [v[0], v[1], v[2], a], None

        v = socket.default_value
        return [v[0], v[1], v[2], v[3]], None

    @staticmethod
    def _tex_info_to_dict(ti) -> dict:
        """Convert a TextureInfo to the plain dict form used by mask textures."""
        tex: dict = {"index": ti.index}
        if ti.tex_coord is not None:
            tex["texCoord"] = ti.tex_coord
        if ti.extensions:
            tex["extensions"] = ti.extensions
        return tex

    def _gather_layer_mask(self, socket) -> dict | None:
        if socket is None or not socket.is_linked:
            return None

        link = socket.links[0]
        src = link.from_node
        from_socket_name = link.from_socket.name

        if src.type == "TEX_IMAGE":
            ti = self.texture_exporter.gather_texture_info(src)
            if ti is None:
                return None
            mask = {"source": "TEXTURE", "texture": self._tex_info_to_dict(ti)}
            channel = "A" if from_socket_name == "Alpha" else "R"
            if channel != "R":
                mask["channel"] = channel
            return mask

        # Separate Color/RGB driven by an image — write the channel
        if src.type in ("SEPARATE_COLOR", "SEPRGB"):
            channel = _socket_to_channel(from_socket_name)
            color_in = src.inputs.get("Color") or src.inputs.get("Image")
            if color_in is not None and color_in.is_linked:
                inner = color_in.links[0].from_node
                if inner.type == "TEX_IMAGE":
                    ti = self.texture_exporter.gather_texture_info(inner)
                    if ti is None:
                        return None
                    mask = {"source": "TEXTURE", "texture": self._tex_info_to_dict(ti)}
                    if channel != "R":
                        mask["channel"] = channel
                    return mask
                if inner.type in ("VERTEX_COLOR", "ATTRIBUTE"):
                    return _vertex_color_mask(inner, channel)

        if src.type in ("VERTEX_COLOR", "ATTRIBUTE"):
            channel = "A" if from_socket_name == "Alpha" else "R"
            return _vertex_color_mask(src, channel)

        return None


def _socket_to_channel(name: str) -> str:
    name = name.upper()
    if name in _VALID_MASK_CHANNELS:
        return name
    if name == "RED":
        return "R"
    if name == "GREEN":
        return "G"
    if name == "BLUE":
        return "B"
    if name == "ALPHA":
        return "A"
    return "R"


def _vertex_color_mask(src, channel: str) -> dict:
    attr = ""
    if src.type == "VERTEX_COLOR":
        attr = getattr(src, "layer_name", "") or ""
    else:
        attr = getattr(src, "attribute_name", "") or ""
    mask = {"source": "VERTEX_COLOR"}
    if attr and attr != "COLOR_0":
        mask["attribute"] = attr
    if channel != "R":
        mask["channel"] = channel
    return mask
