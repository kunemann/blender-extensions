"""Enforcement engine: maintains the locked-tree registry so the UI stays
current.  No more kick-out / remute — locked trees remain accessible.
"""

import bpy
from bpy.app.handlers import persistent

from . import state

IDLE_INTERVAL = 1.0
_RESCAN_EVERY_N = 30

_tick = 0


def _watchdog():
    global _tick
    try:
        if not state.has_locks():
            return IDLE_INTERVAL
        _tick += 1
        if _tick % _RESCAN_EVERY_N == 0:
            state.mark_dirty()
    except Exception:
        import traceback
        traceback.print_exc()
    return IDLE_INTERVAL


@persistent
def _on_load_post(filepath):
    state.pending_finalize.clear()
    state.mark_dirty()


@persistent
def _on_blend_import_post(import_contexts):
    state.mark_dirty()


@persistent
def _on_undo_redo(scene):
    state.mark_dirty()


_HANDLERS = (
    ("load_post", _on_load_post),
    ("blend_import_post", _on_blend_import_post),
    ("undo_post", _on_undo_redo),
    ("redo_post", _on_undo_redo),
)


def register():
    for name, fn in _HANDLERS:
        handler_list = getattr(bpy.app.handlers, name, None)
        if handler_list is not None and fn not in handler_list:
            handler_list.append(fn)
    if not bpy.app.timers.is_registered(_watchdog):
        bpy.app.timers.register(_watchdog, first_interval=0.5, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_watchdog):
        bpy.app.timers.unregister(_watchdog)
    for name, fn in _HANDLERS:
        handler_list = getattr(bpy.app.handlers, name, None)
        if handler_list is not None and fn in handler_list:
            handler_list.remove(fn)
