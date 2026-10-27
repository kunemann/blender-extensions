"""Tree-type abstraction seam.

Everything tree-type-specific funnels through this module so that
enforcement, state, serialization, operators and UI stay generic.
Extending LockNodes to another editor means widening the two sets below
and adding the tree enumeration for that editor.
"""

import bpy

SUPPORTED_TREE_TYPES = {"ShaderNodeTree", "CompositorNodeTree", "GeometryNodeTree"}
GROUP_NODE_IDNAMES = {"ShaderNodeGroup", "CompositorNodeGroup", "GeometryNodeGroup"}

# The group-node idname that instances a tree of a given bl_idname. A node group
# node must match its host tree's type — a ShaderNodeGroup cannot live in a
# CompositorNodeTree and vice versa.
GROUP_IDNAME_FOR_TREE = {
    "ShaderNodeTree": "ShaderNodeGroup",
    "CompositorNodeTree": "CompositorNodeGroup",
    "GeometryNodeTree": "GeometryNodeGroup",
}

# Never delete these on hard lock — they define the group interface.
INTERFACE_NODE_IDNAMES = {"NodeGroupInput", "NodeGroupOutput"}


def group_node_idname(tree_type: str) -> str:
    """The group-node bl_idname to create inside a tree of *tree_type*."""
    return GROUP_IDNAME_FOR_TREE.get(tree_type, "ShaderNodeGroup")


def scene_compositor_tree(scene):
    """The scene's top-level compositor NodeTree, or None.

    5.x exposes it as ``scene.compositing_node_group``; 4.5 as
    ``scene.node_tree`` (only populated when ``use_nodes`` is on)."""
    nt = getattr(scene, "compositing_node_group", None)
    if nt is not None:
        return nt
    if getattr(scene, "use_nodes", False):
        return getattr(scene, "node_tree", None)
    return None


def iter_all_trees():
    """Yield every candidate NodeTree that could carry a lock.

    Embedded material/world trees and the scene compositor tree are NOT in
    bpy.data.node_groups and must be reached through their owner ID.
    """
    # Dedupe by identity: in 5.x the scene compositor tree is itself a
    # bpy.data.node_groups datablock, so it would otherwise be yielded twice.
    seen = set()
    for nt in bpy.data.node_groups:
        if nt.bl_idname in SUPPORTED_TREE_TYPES and id(nt) not in seen:
            seen.add(id(nt))
            yield nt
    for mat in bpy.data.materials:
        if mat.node_tree is not None and id(mat.node_tree) not in seen:
            seen.add(id(mat.node_tree))
            yield mat.node_tree
    for world in bpy.data.worlds:
        if world.node_tree is not None and id(world.node_tree) not in seen:
            seen.add(id(world.node_tree))
            yield world.node_tree
    for scene in bpy.data.scenes:
        nt = scene_compositor_tree(scene)
        if nt is not None and id(nt) not in seen:
            seen.add(id(nt))
            yield nt
    # Geometry node groups are already covered by bpy.data.node_groups once
    # GeometryNodeTree joins SUPPORTED_TREE_TYPES.


def resolve_tree(name: str):
    """Find a candidate tree by datablock name, group trees first."""
    nt = bpy.data.node_groups.get(name)
    if nt is not None and nt.bl_idname in SUPPORTED_TREE_TYPES:
        return nt
    for tree in iter_all_trees():
        if tree.name == name:
            return tree
    return None


def is_group_node(node) -> bool:
    return node is not None and node.bl_idname in GROUP_NODE_IDNAMES


def iter_group_instances(target_tree):
    """Yield every (owner_tree, owner_ref, node) where a group node anywhere
    instances *target_tree*. Used to tint the locked group's instances in their
    parents.

    owner_ref is a stable {kind, name} descriptor resolvable via
    resolve_owner_tree(). It exists because embedded material/world trees are
    NOT globally unique by tree name (every material's tree is 'Shader
    Nodetree'), so restoring by tree name alone recolours the wrong node.
    """
    # Dedupe owner trees by identity: in 5.x the scene compositor tree is also a
    # bpy.data.node_groups datablock, so each of its instance nodes would
    # otherwise be reported twice (kind "g" and kind "s") — double-tinting a node
    # and, worse, capturing its "previous" colour as the already-tinted state so
    # unlock could not revert it.
    seen = set()
    for nt in bpy.data.node_groups:
        if nt.bl_idname in SUPPORTED_TREE_TYPES and id(nt) not in seen:
            seen.add(id(nt))
            for node in nt.nodes:
                if is_group_node(node) and node.node_tree is target_tree:
                    yield nt, {"k": "g", "n": nt.name}, node
    for mat in bpy.data.materials:
        nt = mat.node_tree
        if nt is not None and id(nt) not in seen:
            seen.add(id(nt))
            for node in nt.nodes:
                if is_group_node(node) and node.node_tree is target_tree:
                    yield nt, {"k": "m", "n": mat.name}, node
    for world in bpy.data.worlds:
        nt = world.node_tree
        if nt is not None and id(nt) not in seen:
            seen.add(id(nt))
            for node in nt.nodes:
                if is_group_node(node) and node.node_tree is target_tree:
                    yield nt, {"k": "w", "n": world.name}, node
    for scene in bpy.data.scenes:
        nt = scene_compositor_tree(scene)
        if nt is not None and id(nt) not in seen:
            seen.add(id(nt))
            for node in nt.nodes:
                if is_group_node(node) and node.node_tree is target_tree:
                    yield nt, {"k": "s", "n": scene.name}, node


def resolve_owner_tree(ref):
    """Resolve an owner_ref from iter_group_instances back to its node tree.

    Accepts the {kind, name} dict; also tolerates a bare tree-name string from
    locks written before this descriptor existed (legacy fallback)."""
    if isinstance(ref, str):
        return resolve_tree(ref)
    if not isinstance(ref, dict):
        return None
    kind = ref.get("k")
    name = ref.get("n", "")
    if kind == "g":
        nt = bpy.data.node_groups.get(name)
        return nt if (nt is not None and nt.bl_idname in SUPPORTED_TREE_TYPES) else None
    if kind == "m":
        mat = bpy.data.materials.get(name)
        return mat.node_tree if mat is not None else None
    if kind == "w":
        world = bpy.data.worlds.get(name)
        return world.node_tree if world is not None else None
    if kind == "s":
        scene = bpy.data.scenes.get(name)
        return scene_compositor_tree(scene) if scene is not None else None
    return None


def walk_descendant_trees(tree, _seen=None):
    """Yield every node-group tree reachable from *tree* through group nodes
    (depth-first, each tree once), excluding *tree* itself. Cycle-safe."""
    if _seen is None:
        _seen = set()
    for node in tree.nodes:
        if is_group_node(node) and node.node_tree is not None:
            sub = node.node_tree
            if id(sub) in _seen:
                continue
            _seen.add(id(sub))
            yield sub
            yield from walk_descendant_trees(sub, _seen)
