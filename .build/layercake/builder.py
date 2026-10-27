# SPDX-License-Identifier: GPL-3.0-or-later
"""Builds the reusable node groups that reconstruct the Combined image.

Structure (clean, nested, fully editable afterwards):

    LayerCake                       (parent group, one instance per source)
      ├─ LayerCake · Light×Color    (sub-group, reused for Diffuse/Glossy/…)
      │     (Direct + Indirect) × Color
      ├─ LayerCake · Volume         (sub-group)  Direct + Indirect
      └─ Sum chain  + Emission + Environment  → Set Alpha → Combined
"""

import bpy

from . import arrange
from . import compat
from . import passes as P

LIGHTCOLOR_GROUP = "LayerCake · Light×Color"
VOLUME_GROUP = "LayerCake · Volume"
LIGHTSIMPLE_GROUP = "LayerCake · Light×Color (EEVEE)"
PARENT_GROUP = "LayerCake"


def _fresh_group(name):
    old = bpy.data.node_groups.get(name)
    if old is not None:
        bpy.data.node_groups.remove(old)
    return compat.new_comp_group(name)


# --- reusable sub-groups -----------------------------------------------------
def ensure_lightcolor_subgroup():
    """(Direct + Indirect) × Color → Result."""
    g = bpy.data.node_groups.get(LIGHTCOLOR_GROUP)
    if g is not None:
        return g
    g = compat.new_comp_group(LIGHTCOLOR_GROUP)
    compat.new_socket(g, "Direct", "INPUT", "COLOR")
    compat.new_socket(g, "Indirect", "INPUT", "COLOR")
    compat.new_socket(g, "Color", "INPUT", "COLOR")
    compat.new_socket(g, "Result", "OUTPUT", "COLOR")
    gi = g.nodes.new("NodeGroupInput"); gi.location = (-500, 0)
    go = g.nodes.new("NodeGroupOutput"); go.location = (300, 0)
    _, a, b, r, _ = compat.add_mix(g, "ADD")
    add_n = a.node; add_n.location = (-250, 80)
    _, a2, b2, r2, _ = compat.add_mix(g, "MULTIPLY")
    mul_n = a2.node; mul_n.location = (50, 0)
    g.links.new(gi.outputs["Direct"], a)
    g.links.new(gi.outputs["Indirect"], b)
    g.links.new(r, a2)
    g.links.new(gi.outputs["Color"], b2)
    g.links.new(r2, go.inputs["Result"])
    return g


def ensure_volume_subgroup():
    """Direct + Indirect → Result."""
    g = bpy.data.node_groups.get(VOLUME_GROUP)
    if g is not None:
        return g
    g = compat.new_comp_group(VOLUME_GROUP)
    compat.new_socket(g, "Direct", "INPUT", "COLOR")
    compat.new_socket(g, "Indirect", "INPUT", "COLOR")
    compat.new_socket(g, "Result", "OUTPUT", "COLOR")
    gi = g.nodes.new("NodeGroupInput"); gi.location = (-400, 0)
    go = g.nodes.new("NodeGroupOutput"); go.location = (200, 0)
    _, a, b, r, _ = compat.add_mix(g, "ADD")
    g.links.new(gi.outputs["Direct"], a)
    g.links.new(gi.outputs["Indirect"], b)
    g.links.new(r, go.inputs["Result"])
    return g


def ensure_lightsimple_subgroup():
    """Light × Color → Result  (EEVEE has no Direct/Indirect split)."""
    g = bpy.data.node_groups.get(LIGHTSIMPLE_GROUP)
    if g is not None:
        return g
    g = compat.new_comp_group(LIGHTSIMPLE_GROUP)
    compat.new_socket(g, "Light", "INPUT", "COLOR")
    compat.new_socket(g, "Color", "INPUT", "COLOR")
    compat.new_socket(g, "Result", "OUTPUT", "COLOR")
    gi = g.nodes.new("NodeGroupInput"); gi.location = (-400, 0)
    go = g.nodes.new("NodeGroupOutput"); go.location = (200, 0)
    _, a, b, r, _ = compat.add_mix(g, "MULTIPLY")
    g.links.new(gi.outputs["Light"], a)
    g.links.new(gi.outputs["Color"], b)
    g.links.new(r, go.inputs["Result"])
    return g


# --- parent group ------------------------------------------------------------
def _apply_image_frame_state(node, state):
    """Restore the sequence/frame controls captured by the operator onto a freshly
    created Image / Cryptomatte node so an EXR SEQUENCE keeps resolving to the same
    frames (5.x: on the node; 4.x: on node.image_user)."""
    if not state:
        return
    iu = getattr(node, "image_user", None)
    for attr, val in state.items():
        if val is None:
            continue
        target = node if hasattr(node, attr) else (iu if iu is not None and hasattr(iu, attr) else None)
        if target is None:
            continue
        try:
            setattr(target, attr, val)
        except (AttributeError, TypeError, ValueError):
            pass


def _embed_source_node(g, embed):
    """Recreate the source node (Render Layers / multilayer EXR Image) inside the
    group so it becomes self-contained. Returns the node, or None on failure."""
    try:
        node = g.nodes.new(embed["type"])
    except RuntimeError:
        return None
    if embed["type"] == "CompositorNodeRLayers":
        if embed.get("scene"):
            node.scene = embed["scene"]
        if embed.get("layer"):
            try:
                node.layer = embed["layer"]
            except (TypeError, ValueError):
                pass
    elif embed["type"] == "CompositorNodeImage":
        if embed.get("image"):
            node.image = embed["image"]
        # Restore frame state before layer/view so the right sequence frame is
        # loaded and the layer/view enums it exposes are valid.
        _apply_image_frame_state(node, embed.get("frames"))
        for attr in ("layer", "view"):
            if embed.get(attr) is not None and hasattr(node, attr):
                try:
                    setattr(node, attr, embed[attr])
                except (TypeError, ValueError):
                    pass
    return node


# --- Cryptomatte pickers (live inside the LayerCake group) -------------------
_CRYPTO_LAYER = {"object": "CryptoObject", "material": "CryptoMaterial", "asset": "CryptoAsset"}


def _set_crypto_layer(cm, crypto_type, scene):
    """Point a picker at a layer; the enum id is view-layer-prefixed
    (e.g. 'ViewLayer.CryptoObject'), so match by suffix."""
    try:
        for it in cm.bl_rna.properties["layer_name"].enum_items:
            if it.identifier.rsplit(".", 1)[-1] == crypto_type:
                cm.layer_name = it.identifier
                return
    except (KeyError, AttributeError, TypeError):
        pass
    if scene and scene.view_layers:
        for cand in ("%s.%s" % (scene.view_layers[0].name, crypto_type), crypto_type):
            try:
                cm.layer_name = cand
                return
            except (TypeError, ValueError):
                continue


def _make_crypto_picker(g, kind, crypto):
    """Create + configure a Cryptomatte V2 picker inside group ``g``."""
    try:
        cm = g.nodes.new("CompositorNodeCryptomatteV2")
    except RuntimeError:
        try:
            cm = g.nodes.new("CompositorNodeCryptomatte")
        except RuntimeError:
            return None
    scene = crypto.get("scene")
    if crypto.get("src_kind") == "CompositorNodeImage":
        try:
            cm.source = "IMAGE"
            if crypto.get("image"):
                cm.image = crypto["image"]
        except (AttributeError, TypeError):
            pass
        # Keep the picker on the same EXR-sequence frame as the source, so its
        # mattes line up with the reconstructed Combined image.
        _apply_image_frame_state(cm, crypto.get("frames"))
    else:
        try:
            cm.source = "RENDER"
            if scene:
                cm.scene = scene
        except (AttributeError, TypeError):
            pass
    _set_crypto_layer(cm, _CRYPTO_LAYER[kind], scene)
    cm.label = "Crypto " + kind.title()
    return cm


def _frame_nodes(g, label, nodes):
    """Wrap nodes in a labelled NodeFrame for visual grouping (node_arrange treats
    frames as clusters and keeps their members together)."""
    nodes = [n for n in nodes if n is not None]
    if not nodes:
        return None
    frame = g.nodes.new("NodeFrame")
    frame.label = label
    try:
        frame.label_size = 18
    except (AttributeError, TypeError):
        pass
    for n in nodes:
        n.parent = frame
    return frame


def build_parent_group(avail, engine, opts, src_order=None, embed=None, crypto=None,
                       name=None, src_key=None):
    """Create a 'LayerCake' parent group.

    name      : datablock name for this source's group (default ``PARENT_GROUP``).
                Each source gets its own uniquely-named group so several render
                layers / EXRs can be reconstructed side by side and edited
                independently.
    src_key   : stable source identity stored on the group (custom property
                ``lc_src``) so a rebuild can find and update the right group.
    avail     : dict canonical_key -> source socket name
    engine    : 'CYCLES' or 'EEVEE'
    opts      : dict(film_transparent, include_extras)
    src_order : optional list of the source node's output socket names; in
                external mode the group's pass inputs are sorted to follow it so
                the source wires run straight across.
    embed     : optional dict describing the source node to recreate *inside* the
                group (keys: type, scene, layer, image, view). When given, passes
                are read from that internal node and no pass inputs are exposed.
    crypto    : optional dict(kinds=[...], src_kind, scene, image). For each kind a
                Cryptomatte picker is placed inside the group and its Matte exposed
                as an output; pick its entries by entering the group and using the
                node's ＋ eyedropper.

    Besides the pass inputs, the group exposes a 0..1 multiply factor per pass so
    the mix can be tuned from the outside without entering the group.

    Returns (group, link_map). In embedded mode link_map is empty.
    """
    g = _fresh_group(name or PARENT_GROUP)
    g["lc_role"] = "parent"               # tag so instances are recognisable by role
    if src_key is not None:
        g["lc_src"] = src_key             # which source this group reconstructs
    gi = g.nodes.new("NodeGroupInput")
    go = g.nodes.new("NodeGroupOutput")

    emb = _embed_source_node(g, embed) if embed else None

    link_map = {}      # group input socket name -> source socket name
    input_kinds = {}   # group input socket name -> kind (to create once, ordered)
    input_order = []

    def want_input(key, kind="COLOR", name=None):
        """Register an external pass input (no-op in embedded mode)."""
        if emb is not None:
            return
        nm = name or P.CANON_DISPLAY.get(key, key)
        if nm not in input_kinds:
            input_kinds[nm] = kind
            input_order.append(nm)
            link_map[nm] = avail[key] if name is None else key

    def feed(key, name=None):
        """Output socket carrying canonical ``key`` (or raw socket ``name``)."""
        if emb is not None:
            return emb.outputs.get(name if name is not None else avail[key])
        return gi.outputs.get(name or P.CANON_DISPLAY.get(key, key))

    # decide light categories that are fully present
    cats = []  # (kind, label, [keys])
    if engine == "EEVEE":
        for label, light, color in P.LIGHT_GROUPS_EEVEE:
            if light in avail and color in avail:
                cats.append(("simple", label, [light, color]))
        if P.VOL_LIGHT in avail:
            cats.append(("additive", "Volume Light", [P.VOL_LIGHT]))
    else:
        for label, d, i, c in P.LIGHT_GROUPS_CYCLES:
            if d in avail and i in avail and c in avail:
                cats.append(("lightcolor", label, [d, i, c]))
        _, vd, vi = P.VOLUME_CYCLES
        if vd in avail and vi in avail:
            cats.append(("volume", "Volume", [vd, vi]))

    additive_keys = [P.EMIT] if P.EMIT in avail else []
    if P.ENV in avail and not opts.get("film_transparent"):
        additive_keys.append(P.ENV)

    # register the external pass inputs we will consume, in a tidy order
    for _, _, keys in cats:
        for k in keys:
            want_input(k)
    for k in additive_keys:
        want_input(k)
    if P.ALPHA in avail:
        want_input(P.ALPHA, "FLOAT")
    extras = []
    if opts.get("include_extras"):
        extras = [k for k in P.EXTRA_PASSTHROUGH if k in avail]
        for k in extras:
            want_input(k, P.kind_of(k))

    if src_order and emb is None:
        pos = {nm: i for i, nm in enumerate(src_order)}
        input_order.sort(key=lambda nm: pos.get(link_map.get(nm), len(src_order)))

    for nm in input_order:
        compat.new_socket(g, nm, "INPUT", input_kinds[nm])

    # Exposed value parameters: a multiply factor per participating pass
    # (Direct / Indirect / Light / Volume / Emission / Environment). Colour
    # (albedo) passes stay unscaled. These exist in both modes so the mix can be
    # rebalanced straight from the group node without entering it.
    factor_keys = []  # ordered, no duplicates
    for kind, label, keys in cats:
        if kind in ("lightcolor", "volume"):
            factor_keys += [keys[0], keys[1]]       # Direct, Indirect
        elif kind == "simple":
            factor_keys += [keys[0]]                # Light
        elif kind == "additive":
            factor_keys += [keys[0]]
    factor_keys += list(additive_keys)

    # Put the factor sliders in a collapsible interface panel (closed by default)
    # so the group node stays compact until you expand it.
    factor_panel = compat.new_panel(g, "Pass Factors", default_closed=True)
    factor_name = {}
    for k in factor_keys:
        nm = "%s ×" % P.CANON_DISPLAY.get(k, k)
        factor_name[k] = nm
        item = compat.new_socket(g, nm, "INPUT", "FLOAT", parent=factor_panel)
        try:
            item.default_value = 1.0
            item.min_value = 0.0
            item.max_value = 1.0
            item.subtype = "FACTOR"
        except (AttributeError, TypeError):
            pass

    def pin(key, collect=None):
        """feed(key) multiplied by its exposed factor (identity if it has none)."""
        sock = feed(key)
        nm = factor_name.get(key)
        if nm is None:
            return sock
        n, a, b, res, _ = compat.add_mix(g, "MULTIPLY")
        n.label = nm
        g.links.new(sock, a)
        gp = gi.outputs.get(nm)
        if gp is not None:
            g.links.new(gp, b)
        if collect is not None:
            collect.append(n)
        return res

    # build category result sockets (Direct/Indirect scaled per pass; Color raw).
    # Collect each category's nodes so we can frame them together afterwards.
    results = []
    cat_members = []  # (label, [nodes])
    for kind, label, keys in cats:
        members = []
        if kind == "lightcolor":
            inst = compat.add_group_instance(g, ensure_lightcolor_subgroup())
            inst.label = label
            d, i, c = keys
            g.links.new(pin(d, members), inst.inputs["Direct"])
            g.links.new(pin(i, members), inst.inputs["Indirect"])
            g.links.new(feed(c), inst.inputs["Color"])
            members.append(inst)
            results.append(inst.outputs["Result"])
        elif kind == "simple":
            inst = compat.add_group_instance(g, ensure_lightsimple_subgroup())
            inst.label = label
            l, c = keys
            g.links.new(pin(l, members), inst.inputs["Light"])
            g.links.new(feed(c), inst.inputs["Color"])
            members.append(inst)
            results.append(inst.outputs["Result"])
        elif kind == "volume":
            inst = compat.add_group_instance(g, ensure_volume_subgroup())
            inst.label = label
            d, i = keys
            g.links.new(pin(d, members), inst.inputs["Direct"])
            g.links.new(pin(i, members), inst.inputs["Indirect"])
            members.append(inst)
            results.append(inst.outputs["Result"])
        else:  # additive (e.g. EEVEE Volume Light)
            results.append(pin(keys[0], members))
        cat_members.append((label, members))

    for k in additive_keys:
        results.append(pin(k))

    # sum everything
    combine_nodes = []
    combined = None
    if results:
        combined = results[0]
        for r in results[1:]:
            n, a, b, res, _ = compat.add_mix(g, "ADD")
            n.label = "+"
            g.links.new(combined, a)
            g.links.new(r, b)
            combine_nodes.append(n)
            combined = res

    # alpha
    if combined is not None and P.ALPHA in avail:
        sa = compat.add_set_alpha(g)
        sa.label = "Set Alpha"
        g.links.new(combined, sa.inputs["Image"])
        g.links.new(feed(P.ALPHA), sa.inputs["Alpha"])
        combine_nodes.append(sa)
        combined = sa.outputs["Image"]

    # outputs
    if combined is not None:
        compat.new_socket(g, "Combined", "OUTPUT", "COLOR")
        g.links.new(combined, go.inputs["Combined"])

    # Cryptomatte pickers live inside the group; expose their Matte. Pick entries
    # by entering the group and using the node's ＋ eyedropper.
    crypto_nodes = []
    if crypto and crypto.get("kinds") and combined is not None:
        for kind in crypto["kinds"]:
            cm = _make_crypto_picker(g, kind, crypto)
            if cm is None:
                continue
            crypto_nodes.append(cm)
            img_in = cm.inputs.get("Image")
            if img_in is not None:
                g.links.new(combined, img_in)
            matte = cm.outputs.get("Matte")
            if matte is not None:
                out_name = "%s Matte" % kind.title()
                compat.new_socket(g, out_name, "OUTPUT", "FLOAT")
                g.links.new(matte, go.inputs[out_name])
            # also expose the false-colour Pick view so the picker can be shown
            # in a top-level viewer without entering the group
            pick = cm.outputs.get("Pick")
            if pick is not None:
                pick_name = "%s Pick" % kind.title()
                compat.new_socket(g, pick_name, "OUTPUT", "COLOR")
                g.links.new(pick, go.inputs[pick_name])

    for k in extras:
        nm = P.CANON_DISPLAY[k]
        compat.new_socket(g, nm, "OUTPUT", P.kind_of(k))
        g.links.new(feed(k), go.inputs[nm])

    # Baseline column layout (fallback). The operator prefers the 'node_arrange'
    # extension's Sugiyama layout afterwards, which does its own edge routing.
    arrange.arrange(g, x_gap=130.0, y_gap=90.0, reroute=False)

    # Cluster related nodes into labelled frames (positions already set above, so
    # parenting preserves them). node_arrange treats frames as clusters, keeping
    # each light group / the combine chain / the pickers together.
    for label, members in cat_members:
        _frame_nodes(g, label, members)
    _frame_nodes(g, "Combine", combine_nodes)
    _frame_nodes(g, "Cryptomatte", crypto_nodes)

    return g, link_map
