"""N-panel in the node editor sidebar."""

import bpy

from . import ops, prefs
from .core import state, trees


class LOCKNODES_PT_main(bpy.types.Panel):
    bl_idname = "LOCKNODES_PT_main"
    bl_label = "LockNodes"
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_category = "LockNodes"

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return (
            space is not None
            and space.type == "NODE_EDITOR"
            and space.edit_tree is not None
            and space.tree_type in trees.SUPPORTED_TREE_TYPES
        )

    def draw(self, context):
        layout = self.layout

        if state.pending_finalize:
            box = layout.box()
            box.alert = True
            box.label(text="Lock not final until save & reload", icon="ERROR")
            box.operator("locknodes.finalize_hard_lock", icon="FILE_TICK")

        node = ops.active_group_node(context)
        if node is None:
            layout.label(text="Select a node group", icon="INFO")
            return

        tree = node.node_tree
        lock = state.get_lock(tree)
        locked_targets = ops._locked_targets(tree)

        if lock is not None:
            self._draw_locked(layout, tree, lock)
            if len(locked_targets) > 1:
                layout.operator(
                    "locknodes.unlock_all", icon="UNLOCKED",
                    text=f"Unlock All ({len(locked_targets)})",
                )
        elif locked_targets:
            box = layout.box()
            box.label(text=f"'{tree.name}'", icon="NODETREE")
            box.label(text=f"{len(locked_targets)} locked sub-group(s) inside")
            box.operator(
                "locknodes.unlock_all", icon="UNLOCKED",
                text=f"Unlock All ({len(locked_targets)})",
            )
            layout.separator()
            self._draw_unlocked(layout, tree, context)
        else:
            self._draw_unlocked(layout, tree, context)

    def _draw_locked(self, layout, tree, lock):
        box = layout.box()
        box.label(text=f"'{tree.name}'", icon="LOCKED")
        box.label(text=f"Locked — {lock.get('removed_count', '?')} node(s) protected")
        if not lock.get("blob"):
            alert = box.row()
            alert.alert = True
            alert.label(text="Lock data missing — cannot unlock", icon="ERROR")
            return
        layout.operator("locknodes.unlock", icon="UNLOCKED")

    def _draw_unlocked(self, layout, tree, context):
        if not prefs.master_password_set():
            box = layout.box()
            box.alert = True
            box.label(text="No master password set", icon="ERROR")
            op = box.operator("preferences.addon_show", text="Open Preferences")
            op.module = __package__
            return

        edit_tree = context.space_data.edit_tree
        selected = [
            n for n in edit_tree.nodes
            if n.select and n.bl_idname not in trees.INTERFACE_NODE_IDNAMES
        ] if edit_tree is not None else []

        box = layout.box()
        box.label(text="Lock Nodes", icon="LOCKED")
        col = box.column(align=True)
        col.label(text="Encrypts the chosen nodes and")
        col.label(text="removes them from the group.")
        col.label(text="The password restores them later.")

        if selected:
            box.label(
                text=f"{len(selected)} selected node(s) will be locked",
                icon="RESTRICT_SELECT_OFF",
            )
        else:
            box.label(text="Select nodes to lock", icon="INFO")

        box.operator("locknodes.lock_hard", text="Lock Nodes…", icon="LOCKED")


classes = (LOCKNODES_PT_main,)
