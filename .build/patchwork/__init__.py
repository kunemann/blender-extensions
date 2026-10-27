"""Patchwork — Multi-pattern anti-tiling for image textures.

V2: the Pattern selector (Voronoi / Noise / Gabor / Blocks / Rings) is exposed
ONLY as a named enum dropdown in the N-Panel sidebar (Patchwork tab). The
wrapper Group-Instance no longer carries a Pattern input socket — the value is
held in a hidden ShaderNodeValue inside the wrapper, written by the dropdown.
"""

import bpy

from . import node_group, operators, panel

_CLASSES = operators.CLASSES + panel.CLASSES


def _get_pattern(self):
    """EnumProperty getter — reads Pattern from the hidden Value node inside
    the material's Patchwork wrapper."""
    wrapper = node_group.find_wrapper_for_material(self)
    val_node = node_group.find_wrapper_pattern_value_node(wrapper)
    if val_node is None:
        return 0
    try:
        idx = int(round(val_node.outputs[0].default_value))
    except (TypeError, ValueError):
        return 0
    return max(0, min(len(node_group.PATTERN_NAMES) - 1, idx))


def _set_pattern(self, value):
    """EnumProperty setter — writes Pattern to the hidden Value node inside
    the material's Patchwork wrapper. No-op if the wrapper has not been
    migrated yet (the panel asks the user to click Apply to migrate)."""
    wrapper = node_group.find_wrapper_for_material(self)
    val_node = node_group.find_wrapper_pattern_value_node(wrapper)
    if val_node is None:
        return
    val_node.outputs[0].default_value = float(value)


def _pattern_enum_items():
    return [
        (str(i), name, desc)
        for i, (name, desc) in enumerate(zip(node_group.PATTERN_NAMES, node_group.PATTERN_DESCRIPTIONS))
    ]


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Material.patchwork_pattern = bpy.props.EnumProperty(
        name="Texture-Pattern",
        description="Texture-Logik / pattern type — determines the shape of the anti-tiling regions",
        items=_pattern_enum_items(),
        get=_get_pattern,
        set=_set_pattern,
    )


def unregister():
    try:
        del bpy.types.Material.patchwork_pattern
    except AttributeError:
        pass
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
