import bpy

from . import preferences
from . import operator_swap
from . import panel
from . import keymap


_classes = (
    preferences.EXREPLACE_AddonPreferences,
    operator_swap.EXREPLACE_OT_swap_connections,
    panel.EXREPLACE_PT_compositor_panel,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    keymap.register_keymap()


def unregister():
    keymap.unregister_keymap()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
