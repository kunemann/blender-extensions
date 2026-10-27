"""Patchwork operators (wrapper-group architecture)."""

import bpy

from . import node_group


def _selected_image_nodes_in_tree(context, tree):
    selected = getattr(context, "selected_nodes", None) or []
    return [n for n in selected
            if n.bl_idname == "ShaderNodeTexImage"
            and n.id_data is tree
            and not n.get(node_group.ORIG_INSIDE_MARK)
            and not n.get(node_group.DUP_INSIDE_MARK)]


def _unwrapped_image_nodes_in_tree(tree):
    return [n for n in tree.nodes
            if n.bl_idname == "ShaderNodeTexImage"
            and not n.get(node_group.ORIG_INSIDE_MARK)
            and not n.get(node_group.DUP_INSIDE_MARK)]


def _material_of_edit_tree(context):
    sd = context.space_data
    if not sd or sd.type != "NODE_EDITOR":
        return None
    tree = getattr(sd, "edit_tree", None)
    if tree is None:
        return None
    return node_group.material_for_tree(tree)


class PATCHWORK_OT_apply(bpy.types.Operator):
    bl_idname = "patchwork.apply"
    bl_label = "Apply to Material"
    bl_description = (
        "Wraps Image Texture nodes of the active material into a Patchwork wrapper "
        "group with duplicates and Mix nodes for anti-tiling blending. If Image "
        "Texture nodes are selected, only those are wrapped; otherwise all "
        "unwrapped textures of the material are processed."
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        mat = _material_of_edit_tree(context)
        if mat is None:
            return False
        if _selected_image_nodes_in_tree(context, mat.node_tree):
            return True
        return bool(_unwrapped_image_nodes_in_tree(mat.node_tree))

    def execute(self, context):
        mat = _material_of_edit_tree(context)
        if mat is None:
            self.report({"WARNING"}, "Please work in a material's main node tree (not inside a group)")
            return {"CANCELLED"}
        imgs = _selected_image_nodes_in_tree(context, mat.node_tree)
        if not imgs:
            imgs = _unwrapped_image_nodes_in_tree(mat.node_tree)
        if not imgs:
            self.report({"WARNING"}, "No Image Texture nodes to wrap")
            return {"CANCELLED"}
        newly, already = node_group.apply_to_material(mat, imgs)
        if newly == 0 and already == 0:
            self.report({"WARNING"}, "Nothing to do")
            return {"CANCELLED"}
        parts = []
        if newly:
            parts.append(f"{newly} newly wrapped")
        if already:
            parts.append(f"{already} already active")
        self.report({"INFO"}, "Patchwork: " + ", ".join(parts))
        return {"FINISHED"}


class PATCHWORK_OT_apply_to_objects(bpy.types.Operator):
    bl_idname = "patchwork.apply_to_objects"
    bl_label = "Apply to All Materials"
    bl_description = (
        "Wraps all Image Texture nodes in every material of the selected objects "
        "into a Patchwork wrapper group (one per material)."
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(context.selected_objects)

    def execute(self, context):
        seen_mats = set()
        total_new = 0
        total_already = 0
        mats_touched = 0
        for obj in context.selected_objects:
            for slot in obj.material_slots:
                mat = slot.material
                if not mat or not mat.node_tree:
                    continue
                if mat.name in seen_mats:
                    continue
                seen_mats.add(mat.name)
                imgs = _unwrapped_image_nodes_in_tree(mat.node_tree)
                if not imgs:
                    continue
                newly, already = node_group.apply_to_material(mat, imgs)
                total_new += newly
                total_already += already
                if newly or already:
                    mats_touched += 1
        if mats_touched == 0:
            self.report({"WARNING"}, "No materials with Image Textures found")
            return {"CANCELLED"}
        self.report(
            {"INFO"},
            f"Patchwork: {total_new} newly wrapped, {total_already} already active ({mats_touched} materials)",
        )
        return {"FINISHED"}


class PATCHWORK_OT_remove(bpy.types.Operator):
    bl_idname = "patchwork.remove"
    bl_label = "Remove from Material"
    bl_description = (
        "Dissolves the Patchwork wrapper group of the active material and "
        "restores every wrapped texture back in the material's main tree."
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        mat = _material_of_edit_tree(context)
        if mat is None:
            return False
        return node_group.find_wrapper_for_material(mat) is not None

    def execute(self, context):
        mat = _material_of_edit_tree(context)
        if mat is None:
            self.report({"WARNING"}, "Please work in a material's main node tree")
            return {"CANCELLED"}
        restored = node_group.unwrap_material(mat)
        if restored == 0:
            self.report({"WARNING"}, "No Patchwork group in this material")
            return {"CANCELLED"}
        self.report({"INFO"}, f"{restored} textures restored")
        return {"FINISHED"}


class PATCHWORK_OT_remove_all(bpy.types.Operator):
    bl_idname = "patchwork.remove_all"
    bl_label = "Remove from All Materials"
    bl_description = "Dissolves all Patchwork groups across every material."
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        restored = node_group.unwrap_all()
        orphan = node_group.cleanup_orphan_transform()
        if restored == 0 and not orphan:
            self.report({"WARNING"}, "No Patchwork groups present")
            return {"CANCELLED"}
        suffix = " + transform group deleted" if orphan else ""
        self.report({"INFO"}, f"{restored} textures restored from all groups{suffix}")
        return {"FINISHED"}


CLASSES = (
    PATCHWORK_OT_apply,
    PATCHWORK_OT_apply_to_objects,
    PATCHWORK_OT_remove,
    PATCHWORK_OT_remove_all,
)
