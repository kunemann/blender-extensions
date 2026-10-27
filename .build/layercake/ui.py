# SPDX-License-Identifier: GPL-3.0-or-later
"""Compositor N-panel UI."""

import bpy

from .operators import (
    NODE_OT_exr_combine, NODE_OT_exr_remove, NODE_OT_exr_crypto_pick,
    _find_source, _layercake_instances, _settings_signature, crypto_node,
    _all_sources, _source_key, _source_label, _instance_for_key,
)

_SOURCE_LABEL = {
    "CompositorNodeRLayers": "Render Layers",
    "CompositorNodeImage": "Multilayer EXR",
}


class NODE_PT_exr_combine(bpy.types.Panel):
    bl_label = "LayerCake"
    bl_idname = "NODE_PT_exr_combine"
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_category = "LayerCake"

    @classmethod
    def poll(cls, context):
        sd = context.space_data
        return sd and getattr(sd, "tree_type", "") == "CompositorNodeTree"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        settings = scene.layercake

        tree = getattr(context.space_data, "edit_tree", None)
        sources = _all_sources(tree)
        instances = _layercake_instances(tree)
        selected = _find_source(context)

        # --- summary: how many independent sources / LayerCakes are in the tree.
        n_rl = sum(1 for s in sources if s.bl_idname == "CompositorNodeRLayers")
        n_exr = sum(1 for s in sources if s.bl_idname == "CompositorNodeImage")
        box = layout.box()
        box.label(text="Sources: %d Render Layers · %d EXR" % (n_rl, n_exr), icon="NODE_SEL")
        box.label(text="LayerCakes built: %d" % len(instances), icon="NODETREE")
        if selected is not None:
            box.label(text='Selected: %s "%s"'
                      % (_SOURCE_LABEL.get(selected.bl_idname, ""), _source_label(selected, scene)))

        # --- contextual single-source button: Bake → Update (options changed) →
        #     Remove (unchanged). Acts on the selected source or LayerCake group.
        col = layout.column()
        col.scale_y = 1.4
        sig = _settings_signature(settings)

        # Blender persists an operator's last-used properties, so every button
        # must set its single-vs-all discriminator EXPLICITLY — otherwise a single
        # button silently inherits all_sources / remove_all from a prior batch run
        # and acts on everything.
        def bake_one(parent, text, icon):
            op = parent.operator(NODE_OT_exr_combine.bl_idname, text=text, icon=icon)
            op.all_sources = False

        def remove_one(parent, text, icon):
            op = parent.operator(NODE_OT_exr_remove.bl_idname, text=text, icon=icon)
            op.remove_all = False

        if selected is not None:
            inst = _instance_for_key(tree, _source_key(selected, scene)) if tree is not None else None
            if inst is None:
                bake_one(col, "Bake This Source", "NODETREE")
            elif inst.get("lc_sig", "") != sig:
                bake_one(col, "Update This Source", "FILE_REFRESH")
            else:
                remove_one(col, "Remove This LayerCake", "TRASH")
        else:
            active = getattr(context, "active_node", None)
            inst = active if active in instances else next((n for n in instances if n.select), None)
            if inst is None and instances:
                inst = instances[0]
            if inst is None:
                sub = col.column()
                sub.enabled = False
                bake_one(sub, "Select a source node", "ERROR")
                box.label(text="Render Layers or multilayer EXR", icon="INFO")
            elif inst.get("lc_sig", "") != sig:
                bake_one(col, "Update Selected", "FILE_REFRESH")
            else:
                remove_one(col, "Remove Selected", "TRASH")

        # --- batch buttons: build one independent LayerCake per source at once.
        if sources:
            uncaked = sum(1 for s in sources
                          if _instance_for_key(tree, _source_key(s, scene)) is None)
            row = layout.row()
            row.scale_y = 1.2
            text = ("Bake All Sources (%d)" % len(sources) if uncaked
                    else "Rebuild All Sources (%d)" % len(sources))
            op = row.operator(NODE_OT_exr_combine.bl_idname, text=text, icon="NODETREE")
            op.all_sources = True

        if len(instances) >= 2:
            op = layout.operator(NODE_OT_exr_remove.bl_idname, text="Remove All LayerCakes", icon="TRASH")
            op.remove_all = True

        # Cryptomatte picking — only shown when a crypto node is actually present.
        # The button switches the viewer to the picker view and activates the
        # node; the matte list is editable here. The actual ＋ eyedropper is a
        # Blender-internal widget that add-ons can't trigger, so that single click
        # happens on the node (its ＋ is visible once the button has activated it).
        crypto_rows = [(k, l) for k, l in (("object", "Object"), ("material", "Material"))
                       if crypto_node(tree, k) is not None]
        if crypto_rows:
            cbox = layout.box()
            cbox.label(text="Cryptomatte", icon="EYEDROPPER")
            for kind, label in crypto_rows:
                cm = crypto_node(tree, kind)
                op = cbox.operator(NODE_OT_exr_crypto_pick.bl_idname,
                                   text="Pick %s" % label, icon="EYEDROPPER")
                op.kind = kind
                cbox.prop(cm, "matte_id", text="IDs")

        opts = layout.column(heading="Options")
        opts.use_property_split = True
        opts.use_property_decorate = False
        opts.prop(settings, "embed_source")
        opts.prop(settings, "auto_enable_passes")
        opts.prop(settings, "include_extras")

        crypto = opts.column(heading="Cryptomatte")
        crypto.prop(settings, "include_crypto_object")
        crypto.prop(settings, "include_crypto_material")

        opts.prop(settings, "film_transparent")
        opts.separator()
        opts.prop(settings, "connect_viewer")
        opts.prop(settings, "set_as_output")


classes = (NODE_PT_exr_combine,)
