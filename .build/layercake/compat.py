# SPDX-License-Identifier: GPL-3.0-or-later
"""Version compatibility layer.

Blender 5.x moved the compositor onto the unified node system. The breaking
changes this module hides:

* The compositor tree lives in ``scene.compositing_node_group`` (a regular node
  group whose Group Output "Image" socket is the result), not ``scene.node_tree``.
* ``CompositorNodeMath`` / ``CompositorNodeMixRGB`` / ``CompositorNodeComposite``
  no longer exist; use ``ShaderNodeMath`` / ``ShaderNodeMix`` instead.
* Node-group sockets are created via ``tree.interface.new_socket(...)`` (4.0+),
  not ``tree.inputs/outputs.new(...)``.
"""

import bpy

_SOCKET_TYPE = {"COLOR": "NodeSocketColor", "FLOAT": "NodeSocketFloat", "VECTOR": "NodeSocketVector"}


def using_unified_compositor():
    """True when the unified (5.x-style) compositor API is present."""
    return hasattr(bpy.types.Scene, "compositing_node_group")


# --- node tree access --------------------------------------------------------
def get_comp_tree(scene, create=False):
    """Return the scene's top-level compositor node tree (or None)."""
    if hasattr(scene, "compositing_node_group"):
        tree = scene.compositing_node_group
        if tree is None and create:
            tree = bpy.data.node_groups.new("Compositing Nodes", "CompositorNodeTree")
            scene.compositing_node_group = tree
        return tree
    # legacy
    if create:
        scene.use_nodes = True
    return scene.node_tree


def new_comp_group(name):
    return bpy.data.node_groups.new(name, "CompositorNodeTree")


# --- socket helpers ----------------------------------------------------------
def _find_socket(collection, name, sock_type):
    for s in collection:
        if s.name == name and s.type == sock_type:
            return s
    # fall back to name only (legacy nodes have unique names)
    for s in collection:
        if s.name == name:
            return s
    return None


def new_socket(group, name, in_out, kind, parent=None):
    """Add an interface socket to a node group. ``kind`` in COLOR/FLOAT/VECTOR.
    ``parent`` optionally places it inside an interface panel (4.0+)."""
    st = _SOCKET_TYPE[kind]
    iface = getattr(group, "interface", None)
    if iface is not None:
        if parent is not None:
            try:
                return iface.new_socket(name=name, in_out=in_out, socket_type=st, parent=parent)
            except TypeError:
                pass
        return iface.new_socket(name=name, in_out=in_out, socket_type=st)
    # pre-4.0 legacy
    coll = group.inputs if in_out == "INPUT" else group.outputs
    return coll.new(st, name)


def new_panel(group, name, default_closed=True):
    """Create a collapsible interface panel (4.0+), or None if unsupported."""
    iface = getattr(group, "interface", None)
    if iface is None or not hasattr(iface, "new_panel"):
        return None
    try:
        return iface.new_panel(name, default_closed=default_closed)
    except TypeError:
        try:
            return iface.new_panel(name)
        except (RuntimeError, TypeError):
            return None


# --- math / mix nodes --------------------------------------------------------
def add_math(tree, operation):
    try:
        n = tree.nodes.new("ShaderNodeMath")
    except RuntimeError:
        n = tree.nodes.new("CompositorNodeMath")
    n.operation = operation
    return n


def add_mix(tree, blend):
    """Add an RGBA mix node. Returns (node, A, B, Result, Factor).

    factor is preset to 1.0 so ADD -> A+B and MULTIPLY -> A*B exactly.
    """
    try:
        n = tree.nodes.new("ShaderNodeMix")
        n.data_type = "RGBA"
        n.blend_type = blend
        a = _find_socket(n.inputs, "A", "RGBA")
        b = _find_socket(n.inputs, "B", "RGBA")
        result = _find_socket(n.outputs, "Result", "RGBA")
        factor = _find_socket(n.inputs, "Factor", "VALUE")
    except RuntimeError:
        n = tree.nodes.new("CompositorNodeMixRGB")
        n.blend_type = blend
        factor = n.inputs[0]
        a = n.inputs[1]
        b = n.inputs[2]
        result = n.outputs[0]
    if factor is not None:
        factor.default_value = 1.0
    return n, a, b, result, factor


def add_set_alpha(tree):
    return tree.nodes.new("CompositorNodeSetAlpha")


def add_viewer(tree):
    return tree.nodes.new("CompositorNodeViewer")


def add_group_instance(tree, group):
    n = tree.nodes.new("CompositorNodeGroup")
    n.node_tree = group
    return n


def ensure_group_output_image(tree):
    """For a top-level compositing group, make sure a Group Output node with an
    'Image' socket exists and return (node, image_input_socket)."""
    out = next((n for n in tree.nodes if n.bl_idname == "NodeGroupOutput"), None)
    if out is None:
        out = tree.nodes.new("NodeGroupOutput")
    has_img = any(
        getattr(i, "in_out", None) == "OUTPUT" and i.name == "Image"
        for i in getattr(tree.interface, "items_tree", [])
    ) if hasattr(tree, "interface") else len(tree.outputs)
    if not has_img:
        new_socket(tree, "Image", "OUTPUT", "COLOR")
    img_in = _find_socket(out.inputs, "Image", "RGBA") or (out.inputs[0] if len(out.inputs) else None)
    return out, img_in
