"""LockNodes — password-lock node groups for preset pack distribution."""

import bpy

from . import ops, prefs, ui
from .core import enforce

_classes = prefs.classes + ops.classes + ui.classes


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    enforce.register()


def unregister():
    enforce.unregister()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
