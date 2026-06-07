# SPDX-License-Identifier: MIT
bl_info = {
    "name": "CopyRender",
    "author": "kunemann (www.koen.work)",
    "version": (1, 2, 0),
    "blender": (4, 0, 0),
    "location": "Properties > Render",
    "description": "Copy and paste render settings via clipboard",
    "category": "Render",
}

import bpy
import json
import sys
import subprocess

# ---------------------------
# Clipboard helpers (cross-platform + macOS fallback)
# ---------------------------

def _set_clipboard(text: str, wm):
    try:
        wm.clipboard = text
        if wm.clipboard == text:
            return True
    except Exception:
        pass
    if sys.platform == "darwin":
        try:
            p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            p.communicate(input=text.encode("utf-8"))
            return p.returncode == 0
        except Exception:
            return False
    return False

def _get_clipboard(wm) -> str:
    try:
        txt = wm.clipboard
        if isinstance(txt, str) and txt.strip():
            return txt
    except Exception:
        pass
    if sys.platform == "darwin":
        try:
            out = subprocess.check_output(["pbpaste"])
            return out.decode("utf-8")
        except Exception:
            return ""
    return ""

# ---------------------------
# Utilities: Safe RNA dump/set
# ---------------------------

_PRIMITIVE_PROP_TYPES = {'BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'}
_VECTOR_PROP_TYPES    = {'BOOLEAN_ARRAY', 'INT_ARRAY', 'FLOAT_ARRAY'}

_ALLOWED_POINTERS_BY_ID = {
    'RenderSettings': {'image_settings', 'stereo_3d_format', 'ffmpeg'},
    'ColorManagedViewSettings': set(),
    'ColorManagedDisplaySettings': set(),
    'ColorManagedSequencerColorspaceSettings': set(),
    'CyclesRenderSettings': set(),
    'EeveeRenderSettings': set(),
}

def _dump_rna(obj, visited=None):
    if visited is None:
        visited = set()
    key = (type(obj).__name__, id(obj))
    if key in visited:
        return {}
    visited.add(key)

    data = {}
    rna = obj.bl_rna
    rna_idname = rna.identifier

    for prop in rna.properties:
        ident = prop.identifier
        if ident == "rna_type":
            continue
        if getattr(prop, "is_readonly", False):
            continue
        ptype = prop.type

        try:
            if ptype in _PRIMITIVE_PROP_TYPES:
                val = getattr(obj, ident)
                if ptype == 'ENUM':
                    data[ident] = str(val)
                else:
                    data[ident] = val
            elif ptype in _VECTOR_PROP_TYPES:
                val = getattr(obj, ident)
                data[ident] = list(val)
            elif ptype == 'POINTER':
                allowed = _ALLOWED_POINTERS_BY_ID.get(rna_idname, set())
                if ident in allowed:
                    sub = getattr(obj, ident, None)
                    if sub is not None:
                        data[ident] = {
                            "__pointer_type__": sub.bl_rna.identifier,
                            "data": _dump_rna(sub, visited)
                        }
        except Exception:
            pass

    data["__type__"] = rna_idname
    return data

def _apply_rna(obj, data: dict):
    if not isinstance(data, dict):
        return
    rna = obj.bl_rna
    props_by_name = {p.identifier: p for p in rna.properties}

    for key, val in data.items():
        if key in {"__type__"}:
            continue

        prop = props_by_name.get(key)
        if prop is None or getattr(prop, "is_readonly", False):
            continue

        ptype = prop.type
        try:
            if ptype in _PRIMITIVE_PROP_TYPES:
                if ptype == 'ENUM':
                    enum_items = {i.identifier for i in prop.enum_items}
                    if isinstance(val, str) and val in enum_items:
                        setattr(obj, key, val)
                else:
                    setattr(obj, key, val)
            elif ptype in _VECTOR_PROP_TYPES:
                if isinstance(val, (list, tuple)):
                    setattr(obj, key, val)
            elif ptype == 'POINTER':
                if isinstance(val, dict) and "data" in val:
                    sub = getattr(obj, key, None)
                    if sub is not None:
                        _apply_rna(sub, val["data"])
        except Exception:
            pass

def _gather_scene_render_package(scene: bpy.types.Scene) -> dict:
    pkg = {
        "engine": scene.render.engine,
        "render": _dump_rna(scene.render),
        "view_settings": _dump_rna(scene.view_settings),
        "display_settings": _dump_rna(scene.display_settings),
        "sequencer_colorspace_settings": _dump_rna(scene.sequencer_colorspace_settings),
        "__schema__": "render_sync_v1",
    }
    if hasattr(scene, "cycles") and scene.cycles is not None:
        pkg["cycles"] = _dump_rna(scene.cycles)
    if hasattr(scene, "eevee") and scene.eevee is not None:
        pkg["eevee"] = _dump_rna(scene.eevee)
    return pkg

def _apply_scene_render_package(scene: bpy.types.Scene, pkg: dict, report=None):
    if not isinstance(pkg, dict) or pkg.get("__schema__") != "render_sync_v1":
        if report:
            report({'WARNING'}, "Clipboard does not contain a valid render settings package.")
        return

    engine = pkg.get("engine")
    if engine:
        try:
            scene.render.engine = engine
        except Exception:
            pass

    if "render" in pkg:
        _apply_rna(scene.render, pkg["render"])
    if "view_settings" in pkg:
        _apply_rna(scene.view_settings, pkg["view_settings"])
    if "display_settings" in pkg:
        _apply_rna(scene.display_settings, pkg["display_settings"])
    if "sequencer_colorspace_settings" in pkg:
        _apply_rna(scene.sequencer_colorspace_settings, pkg["sequencer_colorspace_settings"])
    if "cycles" in pkg and hasattr(scene, "cycles") and scene.cycles is not None:
        _apply_rna(scene.cycles, pkg["cycles"])
    if "eevee" in pkg and hasattr(scene, "eevee") and scene.eevee is not None:
        _apply_rna(scene.eevee, pkg["eevee"])

# ---------------------------
# JSON encoder fix
# ---------------------------

def _to_basic(o):
    try:
        from bpy.types import bpy_prop_array as _BPA
        if isinstance(o, _BPA):
            return [ _to_basic(x) for x in o ]
    except Exception:
        pass

    try:
        import mathutils
        if isinstance(o, (mathutils.Vector, mathutils.Euler, mathutils.Quaternion, mathutils.Color)):
            return [ _to_basic(x) for x in o ]
    except Exception:
        pass

    if isinstance(o, dict):
        return { str(k): _to_basic(v) for k, v in o.items() }
    if isinstance(o, (list, tuple, set)):
        return [ _to_basic(v) for v in o ]

    return o

class BlenderJSONEncoder(json.JSONEncoder):
    def default(self, o):
        basic = _to_basic(o)
        if basic is not o:
            return basic
        return super().default(o)

# ---------------------------
# Operators
# ---------------------------

class COPYRENDER_OT_copy(bpy.types.Operator):
    bl_idname = "copyrender.copy"
    bl_label = "Copy Render Settings"
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        pkg = _gather_scene_render_package(scene)
        try:
            payload = json.dumps(pkg, indent=2, ensure_ascii=False, cls=BlenderJSONEncoder)
        except Exception as e:
            self.report({'WARNING'}, f"Serialization failed: {e}")
            return {'CANCELLED'}

        ok = _set_clipboard(payload, context.window_manager)
        if ok:
            self.report({'INFO'}, "Render settings copied.")
            return {'FINISHED'}
        else:
            self.report({'WARNING'}, "Copy to clipboard failed.")
            return {'CANCELLED'}

class COPYRENDER_OT_paste(bpy.types.Operator):
    bl_idname = "copyrender.paste"
    bl_label = "Paste Render Settings"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        text = _get_clipboard(context.window_manager)
        if not text.strip():
            self.report({'WARNING'}, "Clipboard is empty.")
            return {'CANCELLED'}
        try:
            pkg = json.loads(text)
        except Exception:
            self.report({'WARNING'}, "Clipboard does not contain valid JSON.")
            return {'CANCELLED'}

        _apply_scene_render_package(context.scene, pkg, report=self.report)
        return {'FINISHED'}

# ---------------------------
# Panel (in Render Properties)
# ---------------------------

class COPYRENDER_PT_panel(bpy.types.Panel):
    bl_label = "CopyRender"
    bl_idname = "COPYRENDER_PT_panel"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "render"

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.operator("copyrender.copy", icon='COPYDOWN')
        col.operator("copyrender.paste", icon='PASTEDOWN')

# ---------------------------
# Context menu integration (Right-click)
# ---------------------------

def _copyrender_context_entries(layout):
    layout.separator()
    layout.operator("copyrender.copy", icon='COPYDOWN', text="Copy Render Settings")
    layout.operator("copyrender.paste", icon='PASTEDOWN', text="Paste Render Settings")

def _add_to_view3d_object_context(self, context):
    _copyrender_context_entries(self.layout)

def _add_to_outliner_context(self, context):
    _copyrender_context_entries(self.layout)

_MENU_HOOKS = [
    ("VIEW3D_MT_object_context_menu", _add_to_view3d_object_context),
    ("OUTLINER_MT_context_menu", _add_to_outliner_context),
]

def _append_menus():
    for menu_name, fn in _MENU_HOOKS:
        menu_cls = getattr(bpy.types, menu_name, None)
        if menu_cls is not None:
            try:
                menu_cls.append(fn)
            except Exception:
                pass

def _remove_menus():
    for menu_name, fn in _MENU_HOOKS:
        menu_cls = getattr(bpy.types, menu_name, None)
        if menu_cls is not None:
            try:
                menu_cls.remove(fn)
            except Exception:
                pass

# ---------------------------
# Registration
# ---------------------------

classes = (
    COPYRENDER_OT_copy,
    COPYRENDER_OT_paste,
    COPYRENDER_PT_panel,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    _append_menus()

def unregister():
    _remove_menus()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    register()
