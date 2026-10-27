# SPDX-License-Identifier: GPL-3.0-or-later
"""The Combine operator: detect source node(s), fix missing passes, build the graph.

Each Render Layer / multilayer-EXR (and each selected layer of a multilayer EXR)
is treated as an *independent* source: it gets its own uniquely-named LayerCake
group so they can be reconstructed side by side and edited independently. The
operator can act on the one selected source, or on *all* sources in the tree at
once (``all_sources``).
"""

import bpy
from bpy.props import BoolProperty, PointerProperty, StringProperty

from . import arrange
from . import compat
from . import builder
from . import passes as P

_SOURCE_TYPES = {"CompositorNodeRLayers", "CompositorNodeImage"}

# Names of the toggle options shared between the panel settings and the operator.
_OPTION_PROPS = (
    "embed_source", "auto_enable_passes", "include_extras",
    "include_crypto_object", "include_crypto_material",
    "film_transparent", "connect_viewer", "set_as_output",
)


class EXRCombinerSettings(bpy.types.PropertyGroup):
    embed_source: BoolProperty(
        name="Include Source in Group",
        description="Put a copy of the Render Layers / EXR node inside the LayerCake group so it is fully self-contained, instead of feeding it from the outside",
        default=False,
    )
    auto_enable_passes: BoolProperty(
        name="Auto-enable Missing Passes",
        description="On Render Layers: switch on the view-layer passes needed for the reconstruction (requires a re-render)",
        default=True,
    )
    include_extras: BoolProperty(
        name="Expose Extra Passes",
        description="Pass AO / Depth / Normal / Vector / Mist / Index … through as named group outputs",
        default=True,
    )
    include_crypto_object: BoolProperty(
        name="Object",
        description="Add a Cryptomatte picker for the Object layer, when present on the source",
        default=True,
    )
    include_crypto_material: BoolProperty(
        name="Material",
        description="Add a Cryptomatte picker for the Material layer, when present on the source",
        default=True,
    )
    film_transparent: BoolProperty(
        name="Film Transparent",
        description="Exclude Environment from the sum and keep alpha, matching a transparent-film render",
        default=False,
    )
    connect_viewer: BoolProperty(
        name="Connect Viewer",
        description="Wire the Combined result into a Viewer node for instant feedback",
        default=True,
    )
    set_as_output: BoolProperty(
        name="Set As Composite Output",
        description="When run on the scene's top compositor, route Combined to the Group Output (single source only — with several sources the output is ambiguous)",
        default=True,
    )


def _engine_kind(scene):
    eng = scene.render.engine
    return "CYCLES" if eng == "CYCLES" else "EEVEE"


def _scan_outputs(node):
    """Return (avail dict key->socket name, crypto list of (socket name, kind))."""
    avail, crypto = {}, []
    for s in node.outputs:
        key, crypto_kind = P.classify_socket(s.name)
        if crypto_kind is not None:
            crypto.append((s.name, crypto_kind))
        elif key is not None and key not in avail:
            avail[key] = s.name
    return avail, crypto


def _infer_engine(scene, avail):
    if P.DIFF_DIR in avail or P.GLOSS_DIR in avail or P.TRANS_DIR in avail:
        return "CYCLES"
    if P.DIFF_LIGHT in avail or P.SPEC_LIGHT in avail:
        return "EEVEE"
    return _engine_kind(scene)


# --- source detection & identity --------------------------------------------
def _all_sources(tree):
    """Every top-level Render Layers / multilayer-EXR node in this tree."""
    if tree is None:
        return []
    return [n for n in tree.nodes if n.bl_idname in _SOURCE_TYPES]


def _find_source(context):
    node = getattr(context, "active_node", None)
    if node is not None and node.bl_idname in _SOURCE_TYPES:
        return node
    tree = context.space_data.edit_tree
    if tree is not None:
        for n in tree.nodes:
            if n.select and n.bl_idname in _SOURCE_TYPES:
                return n
    return None


def _source_key(source, scene):
    """Per-NODE identity: one LayerCake per source node, independent of which
    layer / frame it currently shows. A stable ``lc_id`` (frozen on the node at
    first bake) keeps the key constant across node renames; until then we fall back
    to the node name. Keying on the node — not on scene/layer/image content — means
    switching the layer of a multilayer EXR, or duplicating an Image node, never
    clobbers or orphans a sibling LayerCake."""
    sid = source.get("lc_id")
    if sid:
        return sid
    prefix = "IMG" if source.bl_idname == "CompositorNodeImage" else "RL"
    return "%s:%s" % (prefix, source.name)


def _source_label(source, scene):
    """Readable name fragment for this source's LayerCake group / instance."""
    if source.bl_idname == "CompositorNodeImage":
        img = getattr(source, "image", None)
        base = img.name if img else "EXR"
        layer = str(getattr(source, "layer", "") or "")
        return "%s · %s" % (base, layer) if layer else base
    src_scene = source.scene or scene
    lyr = source.layer or (src_scene.view_layers[0].name if src_scene.view_layers else "Layer")
    return lyr if src_scene == scene else "%s · %s" % (src_scene.name, lyr)


def _enable_passes(view_layer, engine, include_extras):
    """Turn on the view-layer pass flags needed for reconstruction. Returns the
    list of human labels that were newly switched on."""
    keys = set()
    if engine == "CYCLES":
        for _, d, i, c in P.LIGHT_GROUPS_CYCLES:
            keys.update((d, i, c))
        keys.update((P.VOL_DIR, P.VOL_IND))
    else:
        for _, l, c in P.LIGHT_GROUPS_EEVEE:
            keys.update((l, c))
    keys.update((P.EMIT, P.ENV))
    if include_extras:
        keys.update(P.EXTRA_PASSTHROUGH)

    newly = []
    for key in keys:
        spec = P.VIEW_LAYER_FLAGS.get(key)
        if not spec:
            continue
        holder_name, attr = spec
        holder = view_layer if holder_name == "vl" else getattr(view_layer, holder_name, None)
        if holder is None or not hasattr(holder, attr):
            continue
        if not getattr(holder, attr):
            try:
                setattr(holder, attr, True)
                newly.append(P.CANON_DISPLAY.get(key, key))
            except (AttributeError, TypeError):
                pass
    if newly and hasattr(view_layer, "update_render_passes"):
        try:
            view_layer.update_render_passes()
        except RuntimeError:
            pass
    return newly


# --- crypto / embed / signature helpers --------------------------------------
def _present_crypto_kinds(crypto):
    return {kind for _, kind in crypto}


def _image_frame_state(node):
    """Snapshot an Image node's sequence/frame controls so an EXR SEQUENCE keeps
    resolving to the right frames when the node is recreated. In Blender 5.x these
    live directly on the node; on 4.x they live on ``node.image_user`` — capture
    whichever exists."""
    state = {}
    iu = getattr(node, "image_user", None)
    for a in ("frame_duration", "frame_start", "frame_offset", "frame_current",
              "use_cyclic", "use_auto_refresh"):
        if hasattr(node, a):
            state[a] = getattr(node, a)
        elif iu is not None and hasattr(iu, a):
            state[a] = getattr(iu, a)
    return state


def _source_descriptor(source, scene):
    """Describe the source node so it can be recreated (inside the group when
    embedding, or back in the tree when an embedded source must be re-exposed)."""
    if source.bl_idname == "CompositorNodeImage":
        return {
            "type": "CompositorNodeImage",
            "image": getattr(source, "image", None),
            "layer": getattr(source, "layer", None),
            "view": getattr(source, "view", None),
            "frames": _image_frame_state(source),   # EXR-sequence playback state
        }
    return {
        "type": "CompositorNodeRLayers",
        "scene": source.scene or scene,
        "layer": source.layer,
    }


def _recover_source(inst, tree):
    """Find the source node a LayerCake instance was built from: the embedded
    node inside the group, or (external mode) the node feeding its inputs."""
    if inst.node_tree is not None:
        for n in inst.node_tree.nodes:
            if n.bl_idname in _SOURCE_TYPES:
                return n
    for link in tree.links:
        if link.to_node == inst and link.from_node.bl_idname in _SOURCE_TYPES:
            return link.from_node
    return None


# --- LayerCake group identity ------------------------------------------------
def _is_parent_group(g):
    """True for a LayerCake parent group datablock (tagged, or the legacy fixed
    name from before per-source groups existed)."""
    return g is not None and (g.get("lc_role") == "parent" or g.name == builder.PARENT_GROUP)


def _existing_parent_for_key(key):
    for g in bpy.data.node_groups:
        if _is_parent_group(g) and g.get("lc_src") == key:
            return g
    return None


def _unique_group_name(label, key):
    """Deterministic, collision-safe datablock name for this source's group.
    Re-uses the name of the source's existing LayerCake group (matched by key) so
    a rebuild updates it in place even if the label changed in between."""
    existing = _existing_parent_for_key(key)
    if existing is not None:
        return existing.name
    base = "%s [%s]" % (builder.PARENT_GROUP, label) if label else builder.PARENT_GROUP
    name, i = base, 1
    while True:
        g = bpy.data.node_groups.get(name)
        if g is None or (_is_parent_group(g) and g.get("lc_src") == key):
            return name
        i += 1
        name = "%s.%03d" % (base, i)


def _layercake_instances(tree):
    """LayerCake parent-group instances inserted into this tree."""
    if tree is None:
        return []
    return [n for n in tree.nodes
            if n.bl_idname == "CompositorNodeGroup"
            and _is_parent_group(getattr(n, "node_tree", None))]


def _instance_for_key(tree, key):
    """The LayerCake instance in this tree that belongs to source ``key``."""
    for n in _layercake_instances(tree):
        if n.get("lc_src") == key:
            return n
    return None


def _descriptor(source, inst, scene, tree):
    """Lightweight, handle-free description of a build target. Node handles can be
    invalidated when a node-group datablock is removed mid-build, so targets are
    carried as names and re-fetched per source."""
    return {
        "key": _source_key(source, scene),
        "label": _source_label(source, scene),
        "src_name": source.name if source.id_data == tree else None,
        "inst_name": inst.name if inst is not None else None,
    }


def _resolve_single(context, scene, tree):
    """Descriptor for the one source the contextual button acts on: a selected
    LayerCake instance (→ update it), else the selected raw source node, else the
    first existing instance."""
    insts = _layercake_instances(tree)
    active = getattr(context, "active_node", None)
    sel_inst = active if active in insts else next((n for n in insts if n.select), None)
    if sel_inst is not None:
        src = _recover_source(sel_inst, tree)
        if src is not None:
            return _descriptor(src, sel_inst, scene, tree)
    src = _find_source(context)
    if src is not None:
        return _descriptor(src, _instance_for_key(tree, _source_key(src, scene)), scene, tree)
    if insts:
        src = _recover_source(insts[0], tree)
        if src is not None:
            return _descriptor(src, insts[0], scene, tree)
    return None


def _gather_targets(context, scene, tree):
    """Descriptors for every distinct source in the tree — existing LayerCakes and
    raw source nodes — deduped by source key."""
    out, seen = [], set()
    for inst in _layercake_instances(tree):
        src = _recover_source(inst, tree)
        if src is None:
            continue
        key = _source_key(src, scene)
        if key in seen:
            continue
        seen.add(key)
        out.append(_descriptor(src, inst, scene, tree))
    for n in _all_sources(tree):
        key = _source_key(n, scene)
        if key in seen:
            continue
        seen.add(key)
        out.append(_descriptor(n, None, scene, tree))
    return out


def _has_node_arrange(context):
    return hasattr(context.scene, "na_settings") and getattr(bpy.ops.node, "na_arrange_selected", None) is not None


def _addon_arrange_selected(context, nodes=None):
    """Run the 'node_arrange' extension on ``nodes`` (default: all) of the CURRENT
    edit_tree. Returns True on success."""
    if not _has_node_arrange(context):
        return False
    et = context.space_data.edit_tree
    if et is None:
        return False
    want = {n.name for n in nodes} if nodes is not None else None
    prev = {n.name for n in et.nodes if n.select}
    for n in et.nodes:
        n.select = True if want is None else (n.name in want)
    try:
        ok = "FINISHED" in bpy.ops.node.na_arrange_selected()
    except RuntimeError:
        ok = False
    for n in et.nodes:
        n.select = n.name in prev
    return ok


def _arrange_group_internals(context, group):
    """Arrange the group's internal nodes with node_arrange. Pushes the group onto
    the editor path and forces a redraw so the editor's edit_tree settles before
    calling the extension (mirroring node_arrange's own batch technique — a plain
    group_edit inside execute() leaves the context stale)."""
    if not _has_node_arrange(context):
        return False
    space = context.space_data
    path = space.path
    appended = 0
    try:
        path.append(group)
        appended += 1
        if space.edit_tree != group:
            path.append(group)
            appended += 1
        try:
            bpy.ops.wm.redraw_timer(type="DRAW", iterations=0)
        except RuntimeError:
            pass
        if space.edit_tree != group:
            return False
        return _addon_arrange_selected(context, None)
    except (RuntimeError, ReferenceError, TypeError):
        return False
    finally:
        for _ in range(appended):
            try:
                path.pop()
            except (RuntimeError, ReferenceError):
                pass


def _settings_signature(opts_holder):
    """A stable string of the option values the group was built with, so the
    panel can tell 'Remove' (unchanged) from 'Update' (options changed)."""
    return repr(tuple(bool(getattr(opts_holder, n)) for n in _OPTION_PROPS))


class NODE_OT_exr_combine(bpy.types.Operator):
    """Rebuild the Combined image from the selected Render Layers / EXR node — or, with All Sources, from every source in the tree, each as its own editable LayerCake"""

    bl_idname = "node.layercake_combine"
    bl_label = "Bake Combined"
    bl_options = {"REGISTER", "UNDO"}

    all_sources: BoolProperty(
        name="All Sources",
        description="Build an independent LayerCake for every Render Layers / EXR node in the tree, instead of only the selected one",
        default=False,
    )
    embed_source: BoolProperty(
        name="Include Source in Group",
        description="Put a copy of the Render Layers / EXR node inside the LayerCake group so it is fully self-contained, instead of feeding it from the outside",
        default=False,
    )
    auto_enable_passes: BoolProperty(
        name="Auto-enable Missing Passes",
        description="On Render Layers: switch on the view-layer passes needed for the reconstruction (requires a re-render)",
        default=True,
    )
    include_extras: BoolProperty(
        name="Expose Extra Passes",
        description="Pass AO / Depth / Normal / Vector / Mist / Index … through as named group outputs",
        default=True,
    )
    include_crypto_object: BoolProperty(
        name="Object",
        description="Add a Cryptomatte picker for the Object layer, when present on the source",
        default=True,
    )
    include_crypto_material: BoolProperty(
        name="Material",
        description="Add a Cryptomatte picker for the Material layer, when present on the source",
        default=True,
    )
    film_transparent: BoolProperty(
        name="Film Transparent",
        description="Exclude Environment from the sum and keep alpha, matching a transparent-film render",
        default=False,
    )
    connect_viewer: BoolProperty(
        name="Connect Viewer",
        description="Wire the Combined result into a Viewer node for instant feedback",
        default=True,
    )
    set_as_output: BoolProperty(
        name="Set As Composite Output",
        description="When run on the scene's top compositor, route Combined to the Group Output",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        sd = context.space_data
        return sd and sd.type == "NODE_EDITOR" and getattr(sd, "tree_type", "") == "CompositorNodeTree"

    def invoke(self, context, event):
        # When triggered from the panel button, seed from the scene settings.
        # (all_sources is set by the panel's own button and must survive.)
        settings = getattr(context.scene, "layercake", None)
        if settings is not None:
            for name in _OPTION_PROPS:
                setattr(self, name, getattr(settings, name))
        return self.execute(context)

    def execute(self, context):
        scene = context.scene
        tree = getattr(context.space_data, "edit_tree", None)
        if tree is None:
            self.report({"ERROR"}, "No compositor node tree active.")
            return {"CANCELLED"}
        tree_name = tree.name

        if self.all_sources:
            targets = _gather_targets(context, scene, tree)
            if not targets:
                self.report({"ERROR"}, "No Render Layers / EXR nodes found in this tree.")
                return {"CANCELLED"}
        else:
            d = _resolve_single(context, scene, tree)
            if d is None:
                self.report({"ERROR"}, "Select a Render Layers / EXR node, or a LayerCake group to update.")
                return {"CANCELLED"}
            targets = [d]

        multi = len(targets) > 1
        built, warnings = [], []
        for d in targets:
            info = self._build_one(context, scene, tree_name, d, multi)
            if info is None:
                warnings.append("Skipped '%s' — source no longer available." % d["label"])
                continue
            built.append(info)
            warnings.extend(info["warnings"])

        # Drop any LayerCake groups left orphaned by the rebuild (incl. legacy
        # single-name ones replaced by per-source groups).
        _purge_orphan_groups()

        if not built:
            self.report({"ERROR"}, "Nothing was built.")
            return {"CANCELLED"}

        if multi:
            engines = sorted({b["engine"].title() for b in built})
            self.report({"INFO"}, "LayerCake built for %d source(s) [%s]." % (len(built), ", ".join(engines)))
            if self.set_as_output:
                warnings.append("Composite output left unchanged — pick one LayerCake's Combined manually with several sources.")
        else:
            b = built[0]
            self.report({"INFO"}, "LayerCake built (%s): %s." % (b["engine"].title(), b["wired"]))
        for w in warnings:
            self.report({"WARNING"}, w)
        return {"FINISHED"}

    def _build_one(self, context, scene, tree_name, d, multi):
        """Build / update the LayerCake for one source. Re-fetches everything by
        name (handles can go stale across sources) and returns a result dict, or
        None if the source could not be found."""
        tree = bpy.data.node_groups.get(tree_name) or getattr(context.space_data, "edit_tree", None)
        if tree is None:
            return None
        key = d["key"]

        # Re-resolve the live source + its existing instance for this build.
        existing = tree.nodes.get(d["inst_name"]) if d["inst_name"] else _instance_for_key(tree, key)
        source = _recover_source(existing, tree) if existing is not None else None
        if source is None and d["src_name"]:
            source = tree.nodes.get(d["src_name"])
        if source is None:
            source = next((n for n in _all_sources(tree) if _source_key(n, scene) == key), None)
        if source is None:
            return None

        # Freeze a stable per-node identity so future renames / layer switches keep
        # updating THIS source's LayerCake in place, then key everything off it.
        if not source.get("lc_id"):
            source["lc_id"] = _source_key(source, scene)
        key = source.get("lc_id")

        # Safety net: if no instance matched by key (a cake from an older
        # content-keyed build, or an id we only just froze), adopt any instance in
        # this tree whose source node IS this one — update it in place instead of
        # leaving a mis-wired duplicate behind.
        if existing is None:
            for inst in _layercake_instances(tree):
                if _recover_source(inst, tree) is source:
                    existing = inst
                    break

        warnings = []

        # 1) Render Layers: optionally enable missing passes before scanning.
        if source.bl_idname == "CompositorNodeRLayers" and self.auto_enable_passes:
            src_scene = source.scene or scene
            vl = src_scene.view_layers.get(source.layer) or src_scene.view_layers[0]
            newly = _enable_passes(vl, _engine_kind(src_scene), self.include_extras)
            if newly:
                warnings.append(
                    "[%s] enabled %d pass(es): %s — re-render for them to contain data."
                    % (d["label"], len(newly), ", ".join(sorted(newly)))
                )

        # 2) Scan whatever the source actually exposes.
        avail, crypto = _scan_outputs(source)
        engine = _infer_engine(scene, avail)

        if not any(k in avail for k in (P.DIFF_DIR, P.DIFF_LIGHT, P.GLOSS_DIR, P.EMIT)):
            warnings.append("[%s] no light passes found — Combined may be incomplete." % d["label"])
        if source.bl_idname == "CompositorNodeImage":
            # A light pass present without its matching Color pass can't be
            # reconstructed and is silently dropped — warn for both engines.
            if engine == "EEVEE":
                for _, ll, cc in P.LIGHT_GROUPS_EEVEE:
                    if ll in avail and cc not in avail:
                        warnings.append(
                            "[%s] %s present but its Color pass is missing — that branch can't be reconstructed."
                            % (d["label"], P.CANON_DISPLAY[ll].split()[0])
                        )
            else:
                for _, dd, ii, cc in P.LIGHT_GROUPS_CYCLES:
                    if (dd in avail or ii in avail) and cc not in avail:
                        warnings.append(
                            "[%s] %s present but its Color pass is missing — that branch can't be reconstructed."
                            % (d["label"], P.CANON_DISPLAY[dd].split()[0])
                        )

        # Capture everything from the live source BEFORE any removal / rebuild —
        # both invalidate node handles.
        desc = _source_descriptor(source, scene)
        embed = desc if self.embed_source else None
        src_kind = source.bl_idname
        src_scene = (source.scene or scene) if src_kind == "CompositorNodeRLayers" else None
        src_image = getattr(source, "image", None) if src_kind == "CompositorNodeImage" else None
        src_frames = _image_frame_state(source) if src_kind == "CompositorNodeImage" else None
        src_order = [s.name for s in source.outputs]
        source_in_tree = source.id_data == tree
        external_source_name = source.name if source_in_tree else None
        existing_name = existing.name if existing is not None else None

        # Cryptomatte pickers go inside the group, for the wanted layers present.
        present_crypto = _present_crypto_kinds(crypto)
        want_kinds = []
        if self.include_crypto_object and "object" in present_crypto:
            want_kinds.append("object")
        if self.include_crypto_material and "material" in present_crypto:
            want_kinds.append("material")
        crypto_spec = {
            "kinds": want_kinds,
            "src_kind": src_kind,
            "scene": src_scene or scene,
            "image": src_image,
            "frames": src_frames,   # keep the picker on the same EXR-sequence frame
        } if want_kinds else None

        if existing is not None:
            anchor = (existing.location.x, existing.location.y)
        elif source_in_tree:
            anchor = (source.location.x + source.width + 80, source.location.y)
        else:
            anchor = (0.0, 0.0)

        opts = {
            "film_transparent": self.film_transparent or scene.render.film_transparent,
            "include_extras": self.include_extras,
        }

        gname = _unique_group_name(d["label"], key)

        # 3) Clean up just THIS source's previous LayerCake (instance + its tagged
        #    deps), leaving every other source's LayerCake untouched.
        _cleanup_source(tree, key, existing_name)

        # 4) Build the group. _fresh_group() removes the old datablock, which can
        #    invalidate our tree handle — re-acquire it by name afterwards.
        group, link_map = builder.build_parent_group(
            avail, engine, opts, src_order, embed, crypto_spec, name=gname, src_key=key
        )
        tree = bpy.data.node_groups.get(tree_name) or tree

        # In embed mode the source now lives inside the group: drop the external
        # original so there is no duplicate Render Layers / EXR node in the tree.
        if embed is not None and external_source_name:
            orig = tree.nodes.get(external_source_name)
            if orig is not None:
                tree.nodes.remove(orig)

        # In external mode we need a source node in the tree to wire to. Reuse the
        # existing one, or re-materialise it (e.g. rebuilding a formerly embedded
        # LayerCake back into external mode).
        src_for_wiring = None
        if embed is None:
            src_for_wiring = tree.nodes.get(external_source_name) if external_source_name else None
            if src_for_wiring is None:
                src_for_wiring = builder._embed_source_node(tree, desc)

        inst = compat.add_group_instance(tree, group)
        inst.location = anchor
        inst.label = "LayerCake · %s" % d["label"]
        inst["lc_src"] = key                 # tie the instance to its source
        inst.use_custom_color = True
        inst.color = (0.42, 0.24, 0.60)  # lila

        linked = 0
        if embed is None and src_for_wiring is not None:
            for in_name, src_sock_name in link_map.items():
                a = src_for_wiring.outputs.get(src_sock_name)
                b = inst.inputs.get(in_name)
                if a and b:
                    tree.links.new(a, b)
                    linked += 1

        combined_out = inst.outputs.get("Combined")

        # 5) Viewer for instant feedback — one per source so each stays
        #    independently inspectable (reuse this source's own viewer).
        viewer = None
        if self.connect_viewer and combined_out is not None:
            viewer = _get_or_make_viewer(tree, key=key, near=inst)
            tree.links.new(combined_out, viewer.inputs["Image"])

        # 6) Optionally route to the scene composite output — only when this is the
        #    sole source, since several Combined results can't all be the output.
        top = compat.get_comp_tree(scene)
        if self.set_as_output and not multi and combined_out is not None and tree == top:
            _out_node, img_in = compat.ensure_group_output_image(tree)
            if img_in is not None:
                tree.links.new(combined_out, img_in)

        # 7) Remember the options this instance was built with (panel button).
        inst["lc_sig"] = _settings_signature(self)

        # 8) Tidy layout. Prefer the 'node_arrange' extension's Sugiyama layout;
        #    fall back to our own column layout if it isn't available.
        chain = [n for n in (src_for_wiring, inst, viewer) if n is not None]
        anchor_node = src_for_wiring or inst
        arrange.arrange(tree, chain, origin=(anchor_node.location.x, anchor_node.location.y))
        # Prefer the node_arrange extension's Sugiyama layout.
        _arrange_group_internals(context, group)             # inside the group
        _addon_arrange_selected(context, chain)              # top-level region

        wired = "embedded source" if embed is not None else ("%d passes wired" % linked)
        return {"engine": engine, "wired": wired, "warnings": warnings, "label": d["label"]}


def _get_or_make_viewer(tree, key=None, near=None):
    """A Viewer node for this source (tagged by ``key``). Reuses this source's
    viewer if present; otherwise falls back to any LayerCake / existing viewer so
    we don't multiply them needlessly, and only creates one as a last resort."""
    cands = [n for n in tree.nodes if n.bl_idname == "CompositorNodeViewer"]
    v = None
    if key is not None:
        v = next((n for n in cands if n.get("lc_src") == key), None)
    if v is None and key is None:
        v = next((n for n in cands if n.get("lc_dep")), None) or (cands[0] if cands else None)
    if v is None:
        v = compat.add_viewer(tree)
        v["lc_dep"] = 1
        if near is not None:
            v.location = (near.location.x + near.width + 80, near.location.y)
    if key is not None:
        v["lc_src"] = key
    return v


def _cleanup_source(tree, key, existing_name=None):
    """Remove only one source's LayerCake instance (matched by key, or by the
    explicit ``existing_name`` for legacy untagged instances) and the tagged dep
    helpers we spawned for it — but keep Viewer nodes so they are reused. Other
    sources' LayerCakes are left intact. Removal is by name and re-fetched each
    time, since removing a node can invalidate sibling handles."""
    names = []
    for n in tree.nodes:
        is_inst = (n.bl_idname == "CompositorNodeGroup"
                   and _is_parent_group(getattr(n, "node_tree", None)))
        if is_inst and (n.get("lc_src") == key or (existing_name and n.name == existing_name)):
            names.append(n.name)
        elif (n.get("lc_dep") and n.get("lc_src") == key
              and n.bl_idname != "CompositorNodeViewer"):
            names.append(n.name)
    for nm in names:
        n = tree.nodes.get(nm)
        if n is not None:
            tree.nodes.remove(n)


def _purge_orphan_groups():
    """Free any LayerCake parent group with no users (e.g. replaced on rebuild or
    a legacy single-name group), then the shared sub-groups once unused."""
    for g in list(bpy.data.node_groups):
        if _is_parent_group(g) and g.users == 0:
            bpy.data.node_groups.remove(g)
    for name in (builder.LIGHTCOLOR_GROUP, builder.VOLUME_GROUP, builder.LIGHTSIMPLE_GROUP):
        g = bpy.data.node_groups.get(name)
        if g is not None and g.users == 0:
            bpy.data.node_groups.remove(g)


class NODE_OT_exr_remove(bpy.types.Operator):
    """Remove a LayerCake group inserted into this compositor tree — the selected one, or all of them"""

    bl_idname = "node.layercake_remove"
    bl_label = "Remove LayerCake"
    bl_options = {"REGISTER", "UNDO"}

    remove_all: BoolProperty(
        name="Remove All",
        description="Remove every LayerCake in the tree, not just the selected one",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        sd = context.space_data
        if not (sd and sd.type == "NODE_EDITOR" and getattr(sd, "tree_type", "") == "CompositorNodeTree"):
            return False
        tree = getattr(sd, "edit_tree", None)
        return tree is not None and bool(_layercake_instances(tree))

    def execute(self, context):
        scene = context.scene
        tree = context.space_data.edit_tree
        instances = _layercake_instances(tree)
        if not instances:
            self.report({"INFO"}, "No LayerCake group found in this tree.")
            return {"CANCELLED"}

        # Which to remove: the selected instance (or the selected source's one),
        # else — and for the explicit 'all' button — every instance.
        active = getattr(context, "active_node", None)
        sel_inst = active if active in instances else next((n for n in instances if n.select), None)
        if sel_inst is None:
            src = _find_source(context)
            if src is not None:
                sel_inst = _instance_for_key(tree, _source_key(src, scene))
        targets = list(instances) if (self.remove_all or sel_inst is None) else [sel_inst]

        # Plan each removal while handles are valid. The original source MUST
        # survive a Remove: keep external sources in place; for an EMBEDDED source
        # (it lives inside the group and would be purged with it) capture a
        # descriptor so we can re-materialise it into the tree afterwards.
        plan = []
        for inst in targets:
            s = _recover_source(inst, tree)
            item = {"inst": inst.name, "key": inst.get("lc_src"),
                    "loc": (inst.location.x, inst.location.y), "src": None, "desc": None}
            if s is not None and s.id_data == tree:
                item["src"] = s.name                          # external — already there
            elif s is not None:
                item["desc"] = _source_descriptor(s, scene)   # embedded — rebuild it
            plan.append(item)

        removed = 0
        for item in plan:
            n = tree.nodes.get(item["inst"])
            if n is not None:
                tree.nodes.remove(n)
                removed += 1
        _purge_orphan_groups()

        # Re-expose / re-materialise each source and wire it to ITS OWN viewer
        # (+ the composite output when it is the only removal on the top tree).
        top = compat.get_comp_tree(scene)
        single = len(plan) == 1
        for item in plan:
            src = tree.nodes.get(item["src"]) if item["src"] else None
            if src is None and item["desc"] is not None:
                src = builder._embed_source_node(tree, item["desc"])
                if src is not None:
                    src.location = item["loc"]                # where the group used to sit
            if src is None:
                continue
            img = src.outputs.get("Image")
            if img is None:
                continue
            viewer = _get_or_make_viewer(tree, key=item["key"], near=src)
            tree.links.new(img, viewer.inputs["Image"])
            if single and tree == top:
                _out, img_in = compat.ensure_group_output_image(tree)
                if img_in is not None:
                    tree.links.new(img, img_in)

        self.report({"INFO"}, "Removed %d LayerCake group(s); source(s) kept." % removed)
        return {"FINISHED"}


def _crypto_in_layercake(tree, kind):
    """(instance, picker_node) for ``kind`` inside a LayerCake group, or (None, None)."""
    if tree is None:
        return (None, None)
    want = "crypto " + kind
    for inst in _layercake_instances(tree):
        g = inst.node_tree
        if g is None:
            continue
        for n in g.nodes:
            if n.bl_idname.startswith("CompositorNodeCrypto"):
                layer = getattr(n, "layer_name", "").rsplit(".", 1)[-1].lower()
                if layer.endswith(kind) or n.label.lower() == want:
                    return (inst, n)
    return (None, None)


def crypto_node(tree, kind):
    """The Cryptomatte picker of ``kind`` inside a LayerCake group (for the UI)."""
    return _crypto_in_layercake(tree, kind)[1]


class NODE_OT_exr_crypto_pick(bpy.types.Operator):
    """Pick Cryptomatte entries by clicking in the backdrop. Shows the picker
    colours in the viewer and runs an eyedropper at the current node level — no
    need to enter the group. Click to add, Alt/Opt-click to remove, Enter/Esc to end"""

    bl_idname = "node.layercake_crypto_pick"
    bl_label = "Pick Cryptomatte"
    bl_options = {"REGISTER", "UNDO"}

    kind: StringProperty(default="object")

    @classmethod
    def poll(cls, context):
        sd = context.space_data
        if not (sd and sd.type == "NODE_EDITOR" and getattr(sd, "tree_type", "") == "CompositorNodeTree"):
            return False
        return getattr(sd, "edit_tree", None) is not None

    def invoke(self, context, event):
        sd = context.space_data
        tree = sd.edit_tree
        inst, inner = _crypto_in_layercake(tree, self.kind)
        if inner is None:
            self.report({"WARNING"}, "No %s Cryptomatte found — bake first." % self.kind.title())
            return {"CANCELLED"}
        pick_sock = inst.outputs.get("%s Pick" % self.kind.title())
        if pick_sock is None:
            self.report({"WARNING"}, "Re-bake to expose the %s Pick output." % self.kind.title())
            return {"CANCELLED"}

        # Show the picker's false-colour view in the existing viewer.
        viewer = _get_or_make_viewer(tree, key=inst.get("lc_src"), near=inst)
        tree.links.new(pick_sock, viewer.inputs["Image"])
        try:
            sd.show_backdrop = True
        except AttributeError:
            pass

        self._inner = inner
        self._viewer = viewer
        self._tree = tree
        self._space = sd
        self._combined = inst.outputs.get("Combined")
        self._region = next((r for r in context.area.regions if r.type == "WINDOW"), context.region)

        context.window.cursor_modal_set("EYEDROPPER")
        context.area.header_text_set(
            "Cryptomatte %s — click to add, Alt/Opt-click to remove, Enter to finish, Esc to cancel"
            % self.kind.title()
        )
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def _pick_at(self, event, remove):
        img = bpy.data.images.get("Viewer Node")
        if img is None or img.size[0] == 0 or img.size[1] == 0:
            return
        iw, ih = img.size
        r = self._region
        zoom = getattr(self._space, "backdrop_zoom", 1.0) or 1.0
        off = getattr(self._space, "backdrop_offset", (0.0, 0.0))
        mx = event.mouse_x - r.x
        my = event.mouse_y - r.y
        # backdrop is drawn centred in the region, offset by backdrop_offset, scaled by zoom
        x0 = r.width / 2.0 + off[0] - iw * zoom / 2.0
        y0 = r.height / 2.0 + off[1] - ih * zoom / 2.0
        ix = int((mx - x0) / zoom)
        iy = int((my - y0) / zoom)
        if ix < 0 or iy < 0 or ix >= iw or iy >= ih:
            return
        idx = (iy * iw + ix) * 4
        px = img.pixels
        col = (px[idx], px[idx + 1], px[idx + 2])
        try:
            if remove:
                self._inner.remove = col
            else:
                self._inner.add = col
        except (TypeError, ValueError):
            pass

    def _over_backdrop(self, event):
        r = self._region
        return 0 <= event.mouse_x - r.x < r.width and 0 <= event.mouse_y - r.y < r.height

    def _end(self, context):
        try:
            context.window.cursor_modal_restore()
            context.area.header_text_set(None)
        except Exception:
            pass
        # restore the viewer to the Combined result
        if self._combined is not None and self._viewer is not None:
            try:
                self._tree.links.new(self._combined, self._viewer.inputs["Image"])
            except Exception:
                pass
        if context.area:
            context.area.tag_redraw()

    def modal(self, context, event):
        if event.type in {"MIDDLEMOUSE", "WHEELUPMOUSE", "WHEELDOWNMOUSE", "TRACKPADPAN", "TRACKPADZOOM"}:
            return {"PASS_THROUGH"}
        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            if not self._over_backdrop(event):
                return {"PASS_THROUGH"}
            self._pick_at(event, remove=event.alt)
            if context.area:
                context.area.tag_redraw()
            return {"RUNNING_MODAL"}
        if event.type in {"RET", "NUMPAD_ENTER"}:
            self._end(context)
            return {"FINISHED"}
        if event.type in {"ESC", "RIGHTMOUSE"}:
            self._end(context)
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}


classes = (EXRCombinerSettings, NODE_OT_exr_combine, NODE_OT_exr_remove, NODE_OT_exr_crypto_pick)


def register_props():
    bpy.types.Scene.layercake = PointerProperty(type=EXRCombinerSettings)


def unregister_props():
    if hasattr(bpy.types.Scene, "layercake"):
        del bpy.types.Scene.layercake
