# SPDX-License-Identifier: MIT
bl_info = {
    "name": "Listen Children!",  # Removed exclamation mark and comma
    "author": "kunemann",
    "version": (1, 5),
    "blender": (4, 0, 0),
    "location": "View3D > N Panel > Listen Children",
    "description": "Synchronizes visibility between Parent and Child objects",
    "category": "Object",
}

import bpy

def set_visibility_driver(parent, prop, enable=True):
    if prop == "hide_viewport" and enable:
        if '_view_sync' not in parent:
            parent['_view_sync'] = parent.hide_viewport
        
    for child in parent.children_recursive:
        if not child.animation_data:
            child.animation_data_create()
        
        try:
            child.driver_remove(prop)
        except (AttributeError, KeyError) as e:
            pass

        if enable:
            if prop == "hide_viewport":
                if not parent.animation_data:
                    parent.animation_data_create()
                    
                try:
                    parent.driver_remove('["_view_sync"]')
                except:
                    pass
                    
                parent_driver = parent.driver_add('["_view_sync"]').driver
                parent_driver.type = 'SCRIPTED'
                var = parent_driver.variables.new()
                var.name = 'var'
                var.targets[0].id = parent
                var.targets[0].data_path = prop
                parent_driver.expression = 'var'
                
                driver = child.driver_add(prop).driver
                driver.type = 'SCRIPTED'
                var = driver.variables.new()
                var.name = 'var'
                var.targets[0].id = parent
                var.targets[0].data_path = '["_view_sync"]'
                driver.expression = 'var'
            else:
                driver = child.driver_add(prop).driver
                driver.type = 'SCRIPTED'
                var = driver.variables.new()
                var.name = 'var'
                var.targets[0].id = parent
                var.targets[0].data_path = prop
                driver.expression = 'var'
        else:
            if prop == "hide_viewport" and '_view_sync' in parent:
                del parent['_view_sync']
            setattr(child, prop, getattr(parent, prop))

class OBJECT_OT_ToggleVisibilityBase(bpy.types.Operator):
    bl_idname = "object.toggle_visibility_base"
    bl_label = "Toggle Visibility Base"
    
    prop_name: bpy.props.StringProperty()
    sync_key: bpy.props.StringProperty()

    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.children

    def execute(self, context):
        obj = context.object
        if obj.get(self.sync_key, False):
            set_visibility_driver(obj, self.prop_name, enable=False)
            obj[self.sync_key] = False
        else:
            set_visibility_driver(obj, self.prop_name, enable=True)
            obj[self.sync_key] = True
        return {'FINISHED'}

class OBJECT_OT_ToggleViewportVisibility(OBJECT_OT_ToggleVisibilityBase):
    bl_idname = "object.toggle_viewport_visibility"
    bl_label = "Toggle Viewport Visibility"
    bl_description = "Toggle syncing viewport visibility for child objects based on parent objects' visibility"
    
    prop_name: bpy.props.StringProperty(default="hide_viewport")
    sync_key: bpy.props.StringProperty(default="viewport_visibility_sync")

class OBJECT_OT_ToggleRenderVisibility(OBJECT_OT_ToggleVisibilityBase):
    bl_idname = "object.toggle_render_visibility"
    bl_label = "Toggle Render Visibility"
    bl_description = "Toggle syncing render visibility for child objects based on parent objects' visibility"
    
    prop_name: bpy.props.StringProperty(default="hide_render")
    sync_key: bpy.props.StringProperty(default="render_visibility_sync")

class LC_PT_Panel(bpy.types.Panel):  # Changed class name to follow Blender naming conventions
    bl_label = "Listen Children"
    bl_idname = "VIEW3D_PT_listen_children"  # Changed to follow Blender naming conventions
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Listen Children'

    def draw(self, context):
        layout = self.layout
        obj = context.object

        col = layout.column()

        # Viewport Visibility
        col.label(text="Sync Viewport")
        row = col.row(align=True)
        if obj and obj.get('viewport_visibility_sync', False):
            row.operator("object.toggle_viewport_visibility", text="Disable", icon='CHECKBOX_HLT', depress=True)
        else:
            row.operator("object.toggle_viewport_visibility", text="Enable", icon='CHECKBOX_DEHLT', depress=False)
        
        # Render Visibility
        col.label(text="Sync Render")
        row = col.row(align=True)
        if obj and obj.get('render_visibility_sync', False):
            row.operator("object.toggle_render_visibility", text="Disable", icon='CHECKBOX_HLT', depress=True)
        else:
            row.operator("object.toggle_render_visibility", text="Enable", icon='CHECKBOX_DEHLT', depress=False)

class VIEW3D_MT_ListenChildren(bpy.types.Menu):  # Changed class name to follow Blender naming conventions
    bl_label = "Listen Children"
    bl_idname = "VIEW3D_MT_listen_children"  # Added bl_idname

    def draw(self, context):
        layout = self.layout
        obj = context.object

        if obj:
            options = [
                ("object.toggle_viewport_visibility", "Sync Viewport", "viewport_visibility_sync"),
                ("object.toggle_render_visibility", "Sync Render", "render_visibility_sync")
            ]
            
            for op_id, label, sync_key in options:
                icon = 'CHECKMARK' if obj.get(sync_key, False) else 'X'
                layout.operator(op_id, text=label, icon=icon)

def menu_func(self, context):
    layout = self.layout
    layout.menu("VIEW3D_MT_listen_children", text="Listen Children")

classes = (
    OBJECT_OT_ToggleVisibilityBase,
    OBJECT_OT_ToggleViewportVisibility,
    OBJECT_OT_ToggleRenderVisibility,
    LC_PT_Panel,
    VIEW3D_MT_ListenChildren
)

def register():
    try:
        for cls in classes:
            bpy.utils.register_class(cls)
        bpy.types.VIEW3D_MT_object_context_menu.append(menu_func)
        print("Listen Children add-on registered successfully")
    except Exception as e:
        print(f"Error registering Listen Children add-on: {str(e)}")
        raise

def unregister():
    try:
        for cls in reversed(classes):
            bpy.utils.unregister_class(cls)
        bpy.types.VIEW3D_MT_object_context_menu.remove(menu_func)
        print("Listen Children add-on unregistered successfully")
    except Exception as e:
        print(f"Error unregistering Listen Children add-on: {str(e)}")
        raise

if __name__ == "__main__":
    register()