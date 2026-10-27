"""Lock, unlock and finalize operators."""

import datetime
import json
import math
import os
import re

import bpy

from . import prefs
from .core import crypto, serialize, state, trees

# Visual language for locked groups.
LOCKED_NODE_COLOR = (0.0, 0.0, 0.0)      # black tint on locked group node headers
PLACEHOLDER_COLOR = (0.40, 0.40, 0.40)   # flat base colour the locked group outputs
PLACEHOLDER_TAG = "ln_locked_placeholder"
PLACEHOLDER_IMG_NAME = "ln_locked_label"  # baked vendor-label texture
PLACEHOLDER_TEXT_TILES = 3                # times the label repeats across the output
PLACEHOLDER_TEXT_ROWS = 4                 # staggered text rows baked into one tile
PLACEHOLDER_TEXT_ANGLE = -45.0            # degrees the tiled label is rotated


def active_group_node(context):
    node = getattr(context, "active_node", None)
    if trees.is_group_node(node) and node.node_tree is not None:
        return node
    return None


def _poll_group(context, locked):
    space = context.space_data
    if space is None or space.type != "NODE_EDITOR" or space.edit_tree is None:
        return False
    node = active_group_node(context)
    if node is None:
        return False
    return state.is_locked(node.node_tree) == locked


def _lockable(operator, tree) -> bool:
    if tree.library is not None or not tree.is_editable:
        operator.report(
            {"ERROR"},
            "Linked node groups cannot be locked here — lock them in their source file",
        )
        return False
    if not prefs.master_password_set():
        operator.report(
            {"ERROR"}, "Set a master password in the LockNodes add-on preferences first"
        )
        return False
    return True


def _base_lock_data(password: str) -> dict:
    iters = prefs.get_prefs().kdf_iters
    salt, digest = crypto.make_verify_pair(password, iters)
    return {
        "schema": state.SCHEMA_VERSION,
        "kdf": "pbkdf2_sha256",
        "kdf_iters": iters,
        "verify_salt": salt,
        "verify_hash": digest,
        "addon_version": state.ADDON_VERSION,
        "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }


def _bypass_node(tree, node):
    """Wire muted node's inputs directly to outputs so signal flows
    after the node is removed.  Returns list of bypass descriptors
    so they can be cleaned up on unlock."""
    bypasses = []
    for idx, in_sock in enumerate(node.inputs):
        if not in_sock.is_linked:
            continue
        if idx >= len(node.outputs):
            continue
        out_sock = node.outputs[idx]
        if not out_sock.is_linked:
            continue
        for in_link in in_sock.links:
            from_sock = in_link.from_socket
            if from_sock is None or from_sock.node is None:
                continue
            from_node_name = from_sock.node.name
            from_sock_id = from_sock.identifier
            # Snapshot the downstream targets BEFORE creating any links —
            # tree.links.new() reallocates the link collection and invalidates
            # every existing NodeLink reference (reading out_link afterwards
            # returns a dangling struct whose .to_node is None).
            downstream = []
            for out_link in out_sock.links:
                to_node = out_link.to_node
                to_socket = out_link.to_socket
                if to_node is not None and to_socket is not None:
                    downstream.append((to_node.name, to_socket))
            for to_node_name, to_socket in downstream:
                try:
                    tree.links.new(from_sock, to_socket, handle_dynamic_sockets=True)
                except TypeError:
                    try:
                        tree.links.new(from_sock, to_socket)
                    except RuntimeError:
                        continue
                except RuntimeError:
                    continue
                bypasses.append({
                    "tree": tree.name,
                    "from_node": from_node_name,
                    "from_socket": from_sock_id,
                    "to_node": to_node_name,
                    "to_socket": to_socket.identifier,
                })
    return bypasses


def _remove_bypass_links(lock):
    try:
        bypasses = json.loads(lock.get("bypass_links", "[]"))
    except (KeyError, ValueError, TypeError):
        return
    for entry in bypasses:
        t = bpy.data.node_groups.get(entry.get("tree", ""))
        if t is None:
            continue
        fn = t.nodes.get(entry.get("from_node", ""))
        tn = t.nodes.get(entry.get("to_node", ""))
        if fn is None or tn is None:
            continue
        fs_id = entry.get("from_socket", "")
        ts_id = entry.get("to_socket", "")
        for link in list(t.links):
            if (link.from_node == fn and link.to_node == tn and
                    link.from_socket.identifier == fs_id and
                    link.to_socket.identifier == ts_id):
                t.links.remove(link)
                break


def _bypass_group_tree(tree):
    """Connect NodeGroupInput outputs directly to NodeGroupOutput
    inputs by index so the group node still passes signals through
    after its internal nodes are removed."""
    inp = out = None
    for n in tree.nodes:
        if n.bl_idname == "NodeGroupInput":
            inp = n
        elif n.bl_idname == "NodeGroupOutput":
            out = n
    if inp is None or out is None:
        return []
    bypasses = []
    for idx, in_sock in enumerate(inp.outputs):
        if idx >= len(out.inputs):
            continue
        out_sock = out.inputs[idx]
        try:
            tree.links.new(in_sock, out_sock, handle_dynamic_sockets=True)
        except TypeError:
            try:
                tree.links.new(in_sock, out_sock)
            except RuntimeError:
                continue
        except RuntimeError:
            continue
        bypasses.append({
            "tree": tree.name,
            "from_node": inp.name,
            "from_socket": in_sock.identifier,
            "to_node": out.name,
            "to_socket": out_sock.identifier,
        })
    return bypasses


def _bake_label_image(text, text_color=(1.0, 1.0, 1.0), angle=0.0,
                      dim=256, rows=PLACEHOLDER_TEXT_ROWS):
    """Render *text* into a packed, tiling RGBA image (real glyphs, transparent
    background) via the GPU + blf so a locked group can composite the vendor
    message into what it actually *renders*, not just an editor frame header.

    Shader trees tile + rotate this image downstream with a Mapping node, so
    they bake it flat (``angle=0``). The compositor has no per-pixel Mapping,
    so it bakes the rotation and extra tiling straight into the image
    (``angle`` set, larger ``dim``, more ``rows``).

    Returns the new bpy.types.Image (tagged + packed), or None when baking is
    not possible (empty text, no modules, or no live GPU context) — callers
    fall back to a flat placeholder colour."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        import gpu
        import blf
        from mathutils import Matrix
    except Exception:
        return None

    W = H = dim
    row_h = H / rows
    margin = int(W * 0.06)
    font_id = 0
    try:
        # Scale the font so one line spans ~the full tile width — the label then
        # repeats seamlessly across the horizontal wrap.
        size = int(48 * dim / 256)
        blf.size(font_id, size)
        tw, th = blf.dimensions(font_id, text)
        if tw > 0:
            size = max(6, int(size * (W - 2 * margin) / tw))
            blf.size(font_id, size)
            tw, th = blf.dimensions(font_id, text)
        offscreen = gpu.types.GPUOffScreen(W, H)
    except Exception:
        return None

    try:
        with offscreen.bind():
            fb = gpu.state.active_framebuffer_get()
            fb.clear(color=(0.0, 0.0, 0.0, 0.0))
            gpu.state.blend_set("ALPHA")
            if angle:
                cx, cy = W / 2.0, H / 2.0
                model = (Matrix.Translation((cx, cy, 0.0))
                         @ Matrix.Rotation(math.radians(angle), 4, "Z")
                         @ Matrix.Translation((-cx, -cy, 0.0)))
            else:
                model = Matrix.Identity(4)
            gpu.matrix.load_matrix(model)
            gpu.matrix.load_projection_matrix(Matrix((
                (2.0 / W, 0.0, 0.0, -1.0),
                (0.0, 2.0 / H, 0.0, -1.0),
                (0.0, 0.0, -1.0, 0.0),
                (0.0, 0.0, 0.0, 1.0),
            )))
            blf.color(font_id, text_color[0], text_color[1], text_color[2], 1.0)
            blf.size(font_id, size)
            # Several rows, each nudged sideways ("a bit offset per line"), each
            # drawn with horizontal wrap copies so the tile stays seamless. When
            # the rotation is baked in (compositor), the row/wrap ranges widen so
            # the tilted pattern still covers every corner of the canvas.
            r_range = range(-rows, 2 * rows) if angle else range(rows)
            k_range = (-2, -1, 0, 1, 2) if angle else (-1, 0, 1)
            for r in r_range:
                y = r * row_h + (row_h - th) / 2.0
                x0 = r * (W / rows)
                for k in k_range:
                    blf.position(font_id, x0 + k * W, y, 0.0)
                    blf.draw(font_id, text)
            buf = fb.read_color(0, 0, W, H, 4, 0, "FLOAT")
    except Exception:
        try:
            offscreen.free()
        except Exception:
            pass
        return None
    offscreen.free()

    # ALPHA blending leaves the buffer premultiplied; un-premultiply back to
    # straight alpha so a single Mix node does a correct alpha-over.
    buf.dimensions = W * H * 4
    px = list(buf)
    for i in range(0, len(px), 4):
        a = px[i + 3]
        if a > 0.0:
            inv = 1.0 / a
            px[i] *= inv
            px[i + 1] *= inv
            px[i + 2] *= inv

    img = bpy.data.images.new(PLACEHOLDER_IMG_NAME, W, H, alpha=True)
    img[PLACEHOLDER_TAG] = True
    img.alpha_mode = "STRAIGHT"
    try:
        img.pixels.foreach_set(px)
    except Exception:
        img.pixels = px
    img.pack()
    return img


def _build_locked_group(baked_images, tree_type="ShaderNodeTree"):
    """Create the single reusable 'LOCKED' placeholder group of *tree_type*
    that every locked group instances instead of each rebuilding its own nodes.

    Dispatches per editor — the node palette differs (a compositor tree has no
    shaders and no Mapping tiling; a geometry tree outputs geometry, not a
    colour/shader)."""
    if tree_type == "CompositorNodeTree":
        return _build_locked_group_compositor(baked_images)
    if tree_type == "GeometryNodeTree":
        return _build_locked_group_geometry(baked_images)
    return _build_locked_group_shader(baked_images)


def _build_locked_group_compositor(baked_images):
    """Compositor 'LOCKED' placeholder group: exposes a single Color output —
    the base colour with the vendor label tiled + rotated over it (baked into
    one image, alpha-over'd on top). The compositor has no Shader/Value analog
    worth driving, so unlinked Value outputs of a locked group keep their zero
    default. Returns the tagged node-group datablock; appends any baked image
    name to *baked_images*."""
    try:
        p = prefs.get_prefs()
        color = tuple(p.placeholder_color)[:3]
        label = (p.placeholder_label or "").strip()
    except (KeyError, AttributeError):
        color = PLACEHOLDER_COLOR
        label = ""

    grp = bpy.data.node_groups.new("LOCKED", "CompositorNodeTree")
    grp[PLACEHOLDER_TAG] = True
    grp.interface.new_socket("Color", in_out="OUTPUT", socket_type="NodeSocketColor")
    out = grp.nodes.new("NodeGroupOutput")
    out.location = (300.0, 0.0)

    rgb = grp.nodes.new("CompositorNodeRGB")
    rgb.location = (-220.0, 60.0)
    rgb.outputs[0].default_value = (*color, 1.0)
    color_src = rgb.outputs[0]

    # The rotation + tiling the shader does with a Mapping node is baked into
    # the image here, then composited over the base colour with Alpha Over.
    text_img = (
        _bake_label_image(label, angle=PLACEHOLDER_TEXT_ANGLE, dim=1024, rows=6)
        if label else None
    )
    if text_img is not None:
        baked_images.append(text_img.name)
        img = grp.nodes.new("CompositorNodeImage")
        img.location = (-220.0, -180.0)
        img.image = text_img
        over = grp.nodes.new("CompositorNodeAlphaOver")
        over.location = (40.0, 0.0)
        over.inputs["Factor"].default_value = 1.0
        grp.links.new(color_src, over.inputs["Background"])
        grp.links.new(img.outputs["Image"], over.inputs["Foreground"])
        color_src = over.outputs["Image"]

    grp.links.new(color_src, out.inputs["Color"])

    # vendor label as an editor frame inside the reused group
    if label:
        frame = grp.nodes.new("NodeFrame")
        frame.label = label
        frame.label_size = 28
        frame.use_custom_color = True
        frame.color = color
        for n in grp.nodes:
            if n not in (frame, out):
                n.parent = frame
    return grp


def _build_label_geometry(grp, label):
    """Build a String→Curves→Realize→Fill node chain inside *grp* that renders
    *label* as flat mesh text, returning the output geometry socket — or None if
    this Blender build lacks the geometry text palette. Sockets are addressed by
    index so it survives socket-name churn across versions."""
    try:
        s2c = grp.nodes.new("GeometryNodeStringToCurves")
    except RuntimeError:
        return None
    s2c.location = (-360.0, 0.0)
    try:
        s2c.inputs["String"].default_value = label
    except (KeyError, AttributeError, TypeError):
        pass
    src = s2c.outputs[0]  # "Curve Instances"
    # Realize the per-glyph instances so Fill Curve receives real curve data.
    try:
        realize = grp.nodes.new("GeometryNodeRealizeInstances")
        realize.location = (-160.0, 0.0)
        grp.links.new(src, realize.inputs[0])
        src = realize.outputs[0]
    except RuntimeError:
        pass
    # Fill the closed glyph curves into a visible mesh.
    try:
        fill = grp.nodes.new("GeometryNodeFillCurve")
        fill.location = (40.0, 0.0)
        grp.links.new(src, fill.inputs[0])
        return fill.outputs[0]  # "Mesh"
    except RuntimeError:
        return src


def _build_locked_group_geometry(baked_images):
    """Create the single reusable 'LOCKED' placeholder group (a geometry tree)
    that every locked group instances instead of each rebuilding its own nodes.

    It exposes one Geometry output. When a watermark label is set the group
    renders it as flat mesh text, so the vendor message shows in the viewport
    where the pro nodes produced geometry; with no label (or if the geometry
    text palette is missing) the Geometry output is left empty, so the locked
    object's geometry visibly disappears — an unmistakable 'locked' signal. The
    vendor label also sits as an editor frame inside it. Returns the tagged
    node-group datablock. *baked_images* is accepted for signature parity with
    the shader/compositor builders (geometry bakes no image)."""
    try:
        p = prefs.get_prefs()
        color = tuple(p.placeholder_color)[:3]
        label = (p.placeholder_label or "").strip()
    except (KeyError, AttributeError):
        color = PLACEHOLDER_COLOR
        label = ""

    grp = bpy.data.node_groups.new("LOCKED", "GeometryNodeTree")
    grp[PLACEHOLDER_TAG] = True
    grp.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    out = grp.nodes.new("NodeGroupOutput")
    out.location = (300.0, 0.0)

    geo_src = _build_label_geometry(grp, label) if label else None
    if geo_src is not None:
        try:
            grp.links.new(geo_src, out.inputs[0])
        except (RuntimeError, IndexError):
            pass
    # else: Geometry output stays unlinked → empty geometry (object vanishes).

    # vendor label as an editor frame inside the reused group
    if label:
        frame = grp.nodes.new("NodeFrame")
        frame.label = label
        frame.label_size = 28
        frame.use_custom_color = True
        frame.color = color
        for n in grp.nodes:
            if n not in (frame, out):
                n.parent = frame
    return grp


def _build_locked_group_shader(baked_images):
    """Create the single reusable 'LOCKED' placeholder group (a shader tree)
    that every locked group instances instead of each rebuilding its own nodes.

    It exposes Color / Shader / Value outputs: the base colour with the vendor
    label tiled + rotated over it (alpha-over of a baked text image) for
    colour/shader, and a zero constant for value. The vendor label also sits as
    an editor frame inside it. Returns the tagged node-group datablock; appends
    any baked image name to *baked_images*."""
    try:
        p = prefs.get_prefs()
        color = tuple(p.placeholder_color)[:3]
        label = (p.placeholder_label or "").strip()
    except (KeyError, AttributeError):
        color = PLACEHOLDER_COLOR
        label = ""

    grp = bpy.data.node_groups.new("LOCKED", "ShaderNodeTree")
    grp[PLACEHOLDER_TAG] = True
    grp.interface.new_socket("Color", in_out="OUTPUT", socket_type="NodeSocketColor")
    grp.interface.new_socket("Shader", in_out="OUTPUT", socket_type="NodeSocketShader")
    grp.interface.new_socket("Value", in_out="OUTPUT", socket_type="NodeSocketFloat")
    out = grp.nodes.new("NodeGroupOutput")
    out.location = (420.0, 0.0)

    # colour source: base, optionally with the tiled+rotated label alpha-over'd
    text_img = _bake_label_image(label) if label else None
    if text_img is not None:
        baked_images.append(text_img.name)
        coord = grp.nodes.new("ShaderNodeTexCoord")
        coord.location = (-640.0, 0.0)
        mapping = grp.nodes.new("ShaderNodeMapping")
        mapping.location = (-440.0, 0.0)
        mapping.inputs["Scale"].default_value = (PLACEHOLDER_TEXT_TILES,) * 3
        mapping.inputs["Rotation"].default_value = (0.0, 0.0, math.radians(PLACEHOLDER_TEXT_ANGLE))
        tex = grp.nodes.new("ShaderNodeTexImage")
        tex.location = (-240.0, 0.0)
        tex.image = text_img
        tex.extension = "REPEAT"
        grp.links.new(coord.outputs["Generated"], mapping.inputs["Vector"])
        grp.links.new(mapping.outputs["Vector"], tex.inputs["Vector"])
        mix = grp.nodes.new("ShaderNodeMix")
        mix.location = (0.0, 60.0)
        mix.data_type = "RGBA"
        mix.blend_type = "MIX"
        mix.inputs[6].default_value = (*color, 1.0)          # A = base colour
        grp.links.new(tex.outputs["Color"], mix.inputs[7])   # B = text colour
        grp.links.new(tex.outputs["Alpha"], mix.inputs[0])   # Factor = text alpha
        color_src = mix.outputs[2]
    else:
        rgb = grp.nodes.new("ShaderNodeRGB")
        rgb.location = (0.0, 60.0)
        rgb.outputs[0].default_value = (*color, 1.0)
        color_src = rgb.outputs[0]

    emit = grp.nodes.new("ShaderNodeEmission")
    emit.location = (210.0, 140.0)
    grp.links.new(color_src, emit.inputs["Color"])
    val = grp.nodes.new("ShaderNodeValue")
    val.location = (210.0, -140.0)
    val.outputs[0].default_value = 0.0

    grp.links.new(color_src, out.inputs["Color"])
    grp.links.new(emit.outputs["Emission"], out.inputs["Shader"])
    grp.links.new(val.outputs[0], out.inputs["Value"])

    # vendor label as an editor frame inside the reused group
    if label:
        frame = grp.nodes.new("NodeFrame")
        frame.label = label
        frame.label_size = 28
        frame.use_custom_color = True
        frame.color = color
        for n in grp.nodes:
            if n not in (frame, out):
                n.parent = frame
    return grp


_SOCKET_IFACE_TYPE = {
    "RGBA": "NodeSocketColor",
    "VALUE": "NodeSocketFloat",
    "VECTOR": "NodeSocketVector",
    "SHADER": "NodeSocketShader",
    "INT": "NodeSocketInt",
    "BOOLEAN": "NodeSocketBool",
    "STRING": "NodeSocketString",
    "GEOMETRY": "NodeSocketGeometry",
    "ROTATION": "NodeSocketRotation",
    "MATRIX": "NodeSocketMatrix",
    "MENU": "NodeSocketMenu",
}


def _group_locked_subset(tree, nodes):
    """Replace a partially-locked subset of *nodes* with a single empty 'LOCKED'
    group node whose interface preserves every boundary in/output, so links to
    the rest of the graph survive intact — instead of dying or being bypassed
    by socket index (which silently drops operands on multi-input math nodes).

    The real nodes are already serialized; unlock removes this group node + its
    datablock and rebuilds the originals (boundary links are restored from the
    payload). Returns (group_datablock, group_node), or (None, None) when the
    subset has no external links worth preserving."""
    names = {n.name for n in nodes}
    ext_inputs = []   # (external_source_socket, locked_input_socket)
    ext_outputs = []  # (locked_output_socket, external_target_socket)
    for link in tree.links:
        fn, tn = link.from_node, link.to_node
        if fn is None or tn is None:
            continue
        f_in, t_in = fn.name in names, tn.name in names
        if f_in and not t_in:
            ext_outputs.append((link.from_socket, link.to_socket))
        elif t_in and not f_in:
            ext_inputs.append((link.from_socket, link.to_socket))
    if not ext_inputs and not ext_outputs:
        return None, None

    grp = bpy.data.node_groups.new("LOCKED", tree.bl_idname)
    grp[PLACEHOLDER_TAG] = True
    gin = grp.nodes.new("NodeGroupInput")
    gin.location = (-260.0, 0.0)
    gout = grp.nodes.new("NodeGroupOutput")
    gout.location = (260.0, 0.0)

    # one group INPUT per distinct external source socket, one group OUTPUT per
    # distinct locked output socket that feeds outside
    in_index = {}
    for ext_sock, _locked in ext_inputs:
        key = (ext_sock.node.name, ext_sock.identifier)
        if key not in in_index:
            grp.interface.new_socket(
                ext_sock.name or "In", in_out="INPUT",
                socket_type=_SOCKET_IFACE_TYPE.get(ext_sock.type, "NodeSocketFloat"))
            in_index[key] = len(in_index)
    out_index = {}
    for locked_sock, _ext in ext_outputs:
        key = (locked_sock.node.name, locked_sock.identifier)
        if key not in out_index:
            grp.interface.new_socket(
                locked_sock.name or "Out", in_out="OUTPUT",
                socket_type=_SOCKET_IFACE_TYPE.get(locked_sock.type, "NodeSocketFloat"))
            out_index[key] = len(out_index)

    cx = sum(n.location.x for n in nodes) / len(nodes)
    cy = sum(n.location.y for n in nodes) / len(nodes)
    gn = tree.nodes.new(trees.group_node_idname(tree.bl_idname))
    gn.node_tree = grp
    gn[PLACEHOLDER_TAG] = True
    gn.label = "LOCKED"
    gn.use_custom_color = True
    gn.color = LOCKED_NODE_COLOR
    gn.location = (cx, cy)

    for ext_sock, _locked in ext_inputs:
        k = in_index[(ext_sock.node.name, ext_sock.identifier)]
        try:
            tree.links.new(ext_sock, gn.inputs[k])
        except (RuntimeError, IndexError):
            pass
    for locked_sock, ext_sock in ext_outputs:
        k = out_index[(locked_sock.node.name, locked_sock.identifier)]
        try:
            tree.links.new(gn.outputs[k], ext_sock)
        except (RuntimeError, IndexError):
            pass
    return grp, gn


def _placeholder_group_tree(tree, shared_group):
    """Drive a fully-locked group's outputs from a *single* instance of the
    shared 'LOCKED' placeholder group (reuse across every locked group). The
    instance node itself reads 'LOCKED' and is tinted black.

    Returns the names of the nodes added to *tree* so unlock can remove them."""
    out = next((n for n in tree.nodes if n.bl_idname == "NodeGroupOutput"), None)
    if out is None:
        return []
    # A socket still linked after the internals were removed is a direct
    # interface passthrough (GroupInput→GroupOutput) — leave it untouched.
    # The shared compositor placeholder exposes only Color; the geometry one only
    # Geometry; the shader one Color/Shader/Value. Drive only the outputs the
    # shared group actually provides.
    if tree.bl_idname == "CompositorNodeTree":
        pick = {"RGBA": "Color"}
    elif tree.bl_idname == "GeometryNodeTree":
        pick = {"GEOMETRY": "Geometry"}
    else:
        pick = {"SHADER": "Shader", "RGBA": "Color", "VALUE": "Value"}
    unlinked = [s for s in out.inputs if (not s.is_linked) and s.type in pick]
    if not unlinked:
        return []

    gn = tree.nodes.new(trees.group_node_idname(tree.bl_idname))
    gn.node_tree = shared_group
    gn[PLACEHOLDER_TAG] = True
    gn.label = "LOCKED"
    gn.use_custom_color = True
    gn.color = LOCKED_NODE_COLOR
    gn.location = (out.location.x - 260, out.location.y)
    for sock in unlinked:
        try:
            tree.links.new(gn.outputs[pick[sock.type]], sock)
        except (RuntimeError, KeyError):
            pass
    return [gn.name]


def _remove_placeholder_nodes(lock):
    try:
        data = json.loads(lock.get("placeholder_nodes", "{}"))
    except (KeyError, ValueError, TypeError):
        data = {}
    for tname, names in data.items():
        t = trees.resolve_tree(tname)
        if t is None:
            continue
        for nm in list(names):
            n = t.nodes.get(nm)
            if n is not None and n.get(PLACEHOLDER_TAG):
                t.nodes.remove(n)
    # drop the shared 'LOCKED' placeholder group once its last instance is gone
    # (other trees from the same lock may still reference it — guard on users)
    try:
        groups = json.loads(lock.get("placeholder_groups", "[]"))
    except (KeyError, ValueError, TypeError):
        groups = []
    for gname in groups:
        g = bpy.data.node_groups.get(gname)
        if g is not None and g.get(PLACEHOLDER_TAG) and g.users == 0:
            try:
                bpy.data.node_groups.remove(g)
            except (RuntimeError, ReferenceError):
                pass
    # purge baked label textures that only existed for the placeholder, once the
    # group that referenced them is gone
    try:
        imgs = json.loads(lock.get("placeholder_images", "[]"))
    except (KeyError, ValueError, TypeError):
        imgs = []
    for nm in imgs:
        img = bpy.data.images.get(nm)
        if img is not None and img.get(PLACEHOLDER_TAG) and img.users == 0:
            try:
                bpy.data.images.remove(img)
            except (RuntimeError, ReferenceError):
                pass


def _color_locked_instances(tree):
    """Tint every group node that instances *tree* gray. Returns descriptors
    of the previous colour state so unlock can revert it."""
    saved = []
    for _owner, ref, node in trees.iter_group_instances(tree):
        saved.append({
            "owner": ref,
            "node": node.name,
            "prev_ccolor": bool(node.use_custom_color),
            "prev_color": list(node.color),
        })
        node.use_custom_color = True
        node.color = LOCKED_NODE_COLOR
    return saved


def _uncolor_locked_instances(lock):
    try:
        data = json.loads(lock.get("colored_nodes", "[]"))
    except (KeyError, ValueError, TypeError):
        return
    for e in data:
        t = trees.resolve_owner_tree(e.get("owner", e.get("tree", "")))
        if t is None:
            continue
        n = t.nodes.get(e.get("node", ""))
        if n is None:
            continue
        n.use_custom_color = bool(e.get("prev_ccolor", False))
        col = e.get("prev_color")
        if isinstance(col, list) and len(col) == 3:
            n.color = col


def _protect_referenced_ids(nodes):
    """Fake-user every ID datablock (image, nested node group, texture …)
    referenced by *nodes*. Without this, a datablock whose only user is a
    locked-away node is orphaned and purged on save, so unlock could not
    restore the reference. Returns prior states so unlock can revert them."""
    saved = []
    seen = set()
    for n in nodes:
        for prop in n.bl_rna.properties:
            if prop.type != "POINTER":
                continue
            val = getattr(n, prop.identifier, None)
            if isinstance(val, bpy.types.ID) and id(val) not in seen:
                seen.add(id(val))
                saved.append({"ref": serialize._id_ref(val), "prev_fake": bool(val.use_fake_user)})
                val.use_fake_user = True
    return saved


def _restore_protected_ids(lock):
    try:
        data = json.loads(lock.get("protected_ids", "[]"))
    except (KeyError, ValueError, TypeError):
        return
    rep = serialize.RebuildReport()
    for e in data:
        val = serialize._resolve_id_ref(e.get("ref", {}), rep)
        if val is not None and not e.get("prev_fake", True):
            val.use_fake_user = False


def _locked_targets(tree):
    """Every locked tree reachable from *tree* (itself first, then nested
    groups), de-duplicated — so a parent group can unlock its children in
    one pass."""
    out = []
    if state.is_locked(tree):
        out.append(tree)
    for sub in trees.walk_descendant_trees(tree):
        if state.is_locked(sub) and sub not in out:
            out.append(sub)
    return out


def _sidecar_path(tree) -> str:
    blend = bpy.data.filepath
    stem = os.path.splitext(os.path.basename(blend))[0]
    safe = re.sub(r"[^\w.-]+", "_", tree.name)
    return os.path.join(os.path.dirname(blend), f"{stem}_{safe}.locknodes_backup.json")


class LOCKNODES_OT_lock_hard(bpy.types.Operator):
    bl_idname = "locknodes.lock_hard"
    bl_label = "Lock Nodes"
    bl_description = (
        "Serialize the pro nodes, encrypt them into the group and delete "
        "them from the graph. Without the password they cannot be recovered."
    )
    bl_options = {"REGISTER", "UNDO"}

    scope: bpy.props.EnumProperty(
        name="What to lock",
        items=(
            ("SELECTED", "Selected Nodes", "Lock the nodes you selected (a selected group locks its whole contents)"),
            ("MUTED", "All Muted Nodes", "Lock every muted node in the group"),
        ),
        default="SELECTED",
    )
    write_backup: bpy.props.BoolProperty(
        name="Write plaintext backup (.json)",
        description="Save an unencrypted backup of the locked nodes next to the "
        ".blend so you can recover them if you lose the password — never ship it",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return _poll_group(context, locked=False)

    def invoke(self, context, event):
        self.write_backup = prefs.get_prefs().backup_on_hard_lock if prefs.master_password_set() else True
        # Snapshot the selection NOW — the props-dialog execute() can run in a
        # popup context where the edit tree / node.select are no longer reachable.
        space = context.space_data
        et = getattr(space, "edit_tree", None) if space is not None else None
        self._cap_tree = et
        self._cap_names = [n.name for n in et.nodes if n.select] if et is not None else None
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "scope")
        layout.prop(self, "write_backup")
        if self.scope == "SELECTED":
            et, sel = self._resolve_selection(context)
            grps = 0
            if et is not None:
                for n in et.nodes:
                    picked = (n.name in sel) if sel is not None else n.select
                    if picked and trees.is_group_node(n) and n.node_tree is not None and not state.is_locked(n.node_tree):
                        grps += 1
            if grps:
                gnoun = "group" if grps == 1 else "groups"
                layout.label(text=f"{grps} selected {gnoun} locked in full", icon="NODETREE")
        count = len(self._target_nodes(context))
        if count:
            noun = "node" if count == 1 else "nodes"
            layout.label(text=f"{count} {noun} will be encrypted and removed", icon="INFO")
        else:
            warn = layout.row()
            warn.alert = True
            hint = "select nodes to lock" if self.scope == "SELECTED" else "mute the nodes to lock"
            warn.label(text=f"Nothing to lock yet — {hint}", icon="ERROR")
        # How the locked group will look afterwards — same fields as the prefs.
        try:
            pr = prefs.get_prefs()
            box = layout.box()
            box.label(text="Placeholder shown after locking", icon="NODE")
            box.prop(pr, "placeholder_color", text="Base color")
            box.prop(pr, "placeholder_label", text="Watermark text")
        except (KeyError, AttributeError):
            pass

    def _target_nodes(self, context):
        node = active_group_node(context)
        if node is None:
            return []
        if self.scope == "SELECTED":
            return self._selected_targets(context)
        tree = node.node_tree
        return [(tree, n) for n in tree.nodes if n.mute and n.bl_idname not in trees.INTERFACE_NODE_IDNAMES]

    def _resolve_selection(self, context):
        """(edit_tree, selected_names) preferring the invoke-time snapshot;
        falls back to the live context (e.g. when replayed via redo)."""
        cap = getattr(self, "_cap_tree", None)
        if cap is not None:
            try:
                _ = cap.name  # liveness check on the datablock
                return cap, set(getattr(self, "_cap_names", None) or [])
            except (ReferenceError, AttributeError):
                pass
        space = context.space_data
        et = getattr(space, "edit_tree", None) if space is not None else None
        return et, None

    def _selected_targets(self, context):
        """Collect lock targets from the *selection in the edit tree*.

        Every selected group node locks its entire internal tree — so
        selecting several group nodes locks them all at once. A selected
        loose node is locked in place. Duplicate group instances (several
        nodes pointing at the same tree) collapse to one tree.
        """
        edit_tree, sel_names = self._resolve_selection(context)
        if edit_tree is None:
            return []
        targets = []
        seen_trees = set()
        for n in edit_tree.nodes:
            selected = (n.name in sel_names) if sel_names is not None else n.select
            if not selected or n.bl_idname in trees.INTERFACE_NODE_IDNAMES:
                continue
            if trees.is_group_node(n) and n.node_tree is not None:
                sub = n.node_tree
                if id(sub) in seen_trees or state.is_locked(sub):
                    continue
                seen_trees.add(id(sub))
                targets.extend(
                    (sub, sn) for sn in sub.nodes
                    if sn.bl_idname not in trees.INTERFACE_NODE_IDNAMES
                )
            else:
                targets.append((edit_tree, n))
        return targets

    def execute(self, context):
        node = active_group_node(context)
        if node is None:
            return {"CANCELLED"}
        if not prefs.master_password_set():
            self.report({"ERROR"}, "Set a master password in the LockNodes add-on preferences first")
            return {"CANCELLED"}
        password = prefs.get_master_password()
        if self.write_backup and not bpy.data.is_saved:
            self.report({"ERROR"}, "Save the .blend first — the backup sidecar needs a directory")
            return {"CANCELLED"}

        targets = self._target_nodes(context)
        if not targets:
            hint = {
                "SELECTED": "select the node group(s) you want to lock",
                "MUTED": "mute the nodes you want to lock first",
            }[self.scope]
            self.report({"ERROR"}, f"Nothing to lock — {hint}")
            return {"CANCELLED"}

        # For MUTED mode: expand muted group nodes into their internal content.
        # A muted group wrapper bypasses its own contents, so the LOCKED
        # placeholder we put inside it would never render — track those wrappers
        # so we can unmute them after locking (and re-mute on unlock).
        muted_wrappers = {}  # parent tree name -> [wrapper node names]
        if self.scope == "MUTED":
            expanded = []
            for t, n in targets:
                if trees.is_group_node(n) and n.node_tree is not None:
                    sub = n.node_tree
                    sub_n = [(sub, sn) for sn in sub.nodes if sn.bl_idname not in trees.INTERFACE_NODE_IDNAMES]
                    if sub_n:
                        expanded.extend(sub_n)
                        muted_wrappers.setdefault(t.name, []).append(n.name)
                else:
                    expanded.append((t, n))
            targets = expanded
            if not targets:
                self.report({"ERROR"}, "Nothing to lock — group expands to empty (no internal nodes)")
                return {"CANCELLED"}

        affected = {t for t, _ in targets}
        for t in affected:
            if not _lockable(self, t):
                return {"CANCELLED"}
        target_set = set(n for _, n in targets)
        passthrough = 0
        for _, n in targets:
            has_in = any(
                s.is_linked and any(l.from_node not in target_set for l in s.links)
                for s in n.inputs
            )
            has_out = any(
                s.is_linked and any(l.to_node not in target_set for l in s.links)
                for s in n.outputs
            )
            if has_in and has_out:
                passthrough += 1
        if passthrough:
            self.report(
                {"WARNING"},
                f"{passthrough} locked node(s) sit in an active chain — they become an "
                "empty LOCKED group, so the downstream result will change",
            )

        payload = serialize.serialize_multi_subgraph(targets)
        payload_json = json.dumps(payload)

        if self.write_backup:
            path = _sidecar_path(node.node_tree)
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=1)
            except OSError as exc:
                self.report({"ERROR"}, f"Backup failed, lock aborted: {exc}")
                return {"CANCELLED"}

        data = _base_lock_data(password)
        data["mode"] = state.MODE_HARD
        data["blob"] = crypto.encrypt_blob(payload_json, password, data["kdf_iters"])
        data["removed_count"] = len(targets)

        protected = _protect_referenced_ids([n for _, n in targets])

        placeholders = {}
        baked_images = []
        placeholder_group_names = []
        shared_group = None
        colored = []
        for t in affected:
            tree_ns = [n for tt, n in targets if tt is t]
            content = [n for n in t.nodes if n.bl_idname not in trees.INTERFACE_NODE_IDNAMES]
            fully_locked = len(tree_ns) == len(content)
            if fully_locked:
                # whole group: strip internals, drop in one instance of the
                # shared 'LOCKED' placeholder group (built once, reused) and
                # tint the group's instances black.
                for n in list(tree_ns):
                    t.nodes.remove(n)
                if shared_group is None:
                    shared_group = _build_locked_group(baked_images, t.bl_idname)
                ph = _placeholder_group_tree(t, shared_group)
                if ph:
                    placeholders.setdefault(t.name, []).extend(ph)
                colored.extend(_color_locked_instances(t))
            else:
                # partial lock inside a live graph: collapse the locked nodes
                # into one empty 'LOCKED' group whose interface preserves every
                # boundary link, so the surrounding graph stays wired (no more
                # bypass-by-index that dropped operands on multi-input nodes).
                g, gnode = _group_locked_subset(t, tree_ns)
                if g is not None:
                    placeholder_group_names.append(g.name)
                    placeholders.setdefault(t.name, []).append(gnode.name)
                for n in list(tree_ns):
                    t.nodes.remove(n)

        if shared_group is not None and shared_group.users:
            placeholder_group_names.append(shared_group.name)
        elif shared_group is not None:
            # nothing instanced it (e.g. every output was a passthrough) — drop
            # the orphan group, then its now-unused baked image
            try:
                bpy.data.node_groups.remove(shared_group)
            except (RuntimeError, ReferenceError):
                pass
            shared_group = None
            for nm in list(baked_images):
                im = bpy.data.images.get(nm)
                if im is not None and im.get(PLACEHOLDER_TAG) and im.users == 0:
                    try:
                        bpy.data.images.remove(im)
                    except (RuntimeError, ReferenceError):
                        pass
            baked_images = []

        # Unmute the muted group wrappers so their LOCKED placeholder shows
        # instead of being bypassed. Record them so unlock re-mutes (symmetry).
        remuted = {}
        for tname, names in muted_wrappers.items():
            pt = trees.resolve_tree(tname)
            if pt is None:
                continue
            for nm in names:
                wn = pt.nodes.get(nm)
                if wn is not None and wn.mute:
                    wn.mute = False
                    remuted.setdefault(tname, []).append(nm)
        if remuted:
            data["remuted_wrappers"] = json.dumps(remuted)

        muted = {}
        for t in affected:
            muted[t.name] = json.dumps([n.name for n in t.nodes if n.mute])
        data["muted_nodes"] = json.dumps(muted)
        if placeholders:
            data["placeholder_nodes"] = json.dumps(placeholders)
        if placeholder_group_names:
            data["placeholder_groups"] = json.dumps(placeholder_group_names)
        if baked_images:
            data["placeholder_images"] = json.dumps(baked_images)
        if colored:
            data["colored_nodes"] = json.dumps(colored)
        if protected:
            data["protected_ids"] = json.dumps(protected)

        for t in affected:
            # keep the group alive while locked; remember the prior flag so
            # unlock can revert it (unlock reverses everything the lock changed)
            data["prev_fake_user"] = bool(t.use_fake_user)
            state.stamp_lock(t, data)
            t.use_fake_user = True
            state.pending_finalize.add(t.name)

        self.report(
            {"INFO"},
            f"Hard-locked {len(affected)} tree(s) — {len(targets)} node(s) removed and encrypted",
        )
        try:
            bpy.ops.locknodes.finalize_hard_lock("INVOKE_DEFAULT")
        except RuntimeError:
            # No UI for the confirm dialog (background/scripted) — the
            # panel keeps showing the finalize warning instead.
            pass
        return {"FINISHED"}


class LOCKNODES_OT_finalize_hard_lock(bpy.types.Operator):
    bl_idname = "locknodes.finalize_hard_lock"
    bl_label = "Finalize Lock (Save & Reload)"
    bl_description = (
        "Until the file is saved and reloaded, the removed nodes can be "
        "restored with Undo. Saving and reloading clears the undo history"
    )
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        return bool(state.pending_finalize) and bpy.data.is_saved

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        bpy.ops.wm.save_mainfile()
        state.pending_finalize.clear()
        bpy.ops.wm.revert_mainfile()
        return {"FINISHED"}


def _unmute_multi(payload, lock):
    try:
        muted_raw = json.loads(lock.get("muted_nodes", "{}"))
    except (KeyError, ValueError, TypeError):
        muted_raw = {}
    for sub in payload.get("subtrees", []):
        sn = sub.get("tree_name", "")
        st = bpy.data.node_groups.get(sn)
        if st is None:
            continue
        names = {e["name"] for e in sub.get("nodes", [])}
        if isinstance(muted_raw, dict):
            names |= set(json.loads(muted_raw.get(sn, "[]")))
        for nm in names:
            n = st.nodes.get(nm)
            if n is not None:
                n.mute = False
                n.hide = False


def _remute_wrappers(lock):
    """Re-mute the group wrappers the lock unmuted (so the placeholder could
    show). Reverses the lock-time unmute; skipped when the user reactivates."""
    try:
        remuted = json.loads(lock.get("remuted_wrappers", "{}"))
    except (KeyError, ValueError, TypeError):
        return
    for tname, names in remuted.items():
        t = trees.resolve_tree(tname)
        if t is None:
            continue
        for nm in names:
            n = t.nodes.get(nm)
            if n is not None:
                n.mute = True


def _unmute_single(tree, payload, lock):
    try:
        muted = set(json.loads(lock.get("muted_nodes", "[]")))
    except (KeyError, ValueError, TypeError):
        muted = set()
    names = {e["name"] for e in payload.get("nodes", [])} | muted
    for nm in names:
        n = tree.nodes.get(nm)
        if n is not None:
            n.mute = False
            n.hide = False


def unlock_tree(tree, password, unmute, report):
    """Decrypt and rebuild one locked tree in place, remove its 'locked'
    placeholder and revert the gray tint on its instances.

    `report(level, msg)` receives status strings. Returns (ok, restored)."""
    lock = state.get_lock(tree)
    if lock is None:
        return False, 0
    if tree.library is not None or not tree.is_editable:
        report("ERROR", f"'{tree.name}': linked groups must be unlocked in their source file")
        return False, 0
    iters = int(lock.get("kdf_iters", crypto.DEFAULT_ITERS))
    if not crypto.verify_password(
        password, str(lock.get("verify_salt", "")), str(lock.get("verify_hash", "")), iters
    ):
        report("ERROR", f"'{tree.name}': wrong password")
        return False, 0
    blob = lock.get("blob")
    if not blob:
        report("ERROR", f"'{tree.name}': lock data missing its encrypted payload")
        return False, 0
    try:
        payload_json = crypto.decrypt_blob(str(blob), password)
    except (crypto.TamperError, crypto.BlobFormatError):
        report("ERROR", f"'{tree.name}': lock data corrupted or tampered")
        return False, 0
    payload = json.loads(payload_json)

    if "subtrees" in payload:
        _remove_bypass_links(lock)
        reports = serialize.rebuild_multi_subgraph(payload)
        restored = sum(r.restored for r in reports)
        for r in reports:
            for w in r.warnings:
                report("WARNING", w)
        if unmute:
            _unmute_multi(payload, lock)
        else:
            _remute_wrappers(lock)
        _remove_placeholder_nodes(lock)
        _uncolor_locked_instances(lock)
        _restore_protected_ids(lock)
        for sub in payload.get("subtrees", []):
            st = bpy.data.node_groups.get(sub.get("tree_name", ""))
            if st is not None:
                sub_lock = state.get_lock(st)
                if sub_lock is not None:
                    st.use_fake_user = bool(sub_lock.get("prev_fake_user", False))
                state.clear_lock(st)
    else:
        _remove_bypass_links(lock)
        rep = serialize.rebuild_subgraph(tree, payload)
        for w in rep.warnings:
            report("WARNING", w)
        restored = rep.restored
        if unmute:
            _unmute_single(tree, payload, lock)
        else:
            _remute_wrappers(lock)
        _remove_placeholder_nodes(lock)
        _uncolor_locked_instances(lock)
        _restore_protected_ids(lock)
        tree.use_fake_user = bool(lock.get("prev_fake_user", False))
        state.clear_lock(tree)
    return True, restored


class LOCKNODES_OT_unlock(bpy.types.Operator):
    bl_idname = "locknodes.unlock"
    bl_label = "Unlock"
    bl_description = "Unlock the active node group with its password"
    bl_options = {"REGISTER", "UNDO"}

    password: bpy.props.StringProperty(
        name="Password", subtype="PASSWORD", options={"SKIP_SAVE"}
    )
    unmute: bpy.props.BoolProperty(
        name="Reactivate Locked Nodes",
        description="Unmute and unhide the protected nodes after unlocking so they are active and visible again",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return _poll_group(context, locked=True)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "password")
        layout.prop(self, "unmute")

    def execute(self, context):
        node = active_group_node(context)
        if node is None:
            return {"CANCELLED"}
        tree = node.node_tree
        ok, restored = unlock_tree(
            tree, self.password, self.unmute, lambda lvl, msg: self.report({lvl}, msg)
        )
        if not ok:
            return {"CANCELLED"}
        self.report({"INFO"}, f"Unlocked '{tree.name}' — {restored} node(s) restored")
        return {"FINISHED"}


class LOCKNODES_OT_unlock_all(bpy.types.Operator):
    bl_idname = "locknodes.unlock_all"
    bl_label = "Unlock All Sub-groups"
    bl_description = (
        "Unlock the active group and every locked node group nested inside it "
        "in one pass, with a single password"
    )
    bl_options = {"REGISTER", "UNDO"}

    password: bpy.props.StringProperty(
        name="Password", subtype="PASSWORD", options={"SKIP_SAVE"}
    )
    unmute: bpy.props.BoolProperty(
        name="Reactivate Locked Nodes",
        description="Unmute and unhide the protected nodes after unlocking",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        node = active_group_node(context)
        return node is not None and bool(_locked_targets(node.node_tree))

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        node = active_group_node(context)
        if node is not None:
            count = len(_locked_targets(node.node_tree))
            layout.label(text=f"{count} locked group(s) will be unlocked", icon="UNLOCKED")
        layout.prop(self, "password")
        layout.prop(self, "unmute")

    def execute(self, context):
        node = active_group_node(context)
        if node is None:
            return {"CANCELLED"}
        targets = _locked_targets(node.node_tree)
        if not targets:
            return {"CANCELLED"}
        done = total = 0
        failed = []
        for t in targets:
            ok, restored = unlock_tree(
                t, self.password, self.unmute, lambda lvl, msg: self.report({lvl}, msg)
            )
            if ok:
                done += 1
                total += restored
            else:
                failed.append(t.name)
        if done == 0:
            self.report({"ERROR"}, "Nothing unlocked — check the password")
            return {"CANCELLED"}
        msg = f"Unlocked {done} group(s) — {total} node(s) restored"
        if failed:
            msg += f"; {len(failed)} failed (wrong password?)"
        self.report({"INFO"}, msg)
        return {"FINISHED"}


classes = (
    LOCKNODES_OT_lock_hard,
    LOCKNODES_OT_finalize_hard_lock,
    LOCKNODES_OT_unlock,
    LOCKNODES_OT_unlock_all,
)
