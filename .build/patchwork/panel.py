"""Patchwork N-Panel for the Shader Editor sidebar."""

import bpy

from . import node_group


def _selected_image_texture_count(context):
    sd = context.space_data
    if not sd or sd.type != "NODE_EDITOR":
        return 0
    tree = getattr(sd, "edit_tree", None)
    if tree is None:
        return 0
    selected = getattr(context, "selected_nodes", None) or []
    return sum(1 for n in selected
               if n.bl_idname == "ShaderNodeTexImage"
               and n.id_data is tree
               and not n.get(node_group.ORIG_INSIDE_MARK)
               and not n.get(node_group.DUP_INSIDE_MARK))


def _material_for_panel(context):
    """Return the material whose edit_tree is currently shown, if any."""
    sd = context.space_data
    if not sd or sd.type != "NODE_EDITOR":
        return None
    tree = getattr(sd, "edit_tree", None)
    if tree is None:
        return None
    return node_group.material_for_tree(tree)


class PATCHWORK_PT_main(bpy.types.Panel):
    bl_label = "Patchwork"
    bl_idname = "PATCHWORK_PT_main"
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_category = "Patchwork"

    @classmethod
    def poll(cls, context):
        sd = context.space_data
        return bool(sd and getattr(sd, "tree_type", None) == "ShaderNodeTree")

    def draw(self, context):
        layout = self.layout

        # --- Texture-Pattern dropdown (named, at the very top) ---------------
        # Writes into the hidden Value node inside the active material's
        # Patchwork wrapper. Shown only once Patchwork has been applied to the
        # material AND the wrapper has been migrated to the V2 layout (which
        # happens automatically the first time Apply is clicked under V2).
        mat = _material_for_panel(context)
        wrapper = node_group.find_wrapper_for_material(mat) if mat is not None else None
        pattern_box = layout.box()
        pattern_box.label(text="Texture-Pattern:")
        if wrapper is None:
            row = pattern_box.row()
            row.enabled = False
            row.label(text="Apply Patchwork first", icon="INFO")
        elif node_group.find_wrapper_pattern_value_node(wrapper) is None:
            # Legacy wrapper (V1 or earlier V2 design) — needs migration.
            col = pattern_box.column(align=True)
            col.label(text="Legacy wrapper detected.", icon="INFO")
            col.label(text='Click "Apply to Material"')
            col.label(text="to upgrade to multi-pattern.")
        else:
            pattern_box.prop(mat, "patchwork_pattern", text="")

        layout.separator()

        selected_count = _selected_image_texture_count(context)

        col = layout.column(align=True)
        # If a wrapper already exists for the active material, the operator's
        # job is to ADD textures to it (not create a new wrapper) — reflect
        # that in the section header and button text.
        if wrapper is not None:
            col.label(text="Add:")
            if selected_count == 1:
                btn_text = "Add Texture to Patchwork"
            elif selected_count > 1:
                btn_text = f"Add {selected_count} Textures to Patchwork"
            else:
                btn_text = "Add remaining Textures to Patchwork"
        else:
            col.label(text="Apply:")
            if selected_count > 0:
                btn_text = f"Apply to Selected Textures ({selected_count})"
            else:
                btn_text = "Apply to Material"
        col.operator("patchwork.apply", icon="NODETREE", text=btn_text)
        col.operator(
            "patchwork.apply_to_objects",
            icon="OUTLINER_OB_MESH",
            text="Apply to All Materials",
        )

        layout.separator()

        col2 = layout.column(align=True)
        col2.label(text="Remove:")
        col2.operator("patchwork.remove", icon="X", text="Remove from Material")
        col2.operator("patchwork.remove_all", icon="TRASH", text="Remove from All Materials")


CLASSES = (PATCHWORK_PT_main,)
