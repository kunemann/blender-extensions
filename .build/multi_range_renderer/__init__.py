# SPDX-License-Identifier: MIT
bl_info = {
    "name": "Multi Range Renderer",
    "author": "kunemann (www.koen.work)",
    "version": (1, 2, 0),
    "blender": (3, 0, 0),
    "location": "Render menu (TOPBAR) + Properties > Output > Multi Range Renderer (settings)",
    "description": "Render multiple frame ranges with selected cameras in the visible render window (sequential, modal).",
    "category": "Render",
}

import bpy
import os
import re
from bpy.props import StringProperty, BoolProperty
from bpy.types import Operator, Panel
from bpy.app.handlers import persistent

RANGE_HINT = "Format: 200-230, 340-493, 511-516  (or single frames: 200, 340, 511)"
RANGE_REGEX = re.compile(r"(\d+)-(\d+)")

# Module-level state bridges the persistent render handlers (registered once)
# and the running modal operator instance. Keeping handlers persistent and
# operator state out of them prevents zombie handlers from surviving across
# operator runs — the root cause of skipped-frame glitches.
_MRR_STATE = {
    "active": False,       # True while our modal operator is running
    "rendering": False,    # True between bpy.ops.render.render and its complete/cancel
    "cancelled": False,    # set by render_cancel handler (render window X)
}


def parse_frames(range_text: str):
    """Parse '10-15, 20, 33-35' -> sorted unique list of ints."""
    frames = set()
    for chunk in re.split(r"[\s,;]+", range_text.strip()):
        if not chunk:
            continue
        m = RANGE_REGEX.fullmatch(chunk)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                raise ValueError(f"Invalid range '{chunk}' (start must be <= end)")
            frames.update(range(a, b + 1))
        else:
            if not chunk.isdigit():
                raise ValueError(f"Invalid entry: '{chunk}'")
            frames.add(int(chunk))
    if not frames:
        raise ValueError("No frames specified.")
    return sorted(frames)


def _define_props():
    if not hasattr(bpy.types.Object, "mrr_use_camera"):
        bpy.types.Object.mrr_use_camera = BoolProperty(
            name="Use Camera",
            description="Use this camera for multi-range rendering",
            default=False,
        )
    if not hasattr(bpy.types.Scene, "mrr_frame_ranges"):
        bpy.types.Scene.mrr_frame_ranges = StringProperty(
            name="Frame Ranges",
            description="Enter frame ranges (e.g., 200-230, 340-493) or single frames (200, 340)",
            default="",
        )
    if not hasattr(bpy.types.Scene, "mrr_output_folder"):
        bpy.types.Scene.mrr_output_folder = StringProperty(
            name="Output Folder",
            description="Optional output directory (leave blank to use current render filepath)",
            subtype="DIR_PATH",
            default="",
        )


def _undefine_props():
    for owner, name in (
        (bpy.types.Object, "mrr_use_camera"),
        (bpy.types.Scene, "mrr_frame_ranges"),
        (bpy.types.Scene, "mrr_output_folder"),
    ):
        if hasattr(owner, name):
            try:
                delattr(owner, name)
            except Exception:
                pass


def get_selected_cameras(context):
    return [ob for ob in context.scene.objects
            if ob.type == 'CAMERA' and getattr(ob, "mrr_use_camera", False)]


def apply_hash_template(filename: str, frame: int) -> str:
    """Replace '#' runs with the zero-padded frame. If no '#' is present, append
    a 4-digit frame suffix before the extension so frames don't overwrite."""
    if "#" not in filename:
        base, ext = os.path.splitext(filename)
        filename = (f"{base}_####{ext}") if base else "####"

    def repl(m):
        return str(int(frame)).zfill(len(m.group(0)))
    return re.sub(r"(#+)", repl, filename)


# ---- Persistent render handlers ----
# Registered once at addon register. @persistent survives file loads.
# They only write to _MRR_STATE when our modal operator is active, so they
# are inert when the user runs normal renders.

@persistent
def _mrr_on_render_complete(_scene, *_args):
    if _MRR_STATE["active"]:
        _MRR_STATE["rendering"] = False


@persistent
def _mrr_on_render_cancel(_scene, *_args):
    if _MRR_STATE["active"]:
        _MRR_STATE["rendering"] = False
        _MRR_STATE["cancelled"] = True


def _install_handlers():
    for hlist, cb in (
        (bpy.app.handlers.render_complete, _mrr_on_render_complete),
        (bpy.app.handlers.render_cancel, _mrr_on_render_cancel),
    ):
        if cb not in hlist:
            hlist.append(cb)


def _remove_handlers():
    for hlist, cb in (
        (bpy.app.handlers.render_complete, _mrr_on_render_complete),
        (bpy.app.handlers.render_cancel, _mrr_on_render_cancel),
    ):
        # Remove every occurrence (defensive against duplicate appends from reloads)
        while cb in hlist:
            try:
                hlist.remove(cb)
            except Exception:
                break


class RENDER_OT_multirange_modal(Operator):
    """Visible rendering — shows the render window and writes stills."""
    bl_idname = "render.multirange_visible"
    bl_label = "Multi Range Render"
    bl_description = "Render selected frames for chosen cameras in the visible render window (sequential, non-blocking)"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        # Block a second run while one is in flight — prevents overlapping state.
        return context.scene is not None and not _MRR_STATE["active"]

    def _init_state(self):
        self._timer = None
        self._stop_all = False
        self._awaiting_advance = False  # a render is in flight; advance indices when it ends
        self._cam_idx = 0
        self._frame_idx = 0
        self._cams = []
        self._frames = []
        self._out_folder = ""
        self._orig_render_filepath = None  # sentinel: None = never captured, do not restore
        self._total_done = 0
        self._total_jobs = 0

    def _cleanup(self, context, final_msg=None):
        _MRR_STATE["active"] = False
        _MRR_STATE["rendering"] = False
        _MRR_STATE["cancelled"] = False

        wm = context.window_manager
        if self._timer is not None:
            try:
                wm.event_timer_remove(self._timer)
            except Exception:
                pass
            self._timer = None

        if self._orig_render_filepath is not None:
            try:
                context.scene.render.filepath = self._orig_render_filepath
            except Exception:
                pass
            self._orig_render_filepath = None

        if final_msg:
            self.report({'INFO'}, final_msg)

    def _advance_indices(self):
        self._frame_idx += 1
        if self._frame_idx >= len(self._frames):
            self._frame_idx = 0
            self._cam_idx += 1

    def _current_job(self):
        if self._cam_idx >= len(self._cams):
            return None
        return self._cams[self._cam_idx], self._frames[self._frame_idx]

    def _start_next_render(self, context):
        job = self._current_job()
        if job is None:
            return False
        cam, frame = job
        scene = context.scene

        # Camera may have been deleted mid-run — skip it instead of crashing.
        if cam.name not in scene.objects:
            self.report({'WARNING'}, f"Camera '{cam.name}' no longer in scene, skipping.")
            self._advance_indices()
            return True

        try:
            scene.camera = cam
            scene.frame_set(int(frame))
            # Force depsgraph evaluation before handing off to the render op so
            # drivers / simulations reflect the new frame.
            context.view_layer.update()
        except Exception as e:
            self.report({'WARNING'}, f"Failed to set camera/frame ({cam.name}, {frame}): {e}")
            self._advance_indices()
            return True

        orig_abs = bpy.path.abspath(self._orig_render_filepath or "")
        orig_dir, orig_name = os.path.split(orig_abs)
        base_dir = self._out_folder if self._out_folder else orig_dir
        if not base_dir:
            # Last-resort fallback: blend file directory (execute() already
            # verified the blend is saved, so this is always defined).
            base_dir = os.path.dirname(bpy.path.abspath(bpy.data.filepath))
        target_dir = os.path.join(base_dir, cam.name)
        try:
            os.makedirs(target_dir, exist_ok=True)
        except Exception as e:
            self.report({'ERROR'}, f"Cannot create output folder: {e}")
            self._stop_all = True
            return False

        name_template = orig_name if orig_name else "####"
        final_name = apply_hash_template(name_template, frame)
        scene.render.filepath = os.path.join(target_dir, final_name)

        _MRR_STATE["rendering"] = True
        bpy.ops.render.render('INVOKE_DEFAULT', write_still=True)
        return True

    def execute(self, context):
        if _MRR_STATE["active"]:
            self.report({'ERROR'}, "A Multi Range Render is already in progress.")
            return {'CANCELLED'}

        self._init_state()
        scene = context.scene

        if not scene.mrr_frame_ranges.strip():
            self.report({'ERROR'}, "Please enter frame ranges. " + RANGE_HINT)
            return {'CANCELLED'}
        try:
            self._frames = parse_frames(scene.mrr_frame_ranges)
        except Exception as e:
            self.report({'ERROR'}, f"Frame parsing failed: {e}")
            return {'CANCELLED'}

        self._cams = get_selected_cameras(context)
        if not self._cams:
            self.report({'ERROR'}, "No cameras selected. Enable 'Use Camera' on at least one camera.")
            return {'CANCELLED'}

        blend_path = bpy.data.filepath
        if not blend_path or not os.path.exists(blend_path):
            self.report({'ERROR'}, "Please save the .blend file first.")
            return {'CANCELLED'}

        self._orig_render_filepath = scene.render.filepath

        out_folder_raw = scene.mrr_output_folder.strip()
        if out_folder_raw:
            self._out_folder = bpy.path.abspath(out_folder_raw)
            try:
                os.makedirs(self._out_folder, exist_ok=True)
            except Exception as e:
                self.report({'ERROR'}, f"Cannot create output folder: {e}")
                self._orig_render_filepath = None
                return {'CANCELLED'}
        else:
            self._out_folder = ""

        self._total_jobs = len(self._cams) * len(self._frames)

        _MRR_STATE["active"] = True
        _MRR_STATE["rendering"] = False
        _MRR_STATE["cancelled"] = False

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.2, window=context.window)
        wm.modal_handler_add(self)
        self.report(
            {'INFO'},
            f"Starting visible sequence render: {len(self._cams)} cam(s) × {len(self._frames)} frame(s) = {self._total_jobs} still(s).",
        )
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'ESC':
            self._stop_all = True
            if not _MRR_STATE["rendering"]:
                self._cleanup(context, "Multi Range Render canceled.")
                return {'CANCELLED'}
            # Let the in-flight render finish, then cleanup on next tick.
            return {'RUNNING_MODAL'}

        if event.type != 'TIMER':
            return {'RUNNING_MODAL'}

        if _MRR_STATE["cancelled"]:
            self._stop_all = True

        if _MRR_STATE["rendering"]:
            # Still rendering — wait for the handler to clear the flag.
            return {'RUNNING_MODAL'}

        # Not rendering right now.
        if self._stop_all:
            self._cleanup(context, "Multi Range Render canceled.")
            return {'CANCELLED'}

        # If a render just finished, advance *now* (not in the handler — keeps
        # all index mutation on the main-thread timer path).
        if self._awaiting_advance:
            self._advance_indices()
            self._awaiting_advance = False
            self._total_done += 1

        if self._cam_idx >= len(self._cams):
            self._cleanup(context, f"Rendering finished ({self._total_done}/{self._total_jobs}).")
            return {'FINISHED'}

        started = self._start_next_render(context)
        if not started:
            self._cleanup(context, "Multi Range Render aborted.")
            return {'CANCELLED'}
        self._awaiting_advance = True
        return {'RUNNING_MODAL'}


class RENDER_PT_multirange(Panel):
    bl_label = "Multi Range Renderer"
    bl_idname = "RENDER_PT_multirange"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "output"

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        col = layout.column(align=True)
        col.label(text="Frame Ranges")
        col.prop(scene, "mrr_frame_ranges", text="")
        col.label(text=RANGE_HINT)

        col.separator()
        col.label(text="Select Cameras:")
        box = col.box()
        found = False
        for ob in scene.objects:
            if ob.type == 'CAMERA':
                found = True
                row = box.row(align=True)
                row.prop(ob, "mrr_use_camera", text="", toggle=True)
                row.label(text=ob.name)
        if not found:
            box.label(text="No cameras found in the scene.")

        col.separator()
        col.prop(scene, "mrr_output_folder", text="Output Folder")

        if _MRR_STATE["active"]:
            col.separator()
            col.label(text="Rendering in progress — ESC to cancel.", icon='INFO')


# ---- Render menu integration ----

def MRR_render_menu_draw(self, _context):
    """Insert Multi Range Render into the TOPBAR 'Render' menu."""
    layout = self.layout
    layout.separator()
    layout.operator("render.multirange_visible", icon='RENDER_STILL', text="Multi Range Render")


classes = (RENDER_OT_multirange_modal, RENDER_PT_multirange)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    _define_props()
    _install_handlers()

    try:
        bpy.types.TOPBAR_MT_render.append(MRR_render_menu_draw)
    except Exception:
        # Fallback for very old Blender (pre-2.80 used INFO_MT_render)
        if hasattr(bpy.types, "INFO_MT_render"):
            bpy.types.INFO_MT_render.append(MRR_render_menu_draw)


def unregister():
    # If a run is still active, force-cancel it so we don't leave timers/handlers dangling.
    _MRR_STATE["active"] = False
    _MRR_STATE["rendering"] = False
    _MRR_STATE["cancelled"] = False

    try:
        bpy.types.TOPBAR_MT_render.remove(MRR_render_menu_draw)
    except Exception:
        if hasattr(bpy.types, "INFO_MT_render"):
            try:
                bpy.types.INFO_MT_render.remove(MRR_render_menu_draw)
            except Exception:
                pass

    _remove_handlers()
    _undefine_props()

    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass


if __name__ == "__main__":
    register()
