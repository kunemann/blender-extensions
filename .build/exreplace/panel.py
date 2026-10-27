import bpy


class EXREPLACE_PT_compositor_panel(bpy.types.Panel):
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_category = "EXReplace"
    bl_label = "EXReplace"

    @classmethod
    def poll(cls, context):
        sd = context.space_data
        return sd is not None and sd.tree_type == "CompositorNodeTree"

    def draw(self, context):
        layout = self.layout
        layout.operator(
            "exreplace.swap_connections",
            text="Swap Connections",
            icon="ARROW_LEFTRIGHT",
        )
        col = layout.column(align=True)
        col.scale_y = 0.8
        col.label(text="Select Render Layer")
        col.label(text="and EXR Image, then swap.")
