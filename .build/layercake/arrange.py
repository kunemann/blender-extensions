# SPDX-License-Identifier: GPL-3.0-or-later
"""Tidy left-to-right node layout.

Self-contained layered layout (Sugiyama-style: longest-path columns + barycenter
sweeps to reduce crossings). Optionally inserts Reroute nodes on links that span
more than one column, so long wires route cleanly through the intervening
columns instead of cutting diagonally across the tree. If a "Node Arrange"-type
add-on is installed we try its operator first, but everything works without it.

Nodes are tracked by ``name`` (unique within a tree) rather than by Python object
identity — Blender hands out fresh RNA wrappers per access, so identity/sets of
node objects are unreliable.
"""

import bpy


def _est_height(node):
    """Rough on-screen height of a node before it has ever been drawn."""
    if node.bl_idname == "NodeReroute":
        return 24.0
    socks = sum(1 for s in node.inputs if not s.hide) + sum(1 for s in node.outputs if not s.hide)
    return 34.0 + 24.0 * max(socks, 1)


def _columns(by_name, names, preds):
    """Longest-path column index per node name (with a cycle guard)."""
    col = {}

    def depth(nm, stack):
        if nm in col:
            return col[nm]
        if nm in stack:
            return 0
        stack = stack | {nm}
        col[nm] = 1 + max((depth(p, stack) for p in preds[nm]), default=-1)
        return col[nm]

    for nm in names:
        depth(nm, set())
    return col


def _predecessors(tree, names):
    preds = {nm: set() for nm in names}
    for link in tree.links:
        f, t = link.from_node.name, link.to_node.name
        if f in names and t in names and f != t:
            preds[t].add(f)
    return preds


def _insert_reroutes(tree, by_name, names, col):
    """For every output socket that fans across more than one column, lay a shared
    chain of reroutes — one per intervening column — and branch each consumer off
    at its own column. This turns long diagonals into tidy orthogonal rails (a
    bus behind the Group Input) rather than crossing wires. Mutates ``by_name`` /
    ``names`` / ``col`` in place."""
    groups = {}   # (node_name, socket_identifier) -> [(to_socket, to_column, link)]
    meta = {}     # key -> (from_socket, source_column)
    for link in list(tree.links):
        f, t = link.from_node.name, link.to_node.name
        # skip straight pass-throughs to the Group Output (extras / Matte / Pick):
        # rerouting those just adds long parallel chains without untangling anything.
        if link.to_node.bl_idname == "NodeGroupOutput":
            continue
        if f in names and t in names and (col.get(t, 0) - col.get(f, 0)) >= 2:
            key = (f, link.from_socket.identifier)
            groups.setdefault(key, []).append((link.to_socket, col[t], link))
            meta[key] = (link.from_socket, col[f])

    for key, items in groups.items():
        from_sock, a = meta[key]
        maxc = max(c for (_, c, _) in items)
        # shared rail: one reroute per column from a+1 up to maxc-1
        chain = {}
        prev = from_sock
        for c in range(a + 1, maxc):
            r = tree.nodes.new("NodeReroute")
            by_name[r.name] = r
            names.add(r.name)
            col[r.name] = c
            tree.links.new(prev, r.inputs[0])
            chain[c] = r
            prev = r.outputs[0]
        # branch each consumer off the rail at the column just before it
        for to_sock, c, link in items:
            try:
                tree.links.remove(link)
            except (RuntimeError, ReferenceError):
                pass
            rail = chain.get(c - 1)
            tree.links.new(rail.outputs[0] if rail is not None else from_sock, to_sock)


def arrange(tree, nodes=None, x_gap=70.0, y_gap=45.0, origin=(0.0, 0.0), reroute=False):
    """Lay out ``nodes`` (default: all) of ``tree`` in dependency columns."""
    nodes = list(nodes) if nodes is not None else list(tree.nodes)
    nodes = [n for n in nodes if n.bl_idname != "NodeFrame"]
    if not nodes:
        return

    by_name = {n.name: n for n in nodes}
    names = set(by_name)
    preds = _predecessors(tree, names)
    col = _columns(by_name, names, preds)

    if reroute:
        _insert_reroutes(tree, by_name, names, col)
        preds = _predecessors(tree, names)  # reroutes are now part of the graph

    columns = {}
    for nm in names:
        columns.setdefault(col[nm], []).append(nm)

    # initial within-column order: stable by name
    order = {}
    for c in columns:
        for i, nm in enumerate(sorted(columns[c])):
            order[nm] = i

    # barycenter sweeps against predecessor positions
    for _ in range(6):
        for c in sorted(columns):
            lst = columns[c]

            def bary(nm):
                ps = [order[p] for p in preds[nm] if order.get(p) is not None and col[p] < c]
                return sum(ps) / len(ps) if ps else order[nm]

            lst.sort(key=bary)
            for i, nm in enumerate(lst):
                order[nm] = i

    ox, oy = origin
    x = ox
    for c in sorted(columns):
        lst = sorted(columns[c], key=lambda nm: order[nm])
        y = oy
        for nm in lst:
            n = by_name[nm]
            n.location = (x, y)
            y -= _est_height(n) + y_gap
        colw = max((by_name[nm].width for nm in lst), default=160.0)
        x += colw + x_gap


def try_external_arrange(context):
    """Best-effort call into a "Node Arrange"-style add-on, if one is present.
    Returns True if an external arranger ran. Never raises."""
    candidates = (
        ("node", "button_arrange"),   # JuhaW / Node Arrange
        ("node", "na_main_op"),
        ("node", "arrange_nodes"),
    )
    for mod, op in candidates:
        fn = getattr(getattr(bpy.ops, mod, None), op, None)
        if fn is None:
            continue
        try:
            if fn.poll():
                fn()
                return True
        except Exception:
            continue
    return False
