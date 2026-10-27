# SPDX-License-Identifier: GPL-3.0-or-later
"""LayerCake — rebuild the Combined image from render passes.

Works as a Blender 4.2+/5.x extension (blender_manifest.toml) and as a legacy
add-on (bl_info) in 4.0/4.1.
"""

bl_info = {
    "name": "LayerCake",
    "author": "kunemann",
    "version": (1, 1, 1),
    "blender": (4, 0, 0),
    "location": "Compositor > Sidebar (N) > LayerCake",
    "description": "Auto-rebuilds the Combined image from a multilayer EXR or Render Layers, in clean node groups.",
    "doc_url": "https://www.kunemann.com",
    "category": "Compositing",
}

import bpy

from . import operators
from . import ui


def register():
    for cls in operators.classes:
        bpy.utils.register_class(cls)
    operators.register_props()
    for cls in ui.classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(ui.classes):
        bpy.utils.unregister_class(cls)
    operators.unregister_props()
    for cls in reversed(operators.classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
