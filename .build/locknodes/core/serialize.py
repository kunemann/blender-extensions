"""Subgraph serialization: selected nodes of a node tree ↔ plain dict.

Used by hard lock: serialize the pro set, encrypt, delete; unlock
decrypts and rebuilds. The payload is plain JSON-safe data.

Drivers and keyframe FCurves on the serialized nodes are captured and
restored. Nested node groups are referenced by name only (their internals
are not recursed into by a single-tree serialize; the operator expands them
into their own subtrees for GROUP/MUTED scope).
"""

import bpy

PAYLOAD_SCHEMA = 1

# Properties from the Node base type that ARE worth round-tripping.
# Everything else on the base type (dimensions, select, internal state)
# is skipped; subclass-specific properties are taken generically.
_BASE_WHITELIST = (
    "label",
    "location",
    "width",
    "height",
    "hide",
    "mute",
    "use_custom_color",
    "color",
    "show_options",
    "show_preview",
    "show_texture",
)

_SKIP_PROPS = {"name", "parent", "select", "location_absolute"}

# Keyed by ID.id_type — stable across ID subclasses (a ShaderNodeTree
# reports id_type 'NODETREE', not its RNA identifier).
_ID_LOOKUP = {
    "IMAGE": lambda: bpy.data.images,
    "TEXT": lambda: bpy.data.texts,
    "MATERIAL": lambda: bpy.data.materials,
    "OBJECT": lambda: bpy.data.objects,
    "NODETREE": lambda: bpy.data.node_groups,
    "SCENE": lambda: bpy.data.scenes,
    "WORLD": lambda: bpy.data.worlds,
    "COLLECTION": lambda: bpy.data.collections,
    "TEXTURE": lambda: bpy.data.textures,
    "MESH": lambda: bpy.data.meshes,
    "MOVIECLIP": lambda: bpy.data.movieclips,
    "SOUND": lambda: bpy.data.sounds,
}


class RebuildReport:
    def __init__(self):
        self.warnings = []
        self.restored = 0

    def warn(self, msg):
        self.warnings.append(msg)


# ---------------------------------------------------------------------------
# value helpers


def _serialize_value(value):
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    # bpy_prop_array / Vector / Color / Euler
    try:
        return [float(v) for v in value]
    except TypeError:
        return None


def _id_ref(value):
    return {
        "__idref__": True,
        "id_type": value.id_type,
        "name": value.name,
    }


def _resolve_id_ref(ref, report):
    name = ref.get("name", "")
    id_type = ref.get("id_type")
    lookup = _ID_LOOKUP.get(id_type)
    if lookup is not None:
        hit = lookup().get(name)
        if hit is None:
            report.warn(f"Missing datablock {id_type}:{name!r}")
        return hit
    # Fallback: scan bpy.data collections for a matching datablock.
    for coll_name in dir(bpy.data):
        try:
            coll = getattr(bpy.data, coll_name, None)
            if isinstance(coll, bpy.types.bpy_prop_collection):
                hit = coll.get(name)
                if hit is not None and getattr(hit, "id_type", None) == id_type:
                    return hit
        except (AttributeError, TypeError):
            continue
    report.warn(f"Unknown datablock type {id_type!r} for {name!r}")
    return None


# ---------------------------------------------------------------------------
# embedded struct handlers (keyed by struct RNA identifier, not node type,
# so any node carrying e.g. a ColorRamp is covered)


def _serialize_color_ramp(ramp):
    return {
        "color_mode": ramp.color_mode,
        "interpolation": ramp.interpolation,
        "hue_interpolation": ramp.hue_interpolation,
        "elements": [
            {"position": el.position, "color": list(el.color), "alpha": el.alpha}
            for el in ramp.elements
        ],
    }


def _restore_color_ramp(ramp, data, report):
    ramp.color_mode = data.get("color_mode", "RGB")
    ramp.interpolation = data.get("interpolation", "LINEAR")
    ramp.hue_interpolation = data.get("hue_interpolation", "NEAR")
    elements = data.get("elements", [])
    if not elements:
        return
    # A fresh ramp has 2 elements; a ramp can never have fewer than 1.
    while len(ramp.elements) > 1:
        ramp.elements.remove(ramp.elements[-1])
    while len(ramp.elements) < len(elements):
        ramp.elements.new(1.0)
    for el, el_data in zip(ramp.elements, elements):
        el.position = el_data["position"]
        el.color = el_data["color"]
        if "alpha" in el_data:
            el.alpha = el_data["alpha"]


def _serialize_curve_mapping(mapping):
    return {
        "use_clip": mapping.use_clip,
        "clip_min_x": mapping.clip_min_x,
        "clip_min_y": mapping.clip_min_y,
        "clip_max_x": mapping.clip_max_x,
        "clip_max_y": mapping.clip_max_y,
        "extend": mapping.extend,
        "tone": mapping.tone,
        "black_level": list(mapping.black_level),
        "white_level": list(mapping.white_level),
        "curves": [
            {
                "points": [
                    {"location": list(pt.location), "handle_type": pt.handle_type}
                    for pt in curve.points
                ]
            }
            for curve in mapping.curves
        ],
    }


def _restore_curve_mapping(mapping, data, report):
    for attr in ("use_clip", "clip_min_x", "clip_min_y", "clip_max_x",
                 "clip_max_y", "extend", "tone", "black_level", "white_level"):
        if attr in data:
            try:
                setattr(mapping, attr, data[attr])
            except (AttributeError, TypeError, ValueError):
                report.warn(f"CurveMapping.{attr} not restorable")
    for curve, curve_data in zip(mapping.curves, data.get("curves", [])):
        points = curve_data.get("points", [])
        if len(points) < 2:
            continue
        # A fresh curve has 2 points; points below 2 cannot be removed.
        while len(curve.points) > 2:
            curve.points.remove(curve.points[-1])
        while len(curve.points) < len(points):
            curve.points.new(0.0, 0.0)
        for pt, pt_data in zip(curve.points, points):
            pt.location = pt_data["location"]
            pt.handle_type = pt_data.get("handle_type", "AUTO")
    mapping.update()


def _serialize_generic_struct(struct):
    out = {}
    for prop in struct.bl_rna.properties:
        if prop.is_readonly or prop.identifier == "rna_type":
            continue
        if prop.type in {"BOOLEAN", "INT", "FLOAT", "STRING", "ENUM"}:
            value = getattr(struct, prop.identifier)
            if getattr(prop, "is_array", False):
                out[prop.identifier] = _serialize_value(value)
            elif prop.type == "ENUM" and getattr(prop, "is_enum_flag", False):
                out[prop.identifier] = sorted(value)
            else:
                out[prop.identifier] = _serialize_value(value)
    return out


def _restore_generic_struct(struct, data, report):
    for key, value in data.items():
        try:
            if isinstance(value, list) and struct.bl_rna.properties[key].type == "ENUM":
                setattr(struct, key, set(value))
            else:
                setattr(struct, key, value)
        except (AttributeError, TypeError, ValueError, KeyError):
            report.warn(f"{struct.rna_type.identifier}.{key} not restorable")


STRUCT_HANDLERS = {
    "ColorRamp": (_serialize_color_ramp, _restore_color_ramp),
    "CurveMapping": (_serialize_curve_mapping, _restore_curve_mapping),
    "ImageUser": (_serialize_generic_struct, _restore_generic_struct),
    "TexMapping": (_serialize_generic_struct, _restore_generic_struct),
    "ColorMapping": (_serialize_generic_struct, _restore_generic_struct),
}


# ---------------------------------------------------------------------------
# node properties


def _serialize_node_props(node):
    props = {}
    structs = {}
    base_ids = set(bpy.types.Node.bl_rna.properties.keys())

    for prop in node.bl_rna.properties:
        ident = prop.identifier
        if ident == "rna_type" or ident in _SKIP_PROPS:
            continue
        is_base = ident in base_ids
        if is_base and ident not in _BASE_WHITELIST:
            continue

        if prop.type == "POINTER":
            value = getattr(node, ident)
            if value is None:
                if not prop.is_readonly:
                    props[ident] = None
                continue
            if isinstance(value, bpy.types.ID):
                props[ident] = _id_ref(value)
            else:
                handler = STRUCT_HANDLERS.get(value.rna_type.identifier)
                if handler is not None:
                    structs[ident] = {
                        "struct": value.rna_type.identifier,
                        "data": handler[0](value),
                    }
            continue

        if prop.is_readonly or prop.type == "COLLECTION":
            continue

        value = getattr(node, ident)
        if prop.type == "ENUM" and getattr(prop, "is_enum_flag", False):
            props[ident] = {"__enumset__": sorted(value)}
        elif getattr(prop, "is_array", False):
            props[ident] = _serialize_value(value)
        else:
            props[ident] = _serialize_value(value)

    return props, structs


def _apply_dimension(node, key, value):
    """Set node width/height, temporarily relaxing the node type's minimum so
    a stored below-minimum value restores exactly. Blender clamps assignments
    to ``bl_width_min``/``bl_height_min`` (e.g. a reroute's legacy width of 20
    would snap to 100), but a file load keeps the smaller value — so we lower
    the minimum, assign, then restore it (which does not re-clamp)."""
    min_attr = "bl_width_min" if key == "width" else "bl_height_min"
    orig_min = getattr(node, min_attr, None)
    try:
        if isinstance(orig_min, (int, float)) and value < orig_min:
            setattr(node, min_attr, value)
        setattr(node, key, value)
    except (AttributeError, TypeError, ValueError):
        return
    finally:
        if isinstance(orig_min, (int, float)):
            try:
                setattr(node, min_attr, orig_min)
            except (AttributeError, TypeError, ValueError):
                pass


def _apply_node_props(node, props, structs, report):
    # Enum properties first — they can swap the node's socket layout
    # (operation, blend_type, data_type, ...); everything else after.
    def _is_enum(key):
        try:
            return node.bl_rna.properties[key].type == "ENUM"
        except KeyError:
            return False

    ordered = sorted(props.items(), key=lambda kv: 0 if _is_enum(kv[0]) else 1)
    deferred_location = None
    for key, value in ordered:
        if key == "location":
            deferred_location = value  # parent-relative, applied after parenting
            continue
        if key in ("width", "height") and isinstance(value, (int, float)):
            _apply_dimension(node, key, value)
            continue
        try:
            if isinstance(value, dict) and value.get("__idref__"):
                setattr(node, key, _resolve_id_ref(value, report))
            elif isinstance(value, dict) and "__enumset__" in value:
                setattr(node, key, set(value["__enumset__"]))
            else:
                setattr(node, key, value)
        except (AttributeError, TypeError, ValueError):
            report.warn(f"{node.name}.{key} not restorable")

    for ident, entry in structs.items():
        handler = STRUCT_HANDLERS.get(entry.get("struct"))
        target = getattr(node, ident, None)
        if handler is None or target is None:
            report.warn(f"{node.name}.{ident}: no handler for embedded data")
            continue
        handler[1](target, entry.get("data", {}), report)

    return deferred_location


# ---------------------------------------------------------------------------
# sockets


def _serialize_sockets(sockets):
    out = []
    for index, sock in enumerate(sockets):
        entry = {
            "identifier": sock.identifier,
            "index": index,
            "hide": sock.hide,
            "hide_value": sock.hide_value,
            "enabled": sock.enabled,
        }
        if hasattr(sock, "default_value"):
            value = sock.default_value
            if isinstance(value, bpy.types.ID):
                entry["default_value"] = _id_ref(value)
            else:
                entry["default_value"] = _serialize_value(value)
        out.append(entry)
    return out


def _find_socket(sockets, identifier, index):
    for sock in sockets:
        if sock.identifier == identifier:
            return sock
    if 0 <= index < len(sockets):
        return sockets[index]
    return None


def _restore_sockets(node, sockets, data, report):
    for entry in data:
        sock = _find_socket(sockets, entry["identifier"], entry["index"])
        if sock is None:
            report.warn(f"{node.name}: socket {entry['identifier']!r} missing")
            continue
        for attr in ("hide", "hide_value", "enabled"):
            try:
                setattr(sock, attr, entry[attr])
            except (KeyError, AttributeError, TypeError):
                pass
        if "default_value" in entry and hasattr(sock, "default_value"):
            value = entry["default_value"]
            try:
                if isinstance(value, dict) and value.get("__idref__"):
                    sock.default_value = _resolve_id_ref(value, report)
                elif value is not None:
                    sock.default_value = value
            except (AttributeError, TypeError, ValueError):
                report.warn(f"{node.name}: default of {entry['identifier']!r} not restorable")


# ---------------------------------------------------------------------------
# public API


def serialize_subgraph(tree, nodes) -> dict:
    """Serialize `nodes` (a subset of tree.nodes) plus every link that
    touches them. Links to nodes outside the set reference those kept
    nodes by name."""
    target = set(nodes)
    target_names = {n.name for n in target}

    node_entries = []
    for node in nodes:
        props, structs = _serialize_node_props(node)
        node_entries.append(
            {
                "bl_idname": node.bl_idname,
                "name": node.name,
                "parent": node.parent.name if node.parent else None,
                "props": props,
                "structs": structs,
                "inputs": _serialize_sockets(node.inputs),
                "outputs": _serialize_sockets(node.outputs),
            }
        )

    links = []
    for link in tree.links:
        from_in = link.from_node.name in target_names
        to_in = link.to_node.name in target_names
        if not (from_in or to_in):
            continue
        links.append(
            {
                "from_node": link.from_node.name,
                "from_socket": link.from_socket.identifier,
                "from_index": _socket_index(link.from_node.outputs, link.from_socket),
                "to_node": link.to_node.name,
                "to_socket": link.to_socket.identifier,
                "to_index": _socket_index(link.to_node.inputs, link.to_socket),
                "muted": link.is_muted,
                "sort_id": getattr(link, "multi_input_sort_id", 0),
                "boundary": not (from_in and to_in),
            }
        )
    links.sort(key=lambda entry: entry["sort_id"])

    return {
        "schema": PAYLOAD_SCHEMA,
        "tree_type": tree.bl_idname,
        "nodes": node_entries,
        "links": links,
        "animation": _serialize_animation(tree, target_names),
    }


def _socket_index(sockets, socket):
    for index, sock in enumerate(sockets):
        if sock == socket:
            return index
    return -1


def _iter_action_fcurves(action):
    """Yield every FCurve in *action* across the slotted layer/strip/channelbag
    structure (Blender 4.4+), falling back to the legacy flat collection."""
    layers = getattr(action, "layers", None)
    if layers is None:
        try:
            yield from action.fcurves
        except AttributeError:
            return
        return
    for layer in layers:
        for strip in layer.strips:
            for cb in getattr(strip, "channelbags", ()):
                yield from cb.fcurves


def _fc_hits_nodes(data_path, names):
    return any(f'nodes["{name}"]' in data_path for name in names)


def find_animated_paths(tree, nodes) -> list:
    """Data paths of drivers/FCurves that point into `nodes`."""
    names = {n.name for n in nodes}
    anim = getattr(tree, "animation_data", None)
    if anim is None:
        return []
    hits = [fc.data_path for fc in anim.drivers if _fc_hits_nodes(fc.data_path, names)]
    if anim.action is not None:
        hits += [fc.data_path for fc in _iter_action_fcurves(anim.action)
                 if _fc_hits_nodes(fc.data_path, names)]
    return hits


_DRIVER_TARGET_ATTRS = ("data_path", "bone_target", "transform_type", "transform_space", "rotation_mode")


def _serialize_keyframes(fc):
    out = []
    for kp in fc.keyframe_points:
        out.append({
            "co": [kp.co[0], kp.co[1]],
            "interpolation": kp.interpolation, "easing": kp.easing,
            "handle_left": [kp.handle_left[0], kp.handle_left[1]],
            "handle_right": [kp.handle_right[0], kp.handle_right[1]],
            "handle_left_type": kp.handle_left_type, "handle_right_type": kp.handle_right_type,
            "amplitude": kp.amplitude, "back": kp.back, "period": kp.period,
        })
    return out


def _restore_keyframes(fc, pts):
    for p in pts:
        co = p["co"]
        kp = fc.keyframe_points.insert(co[0], co[1], options={"FAST"})
        try:
            kp.interpolation = p.get("interpolation", "BEZIER")
            kp.easing = p.get("easing", "AUTO")
            kp.handle_left_type = p.get("handle_left_type", "FREE")
            kp.handle_right_type = p.get("handle_right_type", "FREE")
            kp.handle_left = p.get("handle_left", kp.handle_left)
            kp.handle_right = p.get("handle_right", kp.handle_right)
            for a in ("amplitude", "back", "period"):
                if a in p:
                    setattr(kp, a, p[a])
        except (AttributeError, TypeError, ValueError):
            pass
    fc.update()


def _serialize_driver(fc):
    drv = fc.driver
    variables = []
    for v in drv.variables:
        vd = {"name": v.name, "type": v.type, "targets": []}
        for t in v.targets:
            td = {"id": _id_ref(t.id) if getattr(t, "id", None) is not None else None}
            for a in ("id_type",) + _DRIVER_TARGET_ATTRS:
                if hasattr(t, a):
                    try:
                        td[a] = getattr(t, a)
                    except (AttributeError, TypeError):
                        pass
            vd["targets"].append(td)
        variables.append(vd)
    return {
        "data_path": fc.data_path, "array_index": fc.array_index,
        "type": drv.type, "expression": drv.expression, "use_self": drv.use_self,
        "variables": variables, "keyframes": _serialize_keyframes(fc),
        "extrapolation": fc.extrapolation,
    }


def _restore_driver(ad, d, report):
    try:
        fc = ad.drivers.new(d["data_path"], index=d.get("array_index", 0))
    except (RuntimeError, TypeError, ValueError):
        fc = None
        for ex in ad.drivers:
            if ex.data_path == d["data_path"] and ex.array_index == d.get("array_index", 0):
                fc = ex
                break
        if fc is None:
            report.warn(f"driver {d['data_path']!r} not restorable")
            return
    drv = fc.driver
    try:
        drv.type = d.get("type", "SCRIPTED")
    except (AttributeError, TypeError):
        pass
    while len(drv.variables):
        drv.variables.remove(drv.variables[0])
    for vd in d.get("variables", []):
        v = drv.variables.new()
        v.name = vd.get("name", v.name)
        try:
            v.type = vd.get("type", "SINGLE_PROP")
        except (AttributeError, TypeError):
            pass
        for i, td in enumerate(vd.get("targets", [])):
            if i >= len(v.targets):
                break
            tgt = v.targets[i]
            if td.get("id_type"):
                try:
                    tgt.id_type = td["id_type"]
                except (AttributeError, TypeError):
                    pass
            if td.get("id"):
                tgt.id = _resolve_id_ref(td["id"], report)
            for a in _DRIVER_TARGET_ATTRS:
                if a in td:
                    try:
                        setattr(tgt, a, td[a])
                    except (AttributeError, TypeError, ValueError):
                        pass
    try:
        drv.expression = d.get("expression", "")
        drv.use_self = d.get("use_self", False)
    except (AttributeError, TypeError):
        pass
    _restore_keyframes(fc, d.get("keyframes", []))
    try:
        fc.extrapolation = d.get("extrapolation", "CONSTANT")
    except (AttributeError, TypeError):
        pass


def _ensure_action_channelbag(tree, ad, report):
    act = ad.action
    if act is None:
        act = bpy.data.actions.new(f"{tree.name}_LNAnim")
        ad.action = act
    slot = getattr(ad, "action_slot", None)
    if slot is None:
        try:
            slot = act.slots.new(id_type="NODETREE", name="LNSlot")
            ad.action_slot = slot
        except (AttributeError, RuntimeError, TypeError) as exc:
            report.warn(f"animation slot not creatable: {exc}")
            return None
    layer = act.layers[0] if len(act.layers) else act.layers.new("Layer")
    strip = layer.strips[0] if len(layer.strips) else layer.strips.new(type="KEYFRAME")
    try:
        return strip.channelbag(slot, ensure=True)
    except (AttributeError, RuntimeError, TypeError) as exc:
        report.warn(f"animation channelbag not creatable: {exc}")
        return None


def _serialize_animation(tree, node_names):
    ad = getattr(tree, "animation_data", None)
    if ad is None:
        return None
    drivers = [_serialize_driver(fc) for fc in ad.drivers if _fc_hits_nodes(fc.data_path, node_names)]
    fcurves = []
    if ad.action is not None:
        for fc in _iter_action_fcurves(ad.action):
            if _fc_hits_nodes(fc.data_path, node_names):
                fcurves.append({
                    "data_path": fc.data_path, "array_index": fc.array_index,
                    "keyframes": _serialize_keyframes(fc), "extrapolation": fc.extrapolation,
                })
    if not drivers and not fcurves:
        return None
    return {"drivers": drivers, "fcurves": fcurves}


def _restore_animation(tree, anim, report):
    if not anim:
        return
    ad = tree.animation_data or tree.animation_data_create()
    for d in anim.get("drivers", []):
        _restore_driver(ad, d, report)
    fcs = anim.get("fcurves", [])
    if not fcs:
        return
    cb = _ensure_action_channelbag(tree, ad, report)
    if cb is None:
        report.warn(f"{len(fcs)} keyframe channel(s) not restorable on {tree.name!r}")
        return
    for fd in fcs:
        fc = None
        for ex in cb.fcurves:
            if ex.data_path == fd["data_path"] and ex.array_index == fd.get("array_index", 0):
                fc = ex
                break
        if fc is None:
            try:
                fc = cb.fcurves.new(fd["data_path"], index=fd.get("array_index", 0))
            except (RuntimeError, TypeError, ValueError) as exc:
                report.warn(f"fcurve {fd['data_path']!r} not restorable: {exc}")
                continue
        _restore_keyframes(fc, fd.get("keyframes", []))
        try:
            fc.extrapolation = fd.get("extrapolation", "CONSTANT")
        except (AttributeError, TypeError):
            pass


def rebuild_subgraph(tree, payload: dict) -> RebuildReport:
    report = RebuildReport()
    if payload.get("schema") != PAYLOAD_SCHEMA:
        report.warn(f"Payload schema {payload.get('schema')!r} — attempting anyway")

    name_map = {}
    deferred = []  # (node, entry, deferred_location)

    # Frames first so children can parent to them.
    entries = sorted(
        payload.get("nodes", []),
        key=lambda e: 0 if e["bl_idname"] == "NodeFrame" else 1,
    )

    for entry in entries:
        try:
            node = tree.nodes.new(entry["bl_idname"])
        except RuntimeError as exc:
            report.warn(f"Cannot create {entry['bl_idname']}: {exc}")
            continue
        node.name = entry["name"]
        # Blender auto-renames on collision — track the actual name.
        name_map[entry["name"]] = node.name
        location = _apply_node_props(node, entry.get("props", {}), entry.get("structs", {}), report)
        deferred.append((node, entry, location))
        report.restored += 1

    # Parenting, then parent-relative locations.
    for node, entry, location in deferred:
        parent_name = entry.get("parent")
        if parent_name:
            parent = tree.nodes.get(name_map.get(parent_name, parent_name))
            if parent is not None:
                node.parent = parent
            else:
                report.warn(f"{node.name}: parent frame {parent_name!r} missing")
        if location is not None:
            try:
                node.location = location
            except (TypeError, ValueError):
                pass

    # Socket state/defaults after props (enum swaps regenerated sockets).
    for node, entry, _ in deferred:
        _restore_sockets(node, node.inputs, entry.get("inputs", []), report)
        _restore_sockets(node, node.outputs, entry.get("outputs", []), report)

    # Links — created in recorded multi-input sort order.
    for link in payload.get("links", []):
        from_node = tree.nodes.get(name_map.get(link["from_node"], link["from_node"]))
        to_node = tree.nodes.get(name_map.get(link["to_node"], link["to_node"]))
        if from_node is None or to_node is None:
            missing = link["from_node"] if from_node is None else link["to_node"]
            report.warn(f"Link skipped — node {missing!r} missing")
            continue
        from_sock = _find_socket(from_node.outputs, link["from_socket"], link["from_index"])
        to_sock = _find_socket(to_node.inputs, link["to_socket"], link["to_index"])
        if from_sock is None or to_sock is None:
            report.warn(f"Link skipped — socket missing on {from_node.name!r}→{to_node.name!r}")
            continue
        try:
            new_link = tree.links.new(from_sock, to_sock, handle_dynamic_sockets=True)
        except TypeError:
            new_link = tree.links.new(from_sock, to_sock)
        except RuntimeError as exc:
            report.warn(f"Link {from_node.name!r}→{to_node.name!r} failed: {exc}")
            continue
        if new_link is not None and link.get("muted"):
            new_link.is_muted = True

    if payload.get("animation"):
        _restore_animation(tree, payload["animation"], report)
    return report


# ---------------------------------------------------------------------------
# multi-tree (recursive SELECTED mode)


def serialize_multi_subgraph(targets: list) -> dict:
    """Multiple trees → one payload.

    `targets` is a list of *(tree, node)* pairs from potentially different
    node trees.  The returned payload has a ``subtrees`` array, one entry
    per unique tree, each in the same shape as a single-tree payload plus
    a ``tree_name`` field.
    """
    by_tree = {}
    for tree, node in targets:
        by_tree.setdefault(tree, []).append(node)

    entries = []
    for tree, nodes in by_tree.items():
        sub = serialize_subgraph(tree, nodes)
        sub["tree_name"] = tree.name
        entries.append(sub)

    return {
        "schema": PAYLOAD_SCHEMA,
        "subtrees": entries,
    }


def rebuild_multi_subgraph(payload: dict) -> list[RebuildReport]:
    """Rebuild every subtree listed in *payload*."""
    reports = []
    for sub in payload.get("subtrees", []):
        tree_name = sub.get("tree_name", "")
        tree = bpy.data.node_groups.get(tree_name)
        if tree is None:
            rep = RebuildReport()
            rep.warn(f"Tree {tree_name!r} not found — some nodes may be missing")
            reports.append(rep)
            continue
        reports.append(rebuild_subgraph(tree, sub))
    return reports
