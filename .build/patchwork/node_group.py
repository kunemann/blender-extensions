"""Patchwork — Wrapper-group architecture, multi-pattern variant (V2).

Architecture
------------
Each material that has Patchwork applied contains exactly **one** wrapper Group
instance (named "Patchwork"). Its NodeTree (`Patchwork_Wrap_<MatName>`) is
unique per material and contains everything the addon adds:

  - one shared `Patchwork_TilingFix` transform instance per distinct upstream
    UV source (computes UV A, UV B, Weight using the selected Pattern algorithm)
  - per wrapped texture: the original Image-Texture-Node (moved in), a duplicate
    Image-Texture-Node, and Mix nodes for every externally-used output socket
  - parameter pass-through (Pattern, Cell Scale, Random Rotation, …) from the
    wrapper interface to the inner transform instance(s)

The `Patchwork_TilingFix` NodeTree itself is global, shared across all wrappers.

V2 adds a `Pattern` float input at the very top of the wrapper. The transform
group computes ALL four patterns in parallel and uses Mix nodes (driven by the
Pattern value) to select which (UV A, UV B, Weight, Debug) reaches the outputs.
Patterns:
  0 = Voronoi  (default polygonal cells, identical to V1)
  1 = Blocks   (axis-aligned rectangular grid cells, Chebyshev metric)
  2 = Rings    (concentric ring regions from the UV origin)
  3 = Noise    (organic regions driven by a low-frequency Noise field)

External view
-------------
In the user's material main tree, the wrapper appears as a single Group node with:
  - input sockets: Pattern, UV, Bypass, parameter sliders (Cell Scale, …,
    Edge Softness)
  - output sockets: `<TexName> Color`, `<TexName> Alpha` etc. (one per used
    output socket of each wrapped texture)
"""

import math

import bpy

# Shared transform group
TRANSFORM_GROUP_NAME = "Patchwork_TilingFix"

# Internal topology version of Patchwork_TilingFix. Bumped whenever the node
# graph inside the transform group changes in a way existing .blend files need
# to migrate. ensure_transform_group() rebuilds the inner graph in-place when
# it sees an older version on an existing group.
#  v3 -> v4: Multi-pattern (Voronoi/Blocks/Rings/Noise) + Pattern input socket.
#  v4 -> v5: Added Gabor (2D, intensity) pattern; reordered indices to
#            Voronoi=0, Noise=1, Gabor=2, Blocks=3, Rings=4.
TRANSFORM_VERSION = 5
TRANSFORM_VERSION_MARK = "patchwork_transform_version"

# Wrapper group naming
WRAPPER_NAME_PREFIX = "Patchwork_Wrap_"

# Custom-property markers
WRAPPER_NODE_MARK = "patchwork_wrapper"        # bool on Group-Instance in main tree
WRAPPER_TREE_MARK = "patchwork_kind"           # "wrapper" on the wrapper NodeTree
TRANSFORM_INST_MARK = "patchwork_transform"    # bool on the transform-group-instance inside a wrapper
ORIG_INSIDE_MARK = "patchwork_orig_of"         # str: original name on the orig-inside-wrapper TexImage
DUP_INSIDE_MARK = "patchwork_dup_of"           # str: orig name on the duplicate TexImage
MIX_INSIDE_MARK = "patchwork_mix_of"           # str: orig name on the Mix node
WRAPPER_UV_BIND = "patchwork_uv_socket"        # str on the transform-instance: name of the wrapper UV input it consumes

# ShaderNodeMix socket indices per data_type
_MIX_SOCKETS = {
    "FLOAT":  (0, 2, 3, 0),
    "VECTOR": (0, 4, 5, 1),
    "RGBA":   (0, 6, 7, 2),
}

# ---------------------------------------------------------------------------
# Pattern selector
# ---------------------------------------------------------------------------
# The Pattern parameter is intentionally NOT exposed as a wrapper input socket.
# Each wrapper carries a hidden ShaderNodeValue node (marked with
# WRAPPER_PATTERN_VALUE_MARK) whose default_value drives the Pattern input of
# every transform instance in that wrapper. The user changes Pattern only via
# the N-Panel enum dropdown (bpy.types.Material.patchwork_pattern), which
# writes that hidden value.
PATTERN_INPUT_NAME = "Pattern"
PATTERN_NAMES = ["Voronoi", "Noise", "Gabor", "Blocks", "Rings"]
PATTERN_DESCRIPTIONS = [
    "Polygonal Voronoi cells with random rotation/offset per cell (default)",
    "Organic regions driven by a low-frequency Noise field",
    "Banded regions driven by Gabor Texture (2D, intensity output)",
    "Axis-aligned rectangular grid cells (Chebyshev metric)",
    "Concentric ring regions around the UV origin",
]

WRAPPER_PATTERN_VALUE_MARK = "patchwork_pattern_value"  # bool on hidden Value node inside the wrapper

# Pattern is still an input on the SHARED transform group (so the math graph is
# parameterised), but the wrapper consumes it via its internal Value node, not
# as an exposed socket. The spec below is used only inside the transform group.
PATTERN_PARAM_SPEC = (
    PATTERN_INPUT_NAME,
    0.0,
    0.0,
    float(len(PATTERN_NAMES) - 1),
    "NONE",
    (
        "Texture-Logik / pattern type (integer, rounded). "
        "0 = Voronoi, 1 = Noise, 2 = Gabor, 3 = Blocks, 4 = Rings."
    ),
)


# Parameter specs that get forwarded into the inner transform group
TRANSFORM_PARAM_SPECS = [
    ("Cell Scale", 0.01, 0.001, 64.0, "NONE",
     "Cells per input UV unit. ~0.01 keeps a single large cell whose border is broken up organically by Noise Strength."),
    ("Random Rotation", 0.5, 0.0, 1.0, "FACTOR",
     "Per-cell random rotation amount (0 = none, 1 = full 360°)."),
    ("Random Offset", 0.5, 0.0, 1.0, "FACTOR",
     "Per-cell random offset amount."),
    ("Noise Strength", 0.5, 0.0, 2.0, "NONE",
     "Amount the cell BORDERS are broken up by noise. 0 = clean geometric edges, max = strongly fragmented borders. Does not distort the overall cell shapes or positions."),
    ("Noise Detail", 5.0, 0.0, 15.0, "NONE",
     "Fineness (frequency) of the border break-up noise. Higher = smaller / finer fragments at the cell boundaries; lower = larger, blobbier perturbations."),
    ("Border Width", 0.5, 0.0, 1.0, "FACTOR",
     "Width of the transition zone between cells in the final render. 0 = hard cell edges, 1 = wide soft borders. Also visible as the white zone in the Debug output."),
    ("Edge Softness", 0.5, 0.0, 1.0, "FACTOR",
     "Extra soft blending added on the outside of the cell boundary (on top of Border Width). Controls only the smoothness of the transition, not the noise."),
]
TRANSFORM_PARAM_NAMES = [spec[0] for spec in TRANSFORM_PARAM_SPECS]

# Wrapper-only params (not forwarded into the transform group)
WRAPPER_ONLY_PARAM_SPECS = [
    ("Bypass", 0.0, 0.0, 1.0, "FACTOR",
     "1 = bypass the Patchwork effect entirely (raw texture, identical to before Apply)."),
]
WRAPPER_PARAM_SPECS = TRANSFORM_PARAM_SPECS + WRAPPER_ONLY_PARAM_SPECS
WRAPPER_PARAM_NAMES = [spec[0] for spec in WRAPPER_PARAM_SPECS]

# Backwards-compat shim used by the transform builder
PARAM_SPECS = TRANSFORM_PARAM_SPECS
PARAM_NAMES = TRANSFORM_PARAM_NAMES

BYPASS_OF_MARK = "patchwork_bypass_of"          # str on Mix-Vector node: name of the transform instance it gates
BYPASS_SOCKET_MARK = "patchwork_bypass_socket"  # str on Mix-Vector node: "UV A" or "UV B"

# Debug-viz markers (inside the shared transform group)
DEBUG_VIZ_MIX_MARK = "patchwork_debug_viz"      # bool on the Mix RGBA node that produces the Debug output
DEBUG_F1_VORONOI_MARK = "patchwork_debug_vor1"  # bool on the F1 Voronoi node (used for migration lookup)
DEBUG_EDGE_PROX_MARK = "patchwork_debug_edge"   # bool on the edge-proximity MapRange node
WEIGHT_MAP_RANGE_MARK = "patchwork_weight"      # bool on the weight MapRange (final A/B blend factor)
BORDER_WIDTH_DRIVER_MARK = "patchwork_border_w" # bool on the MULTIPLY_ADD that drives edge_proximity.From Min


# --------------------------------------------------------------------------- #
#                            Helpers                                          #
# --------------------------------------------------------------------------- #


def _add_parameter_inputs(iface, specs=None):
    if specs is None:
        specs = TRANSFORM_PARAM_SPECS
    for name, default, mn, mx, subtype, desc in specs:
        s = iface.new_socket(name=name, in_out="INPUT", socket_type="NodeSocketFloat")
        s.default_value = default
        s.min_value = mn
        s.max_value = mx
        s.subtype = subtype
        s.description = desc


def _add_pattern_input(iface, parent=None):
    name, default, mn, mx, subtype, desc = PATTERN_PARAM_SPEC
    kwargs = {"name": name, "in_out": "INPUT", "socket_type": "NodeSocketFloat"}
    if parent is not None:
        kwargs["parent"] = parent
    s = iface.new_socket(**kwargs)
    s.default_value = default
    s.min_value = mn
    s.max_value = mx
    s.subtype = subtype
    s.description = desc
    return s


def _unique_group_name(base):
    if base not in bpy.data.node_groups:
        return base
    i = 1
    while f"{base}.{i:03d}" in bpy.data.node_groups:
        i += 1
    return f"{base}.{i:03d}"


def _mix_data_type_for_socket(sock):
    bl = sock.bl_idname
    if bl == "NodeSocketColor":
        return "RGBA"
    if bl == "NodeSocketVector":
        return "VECTOR"
    return "FLOAT"


# Nodes whose input socket names are meaningful pass names (Base Color, Normal, Roughness, …)
_TERMINAL_NODE_PREFIXES = ("ShaderNodeBsdf",)
_TERMINAL_NODE_EXACT = {
    "ShaderNodeOutputMaterial",
    "ShaderNodeOutputLight",
    "ShaderNodeOutputWorld",
    "ShaderNodeEmission",
    "ShaderNodeBackground",
    "ShaderNodeSubsurfaceScattering",
    "ShaderNodeAmbientOcclusion",
}


def _infer_pass_name_from_socket(out_sock, max_depth=4):
    """Walk downstream from `out_sock` up to `max_depth` hops, following through
    intermediate nodes (Normal Map, Bump, Mix, color modifiers, etc.) until a
    BSDF/Output input is reached. Returns that input's socket name (e.g.
    'Base Color', 'Roughness', 'Normal') so it can be used as the wrapper
    output name. Falls back to `out_sock.name` if no terminal is found.
    """
    if not out_sock.is_linked:
        return out_sock.name

    visited_ids = set()
    current = out_sock

    for _ in range(max_depth):
        if not current.is_linked:
            break
        link = current.links[0]
        if id(link) in visited_ids:
            break
        visited_ids.add(id(link))

        to_node = link.to_node
        to_sock = link.to_socket
        idname = to_node.bl_idname

        if idname in _TERMINAL_NODE_EXACT or any(idname.startswith(p) for p in _TERMINAL_NODE_PREFIXES):
            return to_sock.name

        next_out = None
        for o in to_node.outputs:
            if o.is_linked:
                next_out = o
                break
        if next_out is None:
            return to_sock.name
        current = next_out

    return out_sock.name


def _socket_type_for_output(bl_idname):
    """Map a Blender socket bl_idname to a value usable in interface.new_socket()."""
    if bl_idname in ("NodeSocketColor", "NodeSocketFloat", "NodeSocketVector"):
        return bl_idname
    return "NodeSocketColor"


def _copy_texture_props(src, dst):
    dst.image = src.image
    dst.extension = src.extension
    dst.interpolation = src.interpolation
    dst.projection = src.projection
    dst.projection_blend = src.projection_blend


def _find_node_by_idname(tree, idname):
    return next((n for n in tree.nodes if n.bl_idname == idname), None)


# --------------------------------------------------------------------------- #
#                  Shared transform NodeTree (multi-pattern)                  #
# --------------------------------------------------------------------------- #


def ensure_transform_group():
    nt = bpy.data.node_groups.get(TRANSFORM_GROUP_NAME)
    if nt is None or nt.bl_idname != "ShaderNodeTree":
        return _build_transform_group()
    _ensure_transform_interface(nt)
    if int(nt.get(TRANSFORM_VERSION_MARK, 0)) < TRANSFORM_VERSION:
        _populate_transform_group_nodes(nt)
    return nt


def _transform_group_has_input(nt, name):
    for it in nt.interface.items_tree:
        if (getattr(it, "item_type", None) == "SOCKET"
                and it.in_out == "INPUT"
                and it.name == name):
            return True
    return False


def _transform_group_has_output(nt, name):
    for it in nt.interface.items_tree:
        if (getattr(it, "item_type", None) == "SOCKET"
                and it.in_out == "OUTPUT"
                and it.name == name):
            return True
    return False


def _ensure_transform_interface(nt):
    """Ensure the transform group's interface has all the sockets v4 expects.
    Idempotent — only adds what's missing. The internal node graph is rebuilt
    separately by _populate_transform_group_nodes when the version changes."""
    iface = nt.interface

    if not _transform_group_has_input(nt, "UV"):
        iface.new_socket(name="UV", in_out="INPUT", socket_type="NodeSocketVector")

    if not _transform_group_has_input(nt, PATTERN_INPUT_NAME):
        _add_pattern_input(iface)

    for spec in TRANSFORM_PARAM_SPECS:
        name = spec[0]
        if not _transform_group_has_input(nt, name):
            _, default, mn, mx, subtype, desc = spec
            s = iface.new_socket(name=name, in_out="INPUT", socket_type="NodeSocketFloat")
            s.default_value = default
            s.min_value = mn
            s.max_value = mx
            s.subtype = subtype
            s.description = desc

    for out_name, sock_type, desc in [
        ("UV A", "NodeSocketVector", "Primary UV sample position"),
        ("UV B", "NodeSocketVector", "Secondary UV sample position (blended in near cell borders)"),
        ("Weight", "NodeSocketFloat", "Blend factor between sample A and sample B (0 = A, 1 = B)"),
        ("Debug", "NodeSocketColor",
         "Debug visualisation: per-cell pattern with white-highlighted edge zones."),
    ]:
        if not _transform_group_has_output(nt, out_name):
            s = iface.new_socket(name=out_name, in_out="OUTPUT", socket_type=sock_type)
            s.description = desc


def _build_transform_group():
    nt = bpy.data.node_groups.new(TRANSFORM_GROUP_NAME, "ShaderNodeTree")
    nt.color_tag = "VECTOR"
    nt.description = (
        "Multi-pattern anti-tiling transform: outputs UV A, UV B and Weight "
        "for weighted blending of two sample points of the same texture. "
        "Pattern input selects Voronoi / Blocks / Rings / Noise."
    )

    iface = nt.interface
    iface.new_socket(name="UV", in_out="INPUT", socket_type="NodeSocketVector")
    _add_pattern_input(iface)
    _add_parameter_inputs(iface)
    iface.new_socket(name="UV A", in_out="OUTPUT", socket_type="NodeSocketVector")
    iface.new_socket(name="UV B", in_out="OUTPUT", socket_type="NodeSocketVector")
    iface.new_socket(name="Weight", in_out="OUTPUT", socket_type="NodeSocketFloat")
    debug_socket = iface.new_socket(
        name="Debug", in_out="OUTPUT", socket_type="NodeSocketColor",
    )
    debug_socket.description = (
        "Debug visualisation: per-cell pattern with white-highlighted edge zones."
    )

    _populate_transform_group_nodes(nt)
    return nt


# --------------------------------------------------------------------------- #
#  Pattern generators                                                         #
#                                                                             #
#  Each generator consumes the scaled UV (uv * Cell Scale) and produces:      #
#    color_a  — RGB driving rotation/offset of sample A (R=rotation, GB=off)  #
#    color_b  — same for sample B (the neighbour cell/region)                 #
#    ratio    — symmetric distance-to-border metric, 0 at center, 0.5 at edge #
#    debug    — RGB used as the per-pattern visualisation in the Debug output #
# --------------------------------------------------------------------------- #


def _build_pattern_voronoi(nt, scaled_uv_socket, x_base, y_base):
    nodes = nt.nodes
    links = nt.links

    vor1 = nodes.new("ShaderNodeTexVoronoi")
    vor1.voronoi_dimensions = "2D"
    vor1.feature = "F1"
    vor1.distance = "EUCLIDEAN"
    vor1.label = "Voronoi F1"
    vor1.location = (x_base, y_base + 100)
    vor1[DEBUG_F1_VORONOI_MARK] = True
    links.new(scaled_uv_socket, vor1.inputs["Vector"])

    vor2 = nodes.new("ShaderNodeTexVoronoi")
    vor2.voronoi_dimensions = "2D"
    vor2.feature = "F2"
    vor2.distance = "EUCLIDEAN"
    vor2.label = "Voronoi F2"
    vor2.location = (x_base, y_base - 150)
    links.new(scaled_uv_socket, vor2.inputs["Vector"])

    sum_d = nodes.new("ShaderNodeMath")
    sum_d.operation = "ADD"
    sum_d.label = "F1.d + F2.d"
    sum_d.location = (x_base + 240, y_base - 20)
    links.new(vor1.outputs["Distance"], sum_d.inputs[0])
    links.new(vor2.outputs["Distance"], sum_d.inputs[1])

    div_d = nodes.new("ShaderNodeMath")
    div_d.operation = "DIVIDE"
    div_d.label = "F1.d / (F1.d + F2.d)"
    div_d.location = (x_base + 440, y_base - 20)
    links.new(vor1.outputs["Distance"], div_d.inputs[0])
    links.new(sum_d.outputs["Value"], div_d.inputs[1])

    return (
        vor1.outputs["Color"],
        vor2.outputs["Color"],
        div_d.outputs["Value"],
        vor1.outputs["Color"],
    )


def _build_pattern_blocks(nt, scaled_uv_socket, x_base, y_base):
    """Two-tap rectangular-grid pattern.

    cell_a_id = round(scaled_uv)  (the nearest grid cell)
    pos       = scaled_uv - cell_a_id        (per-component ∈ [-0.5, +0.5])
    cell_b_id = cell_a_id + neighbour direction along the DOMINANT axis
    dist_a, dist_b — Chebyshev distance (per-cell max-abs)
    ratio = dist_a / (dist_a + dist_b)  → 0 at cell center, 0.5 at boundary,
            symmetric across the boundary (just like Voronoi).
    """
    nodes = nt.nodes
    links = nt.links

    half_combine = nodes.new("ShaderNodeCombineXYZ")
    half_combine.label = "Half (0.5,0.5)"
    half_combine.inputs["X"].default_value = 0.5
    half_combine.inputs["Y"].default_value = 0.5
    half_combine.inputs["Z"].default_value = 0.0
    half_combine.location = (x_base - 250, y_base + 240)

    shifted = nodes.new("ShaderNodeVectorMath")
    shifted.operation = "ADD"
    shifted.label = "uv + 0.5"
    shifted.location = (x_base, y_base + 240)
    links.new(scaled_uv_socket, shifted.inputs[0])
    links.new(half_combine.outputs["Vector"], shifted.inputs[1])

    cell_a_id = nodes.new("ShaderNodeVectorMath")
    cell_a_id.operation = "FLOOR"
    cell_a_id.label = "Cell A id"
    cell_a_id.location = (x_base + 220, y_base + 240)
    links.new(shifted.outputs["Vector"], cell_a_id.inputs[0])

    pos = nodes.new("ShaderNodeVectorMath")
    pos.operation = "SUBTRACT"
    pos.label = "pos (uv - cell_a)"
    pos.location = (x_base + 440, y_base + 80)
    links.new(scaled_uv_socket, pos.inputs[0])
    links.new(cell_a_id.outputs["Vector"], pos.inputs[1])

    abs_pos = nodes.new("ShaderNodeVectorMath")
    abs_pos.operation = "ABSOLUTE"
    abs_pos.location = (x_base + 640, y_base + 80)
    links.new(pos.outputs["Vector"], abs_pos.inputs[0])

    sep_abs = nodes.new("ShaderNodeSeparateXYZ")
    sep_abs.location = (x_base + 840, y_base + 80)
    links.new(abs_pos.outputs["Vector"], sep_abs.inputs[0])

    sep_pos = nodes.new("ShaderNodeSeparateXYZ")
    sep_pos.label = "Sign helper"
    sep_pos.location = (x_base + 840, y_base - 120)
    links.new(pos.outputs["Vector"], sep_pos.inputs[0])

    dist_a = nodes.new("ShaderNodeMath")
    dist_a.operation = "MAXIMUM"
    dist_a.label = "dist_a (Chebyshev)"
    dist_a.location = (x_base + 1040, y_base + 80)
    links.new(sep_abs.outputs["X"], dist_a.inputs[0])
    links.new(sep_abs.outputs["Y"], dist_a.inputs[1])

    x_dom = nodes.new("ShaderNodeMath")
    x_dom.operation = "GREATER_THAN"
    x_dom.label = "x_dom"
    x_dom.location = (x_base + 1040, y_base - 60)
    links.new(sep_abs.outputs["X"], x_dom.inputs[0])
    links.new(sep_abs.outputs["Y"], x_dom.inputs[1])

    y_dom = nodes.new("ShaderNodeMath")
    y_dom.operation = "SUBTRACT"
    y_dom.label = "y_dom = 1 - x_dom"
    y_dom.inputs[0].default_value = 1.0
    y_dom.location = (x_base + 1040, y_base - 220)
    links.new(x_dom.outputs["Value"], y_dom.inputs[1])

    sign_x = nodes.new("ShaderNodeMath")
    sign_x.operation = "SIGN"
    sign_x.location = (x_base + 1040, y_base - 380)
    links.new(sep_pos.outputs["X"], sign_x.inputs[0])

    sign_y = nodes.new("ShaderNodeMath")
    sign_y.operation = "SIGN"
    sign_y.location = (x_base + 1040, y_base - 540)
    links.new(sep_pos.outputs["Y"], sign_y.inputs[0])

    n_dx = nodes.new("ShaderNodeMath")
    n_dx.operation = "MULTIPLY"
    n_dx.label = "neighbour dx"
    n_dx.location = (x_base + 1240, y_base - 200)
    links.new(x_dom.outputs["Value"], n_dx.inputs[0])
    links.new(sign_x.outputs["Value"], n_dx.inputs[1])

    n_dy = nodes.new("ShaderNodeMath")
    n_dy.operation = "MULTIPLY"
    n_dy.label = "neighbour dy"
    n_dy.location = (x_base + 1240, y_base - 480)
    links.new(y_dom.outputs["Value"], n_dy.inputs[0])
    links.new(sign_y.outputs["Value"], n_dy.inputs[1])

    n_offset = nodes.new("ShaderNodeCombineXYZ")
    n_offset.label = "neighbour offset"
    n_offset.location = (x_base + 1440, y_base - 340)
    links.new(n_dx.outputs["Value"], n_offset.inputs["X"])
    links.new(n_dy.outputs["Value"], n_offset.inputs["Y"])

    cell_b_id = nodes.new("ShaderNodeVectorMath")
    cell_b_id.operation = "ADD"
    cell_b_id.label = "Cell B id"
    cell_b_id.location = (x_base + 1440, y_base + 240)
    links.new(cell_a_id.outputs["Vector"], cell_b_id.inputs[0])
    links.new(n_offset.outputs["Vector"], cell_b_id.inputs[1])

    pos_b = nodes.new("ShaderNodeVectorMath")
    pos_b.operation = "SUBTRACT"
    pos_b.label = "pos_b"
    pos_b.location = (x_base + 1640, y_base + 80)
    links.new(pos.outputs["Vector"], pos_b.inputs[0])
    links.new(n_offset.outputs["Vector"], pos_b.inputs[1])

    abs_pos_b = nodes.new("ShaderNodeVectorMath")
    abs_pos_b.operation = "ABSOLUTE"
    abs_pos_b.location = (x_base + 1840, y_base + 80)
    links.new(pos_b.outputs["Vector"], abs_pos_b.inputs[0])

    sep_abs_b = nodes.new("ShaderNodeSeparateXYZ")
    sep_abs_b.location = (x_base + 2040, y_base + 80)
    links.new(abs_pos_b.outputs["Vector"], sep_abs_b.inputs[0])

    dist_b = nodes.new("ShaderNodeMath")
    dist_b.operation = "MAXIMUM"
    dist_b.label = "dist_b"
    dist_b.location = (x_base + 2240, y_base + 80)
    links.new(sep_abs_b.outputs["X"], dist_b.inputs[0])
    links.new(sep_abs_b.outputs["Y"], dist_b.inputs[1])

    sum_d = nodes.new("ShaderNodeMath")
    sum_d.operation = "ADD"
    sum_d.label = "dist_a + dist_b"
    sum_d.location = (x_base + 2440, y_base - 20)
    links.new(dist_a.outputs["Value"], sum_d.inputs[0])
    links.new(dist_b.outputs["Value"], sum_d.inputs[1])

    ratio = nodes.new("ShaderNodeMath")
    ratio.operation = "DIVIDE"
    ratio.label = "blocks ratio"
    ratio.location = (x_base + 2640, y_base - 20)
    links.new(dist_a.outputs["Value"], ratio.inputs[0])
    links.new(sum_d.outputs["Value"], ratio.inputs[1])

    color_a = nodes.new("ShaderNodeTexWhiteNoise")
    color_a.noise_dimensions = "3D"
    color_a.label = "cell_a color"
    color_a.location = (x_base + 1840, y_base + 360)
    links.new(cell_a_id.outputs["Vector"], color_a.inputs["Vector"])

    color_b = nodes.new("ShaderNodeTexWhiteNoise")
    color_b.noise_dimensions = "3D"
    color_b.label = "cell_b color"
    color_b.location = (x_base + 1840, y_base + 540)
    links.new(cell_b_id.outputs["Vector"], color_b.inputs["Vector"])

    return (
        color_a.outputs["Color"],
        color_b.outputs["Color"],
        ratio.outputs["Value"],
        color_a.outputs["Color"],
    )


def _build_pattern_rings(nt, scaled_uv_socket, x_base, y_base):
    """Concentric-ring pattern.

    radius     = length(scaled_uv)
    ring_a_id  = round(radius)
    pos_r      = radius - ring_a_id          (∈ [-0.5, 0.5])
    ring_b_id  = ring_a_id + sign(pos_r)
    ratio = |pos_r| / (|pos_r| + |radius - ring_b_id|)
    """
    nodes = nt.nodes
    links = nt.links

    length = nodes.new("ShaderNodeVectorMath")
    length.operation = "LENGTH"
    length.label = "radius"
    length.location = (x_base, y_base)
    links.new(scaled_uv_socket, length.inputs[0])

    half_add = nodes.new("ShaderNodeMath")
    half_add.operation = "ADD"
    half_add.label = "radius + 0.5"
    half_add.inputs[1].default_value = 0.5
    half_add.location = (x_base + 220, y_base)
    links.new(length.outputs["Value"], half_add.inputs[0])

    ring_a_id = nodes.new("ShaderNodeMath")
    ring_a_id.operation = "FLOOR"
    ring_a_id.label = "Ring A id"
    ring_a_id.location = (x_base + 420, y_base)
    links.new(half_add.outputs["Value"], ring_a_id.inputs[0])

    pos_r = nodes.new("ShaderNodeMath")
    pos_r.operation = "SUBTRACT"
    pos_r.label = "pos_r"
    pos_r.location = (x_base + 620, y_base - 120)
    links.new(length.outputs["Value"], pos_r.inputs[0])
    links.new(ring_a_id.outputs["Value"], pos_r.inputs[1])

    dist_a = nodes.new("ShaderNodeMath")
    dist_a.operation = "ABSOLUTE"
    dist_a.label = "dist_a"
    dist_a.location = (x_base + 820, y_base - 120)
    links.new(pos_r.outputs["Value"], dist_a.inputs[0])

    sign_r = nodes.new("ShaderNodeMath")
    sign_r.operation = "SIGN"
    sign_r.location = (x_base + 820, y_base - 320)
    links.new(pos_r.outputs["Value"], sign_r.inputs[0])

    ring_b_id = nodes.new("ShaderNodeMath")
    ring_b_id.operation = "ADD"
    ring_b_id.label = "Ring B id"
    ring_b_id.location = (x_base + 1020, y_base - 320)
    links.new(ring_a_id.outputs["Value"], ring_b_id.inputs[0])
    links.new(sign_r.outputs["Value"], ring_b_id.inputs[1])

    pos_b = nodes.new("ShaderNodeMath")
    pos_b.operation = "SUBTRACT"
    pos_b.label = "pos_b"
    pos_b.location = (x_base + 1220, y_base - 320)
    links.new(length.outputs["Value"], pos_b.inputs[0])
    links.new(ring_b_id.outputs["Value"], pos_b.inputs[1])

    dist_b = nodes.new("ShaderNodeMath")
    dist_b.operation = "ABSOLUTE"
    dist_b.label = "dist_b"
    dist_b.location = (x_base + 1420, y_base - 320)
    links.new(pos_b.outputs["Value"], dist_b.inputs[0])

    sum_d = nodes.new("ShaderNodeMath")
    sum_d.operation = "ADD"
    sum_d.label = "dist_a + dist_b"
    sum_d.location = (x_base + 1620, y_base - 220)
    links.new(dist_a.outputs["Value"], sum_d.inputs[0])
    links.new(dist_b.outputs["Value"], sum_d.inputs[1])

    ratio = nodes.new("ShaderNodeMath")
    ratio.operation = "DIVIDE"
    ratio.label = "rings ratio"
    ratio.location = (x_base + 1820, y_base - 220)
    links.new(dist_a.outputs["Value"], ratio.inputs[0])
    links.new(sum_d.outputs["Value"], ratio.inputs[1])

    vec_a = nodes.new("ShaderNodeCombineXYZ")
    vec_a.label = "Ring A vec"
    vec_a.location = (x_base + 820, y_base + 200)
    links.new(ring_a_id.outputs["Value"], vec_a.inputs["X"])

    vec_b = nodes.new("ShaderNodeCombineXYZ")
    vec_b.label = "Ring B vec"
    vec_b.location = (x_base + 820, y_base + 380)
    links.new(ring_b_id.outputs["Value"], vec_b.inputs["X"])

    color_a = nodes.new("ShaderNodeTexWhiteNoise")
    color_a.noise_dimensions = "3D"
    color_a.label = "ring_a color"
    color_a.location = (x_base + 1020, y_base + 200)
    links.new(vec_a.outputs["Vector"], color_a.inputs["Vector"])

    color_b = nodes.new("ShaderNodeTexWhiteNoise")
    color_b.noise_dimensions = "3D"
    color_b.label = "ring_b color"
    color_b.location = (x_base + 1020, y_base + 380)
    links.new(vec_b.outputs["Vector"], color_b.inputs["Vector"])

    return (
        color_a.outputs["Color"],
        color_b.outputs["Color"],
        ratio.outputs["Value"],
        color_a.outputs["Color"],
    )


def _build_pattern_noise(nt, scaled_uv_socket, x_base, y_base):
    """Organic-region pattern driven by a smooth noise field.

    Same skeleton as Rings, but the discrete axis is sampled from a low-freq
    Noise field (Detail 2, Roughness 0.5) instead of the radial distance.
    """
    nodes = nt.nodes
    links = nt.links

    # N_REGIONS = how many discrete "levels" the noise gets quantised into.
    # Higher → smaller blobs at a given Cell Scale.
    N_REGIONS = 12.0

    noise_tex = nodes.new("ShaderNodeTexNoise")
    noise_tex.noise_dimensions = "2D"
    noise_tex.inputs["Scale"].default_value = 1.0
    noise_tex.inputs["Detail"].default_value = 2.0
    noise_tex.inputs["Roughness"].default_value = 0.5
    noise_tex.label = "Region noise"
    noise_tex.location = (x_base, y_base)
    links.new(scaled_uv_socket, noise_tex.inputs["Vector"])

    discretise = nodes.new("ShaderNodeMath")
    discretise.operation = "MULTIPLY"
    discretise.label = "fac * N_REGIONS"
    discretise.inputs[1].default_value = N_REGIONS
    discretise.location = (x_base + 240, y_base)
    links.new(noise_tex.outputs["Fac"], discretise.inputs[0])

    half_add = nodes.new("ShaderNodeMath")
    half_add.operation = "ADD"
    half_add.label = "axis + 0.5"
    half_add.inputs[1].default_value = 0.5
    half_add.location = (x_base + 440, y_base)
    links.new(discretise.outputs["Value"], half_add.inputs[0])

    region_a_id = nodes.new("ShaderNodeMath")
    region_a_id.operation = "FLOOR"
    region_a_id.label = "Region A id"
    region_a_id.location = (x_base + 640, y_base)
    links.new(half_add.outputs["Value"], region_a_id.inputs[0])

    pos_n = nodes.new("ShaderNodeMath")
    pos_n.operation = "SUBTRACT"
    pos_n.label = "pos_n"
    pos_n.location = (x_base + 840, y_base - 120)
    links.new(discretise.outputs["Value"], pos_n.inputs[0])
    links.new(region_a_id.outputs["Value"], pos_n.inputs[1])

    dist_a = nodes.new("ShaderNodeMath")
    dist_a.operation = "ABSOLUTE"
    dist_a.label = "dist_a"
    dist_a.location = (x_base + 1040, y_base - 120)
    links.new(pos_n.outputs["Value"], dist_a.inputs[0])

    sign_n = nodes.new("ShaderNodeMath")
    sign_n.operation = "SIGN"
    sign_n.location = (x_base + 1040, y_base - 320)
    links.new(pos_n.outputs["Value"], sign_n.inputs[0])

    region_b_id = nodes.new("ShaderNodeMath")
    region_b_id.operation = "ADD"
    region_b_id.label = "Region B id"
    region_b_id.location = (x_base + 1240, y_base - 320)
    links.new(region_a_id.outputs["Value"], region_b_id.inputs[0])
    links.new(sign_n.outputs["Value"], region_b_id.inputs[1])

    pos_b = nodes.new("ShaderNodeMath")
    pos_b.operation = "SUBTRACT"
    pos_b.label = "pos_b"
    pos_b.location = (x_base + 1440, y_base - 320)
    links.new(discretise.outputs["Value"], pos_b.inputs[0])
    links.new(region_b_id.outputs["Value"], pos_b.inputs[1])

    dist_b = nodes.new("ShaderNodeMath")
    dist_b.operation = "ABSOLUTE"
    dist_b.label = "dist_b"
    dist_b.location = (x_base + 1640, y_base - 320)
    links.new(pos_b.outputs["Value"], dist_b.inputs[0])

    sum_d = nodes.new("ShaderNodeMath")
    sum_d.operation = "ADD"
    sum_d.location = (x_base + 1840, y_base - 220)
    links.new(dist_a.outputs["Value"], sum_d.inputs[0])
    links.new(dist_b.outputs["Value"], sum_d.inputs[1])

    ratio = nodes.new("ShaderNodeMath")
    ratio.operation = "DIVIDE"
    ratio.label = "noise ratio"
    ratio.location = (x_base + 2040, y_base - 220)
    links.new(dist_a.outputs["Value"], ratio.inputs[0])
    links.new(sum_d.outputs["Value"], ratio.inputs[1])

    vec_a = nodes.new("ShaderNodeCombineXYZ")
    vec_a.label = "Region A vec"
    vec_a.location = (x_base + 1040, y_base + 200)
    links.new(region_a_id.outputs["Value"], vec_a.inputs["X"])

    vec_b = nodes.new("ShaderNodeCombineXYZ")
    vec_b.label = "Region B vec"
    vec_b.location = (x_base + 1040, y_base + 380)
    links.new(region_b_id.outputs["Value"], vec_b.inputs["X"])

    color_a = nodes.new("ShaderNodeTexWhiteNoise")
    color_a.noise_dimensions = "3D"
    color_a.label = "region_a color"
    color_a.location = (x_base + 1240, y_base + 200)
    links.new(vec_a.outputs["Vector"], color_a.inputs["Vector"])

    color_b = nodes.new("ShaderNodeTexWhiteNoise")
    color_b.noise_dimensions = "3D"
    color_b.label = "region_b color"
    color_b.location = (x_base + 1240, y_base + 380)
    links.new(vec_b.outputs["Vector"], color_b.inputs["Vector"])

    return (
        color_a.outputs["Color"],
        color_b.outputs["Color"],
        ratio.outputs["Value"],
        color_a.outputs["Color"],
    )


def _build_pattern_gabor(nt, scaled_uv_socket, x_base, y_base):
    """Banded organic regions driven by the Gabor texture's Intensity output.

    Same skeleton as Noise/Rings, but the discrete axis is sampled from the
    Gabor texture (2D, Intensity output). Defaults are chosen so the bands
    have visible structure at Cell Scale ≈ 1.
    """
    nodes = nt.nodes
    links = nt.links

    N_REGIONS = 10.0

    gabor = nodes.new("ShaderNodeTexGabor")
    # The Gabor node was added in recent Blender (4.3+). Setting the type is
    # best-effort across versions; if the attribute is missing the default
    # ("2D") still applies.
    try:
        gabor.gabor_type = "2D"
    except (AttributeError, TypeError):
        pass
    gabor.label = "Gabor (2D, Intensity)"
    gabor.location = (x_base, y_base)
    links.new(scaled_uv_socket, gabor.inputs["Vector"])

    intensity_out = gabor.outputs.get("Intensity")
    if intensity_out is None:
        # Fall back to first output if Intensity is not exposed in this Blender
        # version. The math below still produces a sensible banded pattern.
        intensity_out = gabor.outputs[0]

    discretise = nodes.new("ShaderNodeMath")
    discretise.operation = "MULTIPLY"
    discretise.label = "intensity * N_REGIONS"
    discretise.inputs[1].default_value = N_REGIONS
    discretise.location = (x_base + 240, y_base)
    links.new(intensity_out, discretise.inputs[0])

    half_add = nodes.new("ShaderNodeMath")
    half_add.operation = "ADD"
    half_add.label = "axis + 0.5"
    half_add.inputs[1].default_value = 0.5
    half_add.location = (x_base + 440, y_base)
    links.new(discretise.outputs["Value"], half_add.inputs[0])

    region_a_id = nodes.new("ShaderNodeMath")
    region_a_id.operation = "FLOOR"
    region_a_id.label = "Gabor Region A id"
    region_a_id.location = (x_base + 640, y_base)
    links.new(half_add.outputs["Value"], region_a_id.inputs[0])

    pos_n = nodes.new("ShaderNodeMath")
    pos_n.operation = "SUBTRACT"
    pos_n.label = "pos_n"
    pos_n.location = (x_base + 840, y_base - 120)
    links.new(discretise.outputs["Value"], pos_n.inputs[0])
    links.new(region_a_id.outputs["Value"], pos_n.inputs[1])

    dist_a = nodes.new("ShaderNodeMath")
    dist_a.operation = "ABSOLUTE"
    dist_a.label = "dist_a"
    dist_a.location = (x_base + 1040, y_base - 120)
    links.new(pos_n.outputs["Value"], dist_a.inputs[0])

    sign_n = nodes.new("ShaderNodeMath")
    sign_n.operation = "SIGN"
    sign_n.location = (x_base + 1040, y_base - 320)
    links.new(pos_n.outputs["Value"], sign_n.inputs[0])

    region_b_id = nodes.new("ShaderNodeMath")
    region_b_id.operation = "ADD"
    region_b_id.label = "Gabor Region B id"
    region_b_id.location = (x_base + 1240, y_base - 320)
    links.new(region_a_id.outputs["Value"], region_b_id.inputs[0])
    links.new(sign_n.outputs["Value"], region_b_id.inputs[1])

    pos_b = nodes.new("ShaderNodeMath")
    pos_b.operation = "SUBTRACT"
    pos_b.label = "pos_b"
    pos_b.location = (x_base + 1440, y_base - 320)
    links.new(discretise.outputs["Value"], pos_b.inputs[0])
    links.new(region_b_id.outputs["Value"], pos_b.inputs[1])

    dist_b = nodes.new("ShaderNodeMath")
    dist_b.operation = "ABSOLUTE"
    dist_b.label = "dist_b"
    dist_b.location = (x_base + 1640, y_base - 320)
    links.new(pos_b.outputs["Value"], dist_b.inputs[0])

    sum_d = nodes.new("ShaderNodeMath")
    sum_d.operation = "ADD"
    sum_d.location = (x_base + 1840, y_base - 220)
    links.new(dist_a.outputs["Value"], sum_d.inputs[0])
    links.new(dist_b.outputs["Value"], sum_d.inputs[1])

    ratio = nodes.new("ShaderNodeMath")
    ratio.operation = "DIVIDE"
    ratio.label = "gabor ratio"
    ratio.location = (x_base + 2040, y_base - 220)
    links.new(dist_a.outputs["Value"], ratio.inputs[0])
    links.new(sum_d.outputs["Value"], ratio.inputs[1])

    vec_a = nodes.new("ShaderNodeCombineXYZ")
    vec_a.label = "Gabor Region A vec"
    vec_a.location = (x_base + 1040, y_base + 200)
    links.new(region_a_id.outputs["Value"], vec_a.inputs["X"])

    vec_b = nodes.new("ShaderNodeCombineXYZ")
    vec_b.label = "Gabor Region B vec"
    vec_b.location = (x_base + 1040, y_base + 380)
    links.new(region_b_id.outputs["Value"], vec_b.inputs["X"])

    color_a = nodes.new("ShaderNodeTexWhiteNoise")
    color_a.noise_dimensions = "3D"
    color_a.label = "gabor_a color"
    color_a.location = (x_base + 1240, y_base + 200)
    links.new(vec_a.outputs["Vector"], color_a.inputs["Vector"])

    color_b = nodes.new("ShaderNodeTexWhiteNoise")
    color_b.noise_dimensions = "3D"
    color_b.label = "gabor_b color"
    color_b.location = (x_base + 1240, y_base + 380)
    links.new(vec_b.outputs["Vector"], color_b.inputs["Vector"])

    return (
        color_a.outputs["Color"],
        color_b.outputs["Color"],
        ratio.outputs["Value"],
        color_a.outputs["Color"],
    )


def _chain_select_mix(nt, data_type, pattern_value_socket,
                      voronoi_out, noise_out, gabor_out, blocks_out, rings_out,
                      x_base, y_base, label_prefix):
    """Chained Mix selectors driven by COMPARE(pattern_round, k) indicators.

    Voronoi (0) is the implicit default — falls through when none of the other
    indicators is 1. The chain order is: voronoi → noise → gabor → blocks → rings.
    """
    nodes = nt.nodes
    links = nt.links

    f_idx, a_idx, b_idx, r_idx = _MIX_SOCKETS[data_type]

    targets = [(1, noise_out,  "→ noise"),
               (2, gabor_out,  "→ gabor"),
               (3, blocks_out, "→ blocks"),
               (4, rings_out,  "→ rings")]
    current = voronoi_out
    for i, (target_value, b_out, suffix) in enumerate(targets):
        cmp = nodes.new("ShaderNodeMath")
        cmp.operation = "COMPARE"
        cmp.label = f"{label_prefix} sel {target_value}"
        cmp.inputs[1].default_value = float(target_value)
        cmp.inputs[2].default_value = 0.5
        cmp.location = (x_base + i * 280, y_base + 200)
        links.new(pattern_value_socket, cmp.inputs[0])

        m = nodes.new("ShaderNodeMix")
        m.data_type = data_type
        m.label = f"{label_prefix} {suffix}"
        m.location = (x_base + i * 280, y_base)
        links.new(cmp.outputs["Value"], m.inputs[f_idx])
        links.new(current, m.inputs[a_idx])
        links.new(b_out, m.inputs[b_idx])
        current = m.outputs[r_idx]

    return current


def _build_transform_chain(nt, gin, cell_color_socket, x_base, y_base):
    """Per-cell rotation + offset chain. Returns the final UV vector socket."""
    nodes = nt.nodes
    links = nt.links

    sep = nodes.new("ShaderNodeSeparateColor")
    sep.location = (x_base, y_base + 60)
    links.new(cell_color_socket, sep.inputs["Color"])

    rot_2pi = nodes.new("ShaderNodeMath")
    rot_2pi.operation = "MULTIPLY"
    rot_2pi.inputs[1].default_value = 2.0 * math.pi
    rot_2pi.location = (x_base + 220, y_base + 110)
    links.new(sep.outputs["Red"], rot_2pi.inputs[0])

    rot_gate = nodes.new("ShaderNodeMath")
    rot_gate.operation = "MULTIPLY"
    rot_gate.location = (x_base + 440, y_base + 110)
    links.new(rot_2pi.outputs["Value"], rot_gate.inputs[0])
    links.new(gin.outputs["Random Rotation"], rot_gate.inputs[1])

    rotate = nodes.new("ShaderNodeVectorRotate")
    rotate.rotation_type = "Z_AXIS"
    rotate.location = (x_base + 700, y_base + 60)
    links.new(gin.outputs["UV"], rotate.inputs["Vector"])
    links.new(rot_gate.outputs["Value"], rotate.inputs["Angle"])

    off_combine = nodes.new("ShaderNodeCombineXYZ")
    off_combine.location = (x_base + 220, y_base - 90)
    links.new(sep.outputs["Green"], off_combine.inputs["X"])
    links.new(sep.outputs["Blue"], off_combine.inputs["Y"])

    off_gate = nodes.new("ShaderNodeVectorMath")
    off_gate.operation = "SCALE"
    off_gate.location = (x_base + 440, y_base - 90)
    links.new(off_combine.outputs["Vector"], off_gate.inputs[0])
    links.new(gin.outputs["Random Offset"], off_gate.inputs[3])

    add = nodes.new("ShaderNodeVectorMath")
    add.operation = "ADD"
    add.location = (x_base + 940, y_base)
    links.new(rotate.outputs["Vector"], add.inputs[0])
    links.new(off_gate.outputs["Vector"], add.inputs[1])

    return add.outputs["Vector"]


def _build_debug_viz_nodes(nt, pattern_debug_socket, edge_proximity, gout):
    """Create the Mix RGBA node that combines the selected pattern's color with
    white (edges, faded in by edge_proximity), and wire it to gout.Debug.
    Idempotent — re-uses an existing tagged Mix if present."""
    mix = next(
        (n for n in nt.nodes
         if n.bl_idname == "ShaderNodeMix" and n.get(DEBUG_VIZ_MIX_MARK)),
        None,
    )
    if mix is None:
        mix = nt.nodes.new("ShaderNodeMix")
        mix.data_type = "RGBA"
        mix.blend_type = "MIX"
        mix.label = "Debug: Pattern + Edges"
        mix[DEBUG_VIZ_MIX_MARK] = True
        mix.location = (2400, -1100)
        # RGBA Mix socket layout: Factor=0, Result=2, A=6, B=7
        mix.inputs[7].default_value = (1.0, 1.0, 1.0, 1.0)

    f_idx, a_idx, b_idx, r_idx = _MIX_SOCKETS["RGBA"]
    def _link_if_missing(from_sock, to_sock):
        for lk in to_sock.links:
            if lk.from_socket == from_sock:
                return
        nt.links.new(from_sock, to_sock)

    _link_if_missing(edge_proximity.outputs["Result"], mix.inputs[f_idx])
    _link_if_missing(pattern_debug_socket, mix.inputs[a_idx])
    debug_in = gout.inputs.get("Debug")
    if debug_in is not None:
        _link_if_missing(mix.outputs[r_idx], debug_in)


def _populate_transform_group_nodes(nt):
    """Build the internal node graph of the transform group. Clears any
    pre-existing nodes first so this can be used both for fresh groups and for
    in-place migration of older groups (whose interface sockets are reused).

    Structure (v4):
      gin → scale_uv
            ├── Voronoi pattern  → (color_a, color_b, ratio, debug)
            ├── Blocks  pattern  → (color_a, color_b, ratio, debug)
            ├── Rings   pattern  → (color_a, color_b, ratio, debug)
            └── Noise   pattern  → (color_a, color_b, ratio, debug)
                  ↓ (Mix selectors driven by COMPARE(round(Pattern), k))
            sel_color_a, sel_color_b, sel_ratio, sel_debug_pattern
                  ↓
      edge-noise perturbation (Noise Strength / Noise Detail) on sel_ratio
                  ↓
      weight MapRange (Border Width / Edge Softness) → Weight output
      edge_proximity MapRange → debug visualisation gate
      transform chains for UV A (sel_color_a) and UV B (sel_color_b)
    """
    nt.nodes.clear()

    nodes = nt.nodes
    links = nt.links

    gin = nodes.new("NodeGroupInput")
    gin.location = (-2400, 0)
    gout = nodes.new("NodeGroupOutput")
    gout.location = (3000, 0)

    scale_uv = nodes.new("ShaderNodeVectorMath")
    scale_uv.operation = "SCALE"
    scale_uv.label = "uv * Cell Scale"
    scale_uv.location = (-2150, 250)
    links.new(gin.outputs["UV"], scale_uv.inputs[0])
    links.new(gin.outputs["Cell Scale"], scale_uv.inputs[3])
    scaled_uv = scale_uv.outputs["Vector"]

    # --- Pattern generators (parallel) -------------------------------------
    # Vertically stacked; selectors to the right consume them.
    vor_color_a, vor_color_b, vor_ratio, vor_debug = _build_pattern_voronoi(
        nt, scaled_uv, x_base=-1850, y_base=2400,
    )
    nse_color_a, nse_color_b, nse_ratio, nse_debug = _build_pattern_noise(
        nt, scaled_uv, x_base=-1850, y_base=1500,
    )
    gab_color_a, gab_color_b, gab_ratio, gab_debug = _build_pattern_gabor(
        nt, scaled_uv, x_base=-1850, y_base=400,
    )
    blk_color_a, blk_color_b, blk_ratio, blk_debug = _build_pattern_blocks(
        nt, scaled_uv, x_base=-1850, y_base=-700,
    )
    rng_color_a, rng_color_b, rng_ratio, rng_debug = _build_pattern_rings(
        nt, scaled_uv, x_base=-1850, y_base=-1900,
    )

    # --- Round the Pattern value, then chain-mix select -------------------
    p_round = nodes.new("ShaderNodeMath")
    p_round.operation = "ROUND"
    p_round.label = "round(Pattern)"
    p_round.location = (1000, -2200)
    links.new(gin.outputs[PATTERN_INPUT_NAME], p_round.inputs[0])

    sel_color_a = _chain_select_mix(
        nt, "RGBA", p_round.outputs["Value"],
        vor_color_a, nse_color_a, gab_color_a, blk_color_a, rng_color_a,
        x_base=1200, y_base=900, label_prefix="color_a",
    )
    sel_color_b = _chain_select_mix(
        nt, "RGBA", p_round.outputs["Value"],
        vor_color_b, nse_color_b, gab_color_b, blk_color_b, rng_color_b,
        x_base=1200, y_base=300, label_prefix="color_b",
    )
    sel_ratio = _chain_select_mix(
        nt, "FLOAT", p_round.outputs["Value"],
        vor_ratio, nse_ratio, gab_ratio, blk_ratio, rng_ratio,
        x_base=1200, y_base=-300, label_prefix="ratio",
    )
    sel_debug_pattern = _chain_select_mix(
        nt, "RGBA", p_round.outputs["Value"],
        vor_debug, nse_debug, gab_debug, blk_debug, rng_debug,
        x_base=1200, y_base=-900, label_prefix="debug",
    )

    # --- Edge-noise perturbation (acts on the selected ratio) -------------
    edge_noise = nodes.new("ShaderNodeTexNoise")
    edge_noise.noise_dimensions = "2D"
    edge_noise.inputs["Roughness"].default_value = 0.6
    edge_noise.inputs["Detail"].default_value = 5.0
    edge_noise.location = (2100, -300)
    links.new(gin.outputs["UV"], edge_noise.inputs["Vector"])

    nd_to_scale = nodes.new("ShaderNodeMath")
    nd_to_scale.operation = "MULTIPLY_ADD"
    nd_to_scale.label = "Noise Detail → Scale"
    nd_to_scale.inputs[1].default_value = 2.0
    nd_to_scale.inputs[2].default_value = 2.0
    nd_to_scale.location = (1900, -300)
    links.new(gin.outputs["Noise Detail"], nd_to_scale.inputs[0])
    links.new(nd_to_scale.outputs["Value"], edge_noise.inputs["Scale"])

    edge_centered = nodes.new("ShaderNodeMath")
    edge_centered.operation = "SUBTRACT"
    edge_centered.inputs[1].default_value = 0.5
    edge_centered.location = (2300, -300)
    links.new(edge_noise.outputs["Fac"], edge_centered.inputs[0])

    edge_amp = nodes.new("ShaderNodeMath")
    edge_amp.operation = "MULTIPLY"
    edge_amp.inputs[1].default_value = 1.5
    edge_amp.location = (2480, -300)
    links.new(edge_centered.outputs["Value"], edge_amp.inputs[0])

    edge_noise_fine = nodes.new("ShaderNodeTexNoise")
    edge_noise_fine.noise_dimensions = "2D"
    edge_noise_fine.inputs["Scale"].default_value = 60.0
    edge_noise_fine.inputs["Detail"].default_value = 2.0
    edge_noise_fine.inputs["Roughness"].default_value = 0.5
    edge_noise_fine.location = (2100, -560)
    links.new(gin.outputs["UV"], edge_noise_fine.inputs["Vector"])

    fine_centered = nodes.new("ShaderNodeMath")
    fine_centered.operation = "SUBTRACT"
    fine_centered.inputs[1].default_value = 0.5
    fine_centered.location = (2300, -560)
    links.new(edge_noise_fine.outputs["Fac"], fine_centered.inputs[0])

    fine_amp = nodes.new("ShaderNodeMath")
    fine_amp.operation = "MULTIPLY"
    fine_amp.inputs[1].default_value = 0.12
    fine_amp.location = (2480, -560)
    links.new(fine_centered.outputs["Value"], fine_amp.inputs[0])

    edge_combined = nodes.new("ShaderNodeMath")
    edge_combined.operation = "ADD"
    edge_combined.location = (2660, -430)
    links.new(edge_amp.outputs["Value"], edge_combined.inputs[0])
    links.new(fine_amp.outputs["Value"], edge_combined.inputs[1])

    edge_gate = nodes.new("ShaderNodeMath")
    edge_gate.operation = "MULTIPLY"
    edge_gate.location = (2840, -430)
    links.new(edge_combined.outputs["Value"], edge_gate.inputs[0])
    links.new(gin.outputs["Noise Strength"], edge_gate.inputs[1])

    noise_zone_mask = nodes.new("ShaderNodeMapRange")
    noise_zone_mask.interpolation_type = "SMOOTHSTEP"
    noise_zone_mask.clamp = True
    noise_zone_mask.inputs["From Min"].default_value = 0.25
    noise_zone_mask.inputs["From Max"].default_value = 0.5
    noise_zone_mask.inputs["To Min"].default_value = 0.0
    noise_zone_mask.inputs["To Max"].default_value = 1.0
    noise_zone_mask.location = (1900, -100)
    links.new(sel_ratio, noise_zone_mask.inputs["Value"])

    edge_gate_masked = nodes.new("ShaderNodeMath")
    edge_gate_masked.operation = "MULTIPLY"
    edge_gate_masked.location = (3020, -430)
    links.new(edge_gate.outputs["Value"], edge_gate_masked.inputs[0])
    links.new(noise_zone_mask.outputs["Result"], edge_gate_masked.inputs[1])

    ratio_pert = nodes.new("ShaderNodeMath")
    ratio_pert.operation = "ADD"
    ratio_pert.label = "ratio + edge noise"
    ratio_pert.location = (2100, -100)
    links.new(sel_ratio, ratio_pert.inputs[0])
    links.new(edge_gate_masked.outputs["Value"], ratio_pert.inputs[1])

    border_width_to_from_min = nodes.new("ShaderNodeMath")
    border_width_to_from_min.operation = "MULTIPLY_ADD"
    border_width_to_from_min.label = "Border Width → From Min"
    border_width_to_from_min[BORDER_WIDTH_DRIVER_MARK] = True
    border_width_to_from_min.inputs[1].default_value = -0.45
    border_width_to_from_min.inputs[2].default_value = 0.5
    border_width_to_from_min.location = (1900, -700)
    links.new(gin.outputs["Border Width"], border_width_to_from_min.inputs[0])

    blend_widen = nodes.new("ShaderNodeMath")
    blend_widen.operation = "MULTIPLY"
    blend_widen.inputs[1].default_value = 0.2
    blend_widen.location = (1900, 200)
    links.new(gin.outputs["Edge Softness"], blend_widen.inputs[0])

    bw_widen = nodes.new("ShaderNodeMath")
    bw_widen.operation = "MULTIPLY"
    bw_widen.inputs[1].default_value = 0.3
    bw_widen.label = "Border Width → From Max"
    bw_widen.location = (1900, 50)
    links.new(gin.outputs["Border Width"], bw_widen.inputs[0])

    blend_widen_sum = nodes.new("ShaderNodeMath")
    blend_widen_sum.operation = "ADD"
    blend_widen_sum.location = (2100, 200)
    links.new(blend_widen.outputs["Value"], blend_widen_sum.inputs[0])
    links.new(bw_widen.outputs["Value"], blend_widen_sum.inputs[1])

    blend_range = nodes.new("ShaderNodeMath")
    blend_range.operation = "ADD"
    blend_range.inputs[1].default_value = 0.52
    blend_range.location = (2300, 200)
    links.new(blend_widen_sum.outputs["Value"], blend_range.inputs[0])

    weight = nodes.new("ShaderNodeMapRange")
    weight.interpolation_type = "SMOOTHSTEP"
    weight.clamp = True
    weight.inputs["To Min"].default_value = 0.0
    weight.inputs["To Max"].default_value = 1.0
    weight.location = (2500, -100)
    weight[WEIGHT_MAP_RANGE_MARK] = True
    links.new(ratio_pert.outputs["Value"], weight.inputs["Value"])
    links.new(blend_range.outputs["Value"], weight.inputs["From Max"])
    links.new(border_width_to_from_min.outputs["Value"], weight.inputs["From Min"])

    edge_proximity = nodes.new("ShaderNodeMapRange")
    edge_proximity.interpolation_type = "SMOOTHSTEP"
    edge_proximity.clamp = True
    edge_proximity.inputs["To Min"].default_value = 0.0
    edge_proximity.inputs["To Max"].default_value = 1.0
    edge_proximity.location = (2500, -700)
    edge_proximity[DEBUG_EDGE_PROX_MARK] = True
    links.new(ratio_pert.outputs["Value"], edge_proximity.inputs["Value"])
    links.new(border_width_to_from_min.outputs["Value"], edge_proximity.inputs["From Min"])
    links.new(blend_range.outputs["Value"], edge_proximity.inputs["From Max"])

    # --- Transform chains -------------------------------------------------
    uv_a_out = _build_transform_chain(nt, gin, sel_color_a, x_base=1800, y_base=1200)
    uv_b_out = _build_transform_chain(nt, gin, sel_color_b, x_base=1800, y_base=600)

    links.new(uv_a_out, gout.inputs["UV A"])
    links.new(uv_b_out, gout.inputs["UV B"])
    links.new(weight.outputs["Result"], gout.inputs["Weight"])

    _build_debug_viz_nodes(nt, sel_debug_pattern, edge_proximity, gout)

    nt[TRANSFORM_VERSION_MARK] = TRANSFORM_VERSION


# --------------------------------------------------------------------------- #
#                       Per-material wrapper                                  #
# --------------------------------------------------------------------------- #


def find_wrapper_for_material(mat):
    if not mat or not mat.node_tree:
        return None
    for n in mat.node_tree.nodes:
        if n.bl_idname == "ShaderNodeGroup" and n.get(WRAPPER_NODE_MARK):
            return n
    return None


def _wrapper_display_name(mat):
    return f"{mat.name} – Patchwork"


def ensure_wrapper_for_material(mat):
    """Return (wrapper_instance_node, wrapper_node_tree). Create if missing.

    Interface ordering (top to bottom in the N-panel and on the group node):
      1. Bypass (root)
      2. Panel 'Anti-Tiling Settings' containing Cell Scale, Random Rotation,
         Random Offset, Noise Strength, Noise Detail, Border Width, Edge Softness
      3. UV inputs (added per texture as they are wrapped)
      4. Output sockets (Base Color, Normal, … added per texture)

    Pattern is NOT a wrapper interface input — it is held in a hidden
    ShaderNodeValue inside the wrapper (see _ensure_pattern_value_node).
    The N-Panel enum dropdown writes that value; the user never sees a Pattern
    slider on the wrapper itself.
    """
    desired_name = _wrapper_display_name(mat)

    existing = find_wrapper_for_material(mat)
    if existing is not None:
        existing.label = desired_name
        if existing.node_tree is not None and existing.node_tree.name != desired_name:
            target_name = _unique_group_name(desired_name) if desired_name not in bpy.data.node_groups else desired_name
            try:
                existing.node_tree.name = target_name
            except Exception:
                pass
        if existing.node_tree is not None:
            _migrate_wrapper_params(existing.node_tree)
        return existing, existing.node_tree

    main_tree = mat.node_tree

    wrapper_nt_name = _unique_group_name(desired_name)
    wrapper_nt = bpy.data.node_groups.new(wrapper_nt_name, "ShaderNodeTree")
    wrapper_nt[WRAPPER_TREE_MARK] = "wrapper"
    wrapper_nt.color_tag = "SHADER"
    wrapper_nt.description = f"Patchwork wrapper for material '{mat.name}'"

    iface = wrapper_nt.interface

    # 1. Bypass, free-standing root input
    s = iface.new_socket(name="Bypass", in_out="INPUT", socket_type="NodeSocketFloat")
    s.default_value = 0.0
    s.min_value = 0.0
    s.max_value = 1.0
    s.subtype = "FACTOR"
    s.description = (
        "1 = bypass the Patchwork effect entirely (raw texture, identical to before Apply)."
    )

    # 2. Collapsible panel grouping the algorithm parameters
    settings_panel = iface.new_panel(name="Anti-Tiling Settings", default_closed=False)
    for name, default, mn, mx, subtype, desc in TRANSFORM_PARAM_SPECS:
        ps = iface.new_socket(
            name=name, in_out="INPUT", socket_type="NodeSocketFloat", parent=settings_panel,
        )
        ps.default_value = default
        ps.min_value = mn
        ps.max_value = mx
        ps.subtype = subtype
        ps.description = desc

    gin = wrapper_nt.nodes.new("NodeGroupInput")
    gin.location = (-1500, 0)
    gout = wrapper_nt.nodes.new("NodeGroupOutput")
    gout.location = (1500, 0)

    # Hidden Value node feeding Pattern to every transform_inst inside.
    _ensure_pattern_value_node(wrapper_nt)

    inst = main_tree.nodes.new("ShaderNodeGroup")
    inst.node_tree = wrapper_nt
    inst.label = desired_name
    inst[WRAPPER_NODE_MARK] = True
    inst.location = (300, 200)

    return inst, wrapper_nt


def _ensure_pattern_value_node(wrapper_nt):
    """Find or create the hidden Value node that drives Pattern. Idempotent.
    The node lives inside the wrapper NodeTree and is wired to each
    transform_inst's Pattern input. The N-Panel enum dropdown writes its
    default_value."""
    for n in wrapper_nt.nodes:
        if n.bl_idname == "ShaderNodeValue" and n.get(WRAPPER_PATTERN_VALUE_MARK):
            return n
    val = wrapper_nt.nodes.new("ShaderNodeValue")
    val.label = "Patchwork Pattern (set via N-Panel)"
    val[WRAPPER_PATTERN_VALUE_MARK] = True
    val.outputs[0].default_value = 0.0
    val.location = (-1500, -400)
    val.hide = True
    return val


def _wire_pattern_value(wrapper_nt, transform_inst):
    """Connect the hidden Pattern value node to a transform_inst's Pattern input.
    Replaces any existing link to that input."""
    if PATTERN_INPUT_NAME not in transform_inst.inputs:
        return
    val_node = _ensure_pattern_value_node(wrapper_nt)
    tgt = transform_inst.inputs[PATTERN_INPUT_NAME]
    for lk in list(tgt.links):
        wrapper_nt.links.remove(lk)
    wrapper_nt.links.new(val_node.outputs[0], tgt)


def find_wrapper_pattern_value_node(wrapper_inst):
    """Return the hidden Value node that holds the Pattern for this wrapper,
    or None if not set up yet."""
    if wrapper_inst is None or wrapper_inst.node_tree is None:
        return None
    for n in wrapper_inst.node_tree.nodes:
        if n.bl_idname == "ShaderNodeValue" and n.get(WRAPPER_PATTERN_VALUE_MARK):
            return n
    return None


def _wrapper_uv_inputs(wrapper_nt):
    out = []
    for it in wrapper_nt.interface.items_tree:
        if getattr(it, "item_type", None) != "SOCKET":
            continue
        if it.in_out != "INPUT":
            continue
        if not it.name.startswith("UV"):
            continue
        if getattr(it, "socket_type", None) != "NodeSocketVector":
            continue
        out.append(it)
    return out


def _wrapper_uv_socket_name_for_main_source(wrapper_inst, main_source_socket):
    if main_source_socket is None:
        return None
    for inp in wrapper_inst.inputs:
        if inp.bl_idname != "NodeSocketVector":
            continue
        if not inp.name.startswith("UV"):
            continue
        if inp.is_linked and inp.links[0].from_socket == main_source_socket:
            return inp.name
    return None


def _add_wrapper_uv_input(wrapper_nt):
    iface = wrapper_nt.interface
    existing = len(_wrapper_uv_inputs(wrapper_nt))
    name = "UV" if existing == 0 else f"UV.{existing:03d}"
    iface.new_socket(name=name, in_out="INPUT", socket_type="NodeSocketVector")
    return name


def _find_bypass_mix(wrapper_nt, transform_inst_name, socket_label):
    for n in wrapper_nt.nodes:
        if (n.bl_idname == "ShaderNodeMix"
                and getattr(n, "data_type", None) == "VECTOR"
                and n.get(BYPASS_OF_MARK) == transform_inst_name
                and n.get(BYPASS_SOCKET_MARK) == socket_label):
            return n
    return None


def _ensure_transform_instance(wrapper_nt, wrapper_uv_socket_name):
    """Find or create a transform-group-instance and the bypass Mix Vector pair
    that gates its UV A / UV B outputs against the raw UV when Bypass == 1.
    Forwards Pattern + all TRANSFORM_PARAM_NAMES from the wrapper gin to the
    transform_inst.
    """
    transform_nt = ensure_transform_group()
    gin = _find_node_by_idname(wrapper_nt, "NodeGroupInput")

    transform_inst = None
    for n in wrapper_nt.nodes:
        if (n.bl_idname == "ShaderNodeGroup"
                and n.node_tree is transform_nt
                and n.get(TRANSFORM_INST_MARK)
                and n.get(WRAPPER_UV_BIND) == wrapper_uv_socket_name):
            transform_inst = n
            break

    if transform_inst is None:
        transform_inst = wrapper_nt.nodes.new("ShaderNodeGroup")
        transform_inst.node_tree = transform_nt
        transform_inst.label = "Transform"
        transform_inst[TRANSFORM_INST_MARK] = True
        transform_inst[WRAPPER_UV_BIND] = wrapper_uv_socket_name
        transform_count = sum(1 for n in wrapper_nt.nodes
                              if n.bl_idname == "ShaderNodeGroup" and n.get(TRANSFORM_INST_MARK))
        transform_inst.location = (-1100, 200 - (transform_count - 1) * 500)

        uv_out = gin.outputs.get(wrapper_uv_socket_name)
        if uv_out is not None:
            wrapper_nt.links.new(uv_out, transform_inst.inputs["UV"])
        # Pattern is driven by the hidden Value node (not a wrapper socket).
        _wire_pattern_value(wrapper_nt, transform_inst)
        for param in TRANSFORM_PARAM_NAMES:
            if param in transform_inst.inputs and param in gin.outputs:
                wrapper_nt.links.new(gin.outputs[param], transform_inst.inputs[param])

    raw_uv_socket = gin.outputs.get(wrapper_uv_socket_name)
    bypass_socket = gin.outputs.get("Bypass")

    def _ensure_bypass_mix(socket_label, transform_out_socket, y_offset):
        existing = _find_bypass_mix(wrapper_nt, transform_inst.name, socket_label)
        if existing is not None:
            return existing
        mix = wrapper_nt.nodes.new("ShaderNodeMix")
        mix.data_type = "VECTOR"
        mix.blend_type = "MIX"
        mix.label = f"Bypass {socket_label}"
        mix[BYPASS_OF_MARK] = transform_inst.name
        mix[BYPASS_SOCKET_MARK] = socket_label
        mix.location = (transform_inst.location.x + 380, transform_inst.location.y + y_offset)
        # MIX VECTOR socket indices: Factor=0, A=4, B=5, Result=1
        wrapper_nt.links.new(transform_out_socket, mix.inputs[4])
        if raw_uv_socket is not None:
            wrapper_nt.links.new(raw_uv_socket, mix.inputs[5])
        if bypass_socket is not None:
            wrapper_nt.links.new(bypass_socket, mix.inputs[0])
        return mix

    mix_a = _ensure_bypass_mix("UV A", transform_inst.outputs["UV A"], 100)
    mix_b = _ensure_bypass_mix("UV B", transform_inst.outputs["UV B"], -100)

    return transform_inst, mix_a.outputs[1], mix_b.outputs[1]


def _next_tex_row_y(wrapper_nt):
    existing = sum(1 for n in wrapper_nt.nodes
                   if n.bl_idname == "ShaderNodeTexImage" and n.get(ORIG_INSIDE_MARK))
    return 300 - existing * 700


def _add_wrapper_output(wrapper_nt, name, source_bl_idname):
    iface = wrapper_nt.interface
    existing_names = [
        it.name for it in iface.items_tree
        if getattr(it, "item_type", None) == "SOCKET" and it.in_out == "OUTPUT"
    ]
    final = name
    i = 1
    while final in existing_names:
        final = f"{name}.{i:03d}"
        i += 1
    iface.new_socket(name=final, in_out="OUTPUT", socket_type=_socket_type_for_output(source_bl_idname))
    return final


# --------------------------------------------------------------------------- #
#                              Wrap / Unwrap                                  #
# --------------------------------------------------------------------------- #


def wrap_texture(wrapper_inst, wrapper_nt, main_tree, orig_img):
    """Move `orig_img` from `main_tree` into the wrapper. Returns True if newly
    wrapped, False if it was already wrapped or invalid."""
    if orig_img.get(ORIG_INSIDE_MARK) or orig_img.get(DUP_INSIDE_MARK):
        return False

    for n in wrapper_nt.nodes:
        if n.bl_idname == "ShaderNodeTexImage" and n.get(ORIG_INSIDE_MARK) == orig_img.name:
            return False

    vec_in = orig_img.inputs.get("Vector")
    main_source_socket = None
    if vec_in is not None and vec_in.is_linked:
        main_source_socket = vec_in.links[0].from_socket

    uv_socket_name = _wrapper_uv_socket_name_for_main_source(wrapper_inst, main_source_socket)
    if uv_socket_name is None:
        uv_socket_name = _add_wrapper_uv_input(wrapper_nt)
        if main_source_socket is not None:
            main_tree.links.new(main_source_socket, wrapper_inst.inputs[uv_socket_name])

    transform_inst, gated_uv_a, gated_uv_b = _ensure_transform_instance(wrapper_nt, uv_socket_name)

    row_y = _next_tex_row_y(wrapper_nt)

    orig_inside = wrapper_nt.nodes.new("ShaderNodeTexImage")
    _copy_texture_props(orig_img, orig_inside)
    orig_inside.label = orig_img.name
    orig_inside[ORIG_INSIDE_MARK] = orig_img.name
    orig_inside.location = (-500, row_y + 80)

    dup_inside = wrapper_nt.nodes.new("ShaderNodeTexImage")
    _copy_texture_props(orig_img, dup_inside)
    dup_inside.label = orig_img.name + " (dup)"
    dup_inside[DUP_INSIDE_MARK] = orig_img.name
    dup_inside.location = (-500, row_y - 240)

    wrapper_nt.links.new(gated_uv_a, orig_inside.inputs["Vector"])
    wrapper_nt.links.new(gated_uv_b, dup_inside.inputs["Vector"])

    gout = _find_node_by_idname(wrapper_nt, "NodeGroupOutput")

    for out_idx, main_out_sock in enumerate(orig_img.outputs):
        if not main_out_sock.is_linked:
            continue

        inferred_pass = _infer_pass_name_from_socket(main_out_sock)
        wrapper_out_name = _add_wrapper_output(
            wrapper_nt,
            inferred_pass,
            main_out_sock.bl_idname,
        )

        dt = _mix_data_type_for_socket(main_out_sock)
        f_idx, a_idx, b_idx, r_idx = _MIX_SOCKETS[dt]

        mix = wrapper_nt.nodes.new("ShaderNodeMix")
        mix.data_type = dt
        mix.blend_type = "MIX"
        mix.label = f"{orig_img.name} {main_out_sock.name}"
        mix[MIX_INSIDE_MARK] = orig_img.name
        mix.location = (-100, row_y - 60 * out_idx)

        wrapper_nt.links.new(transform_inst.outputs["Weight"], mix.inputs[f_idx])
        wrapper_nt.links.new(orig_inside.outputs[out_idx], mix.inputs[a_idx])
        wrapper_nt.links.new(dup_inside.outputs[out_idx], mix.inputs[b_idx])
        wrapper_nt.links.new(mix.outputs[r_idx], gout.inputs[wrapper_out_name])

        wrapper_out_main = wrapper_inst.outputs[wrapper_out_name]
        for link in list(main_out_sock.links):
            target = link.to_socket
            main_tree.links.remove(link)
            main_tree.links.new(wrapper_out_main, target)

    main_tree.nodes.remove(orig_img)
    return True


def apply_to_material(mat, img_nodes):
    """Wrap a list of Image-Texture-Nodes (which must live in mat.node_tree) into
    mat's Patchwork wrapper. Returns (newly_wrapped, already)."""
    if not img_nodes:
        return 0, 0
    ensure_transform_group()

    pre_existing = find_wrapper_for_material(mat) is not None

    centroid = None
    if img_nodes:
        cx = sum(n.location.x for n in img_nodes) / len(img_nodes)
        cy = sum(n.location.y for n in img_nodes) / len(img_nodes)
        centroid = (cx, cy)

    wrapper_inst, wrapper_nt = ensure_wrapper_for_material(mat)
    main_tree = mat.node_tree

    if not pre_existing and centroid is not None:
        wrapper_inst.location = centroid

    newly = 0
    already = 0
    for img in img_nodes:
        try:
            _ = img.name
        except ReferenceError:
            continue
        if wrap_texture(wrapper_inst, wrapper_nt, main_tree, img):
            newly += 1
        else:
            already += 1

    _ensure_wrapper_debug_output(wrapper_nt)
    _layout_wrapper_nodes(wrapper_nt)

    return newly, already


def _migrate_wrapper_params(wrapper_nt):
    """Ensure every wrapper-level parameter is present and wired. Idempotent.

    - Removes any legacy `Pattern` interface socket (previous V2 design),
      carrying its default into the hidden Value node.
    - Adds missing transform params inside the Settings panel.
    - Ensures the hidden Pattern Value node exists and is wired to every
      transform_inst's Pattern input (replacing any old gin → transform_inst
      Pattern link).
    - Re-wires transform params from gin to each transform_inst.
    """
    iface = wrapper_nt.interface

    panel = next(
        (it for it in iface.items_tree
         if getattr(it, "item_type", None) == "PANEL" and it.name == "Anti-Tiling Settings"),
        None,
    )
    existing_input_names = {
        it.name for it in iface.items_tree
        if getattr(it, "item_type", None) == "SOCKET" and it.in_out == "INPUT"
    }

    # 1. Detect & remove legacy Pattern interface socket, carrying its value.
    legacy_pattern_default = None
    legacy_pattern_item = None
    for item in iface.items_tree:
        if (getattr(item, "item_type", None) == "SOCKET"
                and item.in_out == "INPUT"
                and item.name == PATTERN_INPUT_NAME):
            legacy_pattern_item = item
            try:
                legacy_pattern_default = float(item.default_value)
            except Exception:
                pass
            break

    if legacy_pattern_item is not None:
        try:
            iface.remove(legacy_pattern_item)
        except Exception:
            pass
        existing_input_names.discard(PATTERN_INPUT_NAME)

    # 2. Hidden Pattern Value node — create if missing, carry legacy default.
    val_node = _ensure_pattern_value_node(wrapper_nt)
    if legacy_pattern_default is not None:
        val_node.outputs[0].default_value = legacy_pattern_default

    # 3. Add any missing transform params (panel sockets)
    for spec in TRANSFORM_PARAM_SPECS:
        name, default, mn, mx, subtype, desc = spec
        if name in existing_input_names:
            continue
        new_kwargs = {"name": name, "in_out": "INPUT", "socket_type": "NodeSocketFloat"}
        if panel is not None:
            new_kwargs["parent"] = panel
        ps = iface.new_socket(**new_kwargs)
        ps.default_value = default
        ps.min_value = mn
        ps.max_value = mx
        ps.subtype = subtype
        ps.description = desc

    gin = _find_node_by_idname(wrapper_nt, "NodeGroupInput")
    if gin is None:
        return
    for n in wrapper_nt.nodes:
        if not (n.bl_idname == "ShaderNodeGroup" and n.get(TRANSFORM_INST_MARK)):
            continue
        # Pattern: always rewire to the Value node (any pre-existing link from
        # gin gets replaced).
        _wire_pattern_value(wrapper_nt, n)
        # Transform params: wire from gin if unlinked.
        for name in TRANSFORM_PARAM_NAMES:
            tgt = n.inputs.get(name)
            src = gin.outputs.get(name)
            if tgt is None or src is None or tgt.is_linked:
                continue
            wrapper_nt.links.new(src, tgt)


def _ensure_wrapper_debug_output(wrapper_nt):
    """Add a 'Debug' Color output to the wrapper and connect it from the
    first transform instance's Debug output. Moves the socket to the end
    of the interface so per-texture outputs (Base Color, …) stay on top.
    Idempotent."""
    iface = wrapper_nt.interface

    transform_inst = next(
        (n for n in wrapper_nt.nodes
         if n.bl_idname == "ShaderNodeGroup" and n.get(TRANSFORM_INST_MARK)),
        None,
    )
    if transform_inst is None or "Debug" not in transform_inst.outputs:
        return

    gout = _find_node_by_idname(wrapper_nt, "NodeGroupOutput")
    if gout is None:
        return

    debug_item = None
    for it in iface.items_tree:
        if (getattr(it, "item_type", None) == "SOCKET"
                and it.in_out == "OUTPUT"
                and it.name == "Debug"):
            debug_item = it
            break
    if debug_item is None:
        debug_item = iface.new_socket(
            name="Debug", in_out="OUTPUT", socket_type="NodeSocketColor",
        )
        debug_item.description = (
            "Debug: per-pattern cell pattern with white-highlighted edge zones — "
            "lets you visualise the anti-tiling structure."
        )

    debug_in = gout.inputs.get("Debug")
    if debug_in is not None and not debug_in.is_linked:
        wrapper_nt.links.new(transform_inst.outputs["Debug"], debug_in)

    try:
        iface.move(debug_item, len(iface.items_tree) - 1)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#                       Layout / auto-arrange                                 #
# --------------------------------------------------------------------------- #


def _layout_wrapper_nodes(wrapper_nt):
    """Lay out wrapper contents left-to-right by role: Group Input → Transform →
    Bypass → Texture (orig+dup) → Mix → Group Output. Stable across runs.
    """
    COL = {
        "gin":       -1700,
        "transform": -1200,
        "bypass":     -750,
        "tex":        -300,
        "mix":         200,
        "gout":        700,
    }
    ROW_HEIGHT = 600

    nodes = wrapper_nt.nodes
    gin = next((n for n in nodes if n.bl_idname == "NodeGroupInput"), None)
    gout = next((n for n in nodes if n.bl_idname == "NodeGroupOutput"), None)
    transforms = [n for n in nodes if n.bl_idname == "ShaderNodeGroup" and n.get(TRANSFORM_INST_MARK)]
    bypass_mixes = [n for n in nodes if n.bl_idname == "ShaderNodeMix" and n.get(BYPASS_OF_MARK)]
    origs = sorted(
        [n for n in nodes if n.bl_idname == "ShaderNodeTexImage" and n.get(ORIG_INSIDE_MARK)],
        key=lambda n: n.get(ORIG_INSIDE_MARK) or "",
    )
    dups_by_orig = {
        n.get(DUP_INSIDE_MARK): n
        for n in nodes
        if n.bl_idname == "ShaderNodeTexImage" and n.get(DUP_INSIDE_MARK)
    }
    mixes_by_orig = {}
    for n in nodes:
        if n.bl_idname == "ShaderNodeMix" and n.get(MIX_INSIDE_MARK):
            mixes_by_orig.setdefault(n.get(MIX_INSIDE_MARK), []).append(n)

    if gin is not None:
        gin.location = (COL["gin"], 0)

    transforms_sorted = sorted(transforms, key=lambda n: n.get(WRAPPER_UV_BIND) or "")
    for i, t in enumerate(transforms_sorted):
        t.location = (COL["transform"], 240 - i * 700)

    for bm in bypass_mixes:
        owner_name = bm.get(BYPASS_OF_MARK)
        owner = next((t for t in transforms if t.name == owner_name), None)
        if owner is not None:
            y_off = 120 if bm.get(BYPASS_SOCKET_MARK) == "UV A" else -150
            bm.location = (COL["bypass"], owner.location.y + y_off)

    for i, orig in enumerate(origs):
        row_y = 240 - i * ROW_HEIGHT
        orig.location = (COL["tex"], row_y + 40)
        dup = dups_by_orig.get(orig.get(ORIG_INSIDE_MARK))
        if dup is not None:
            dup.location = (COL["tex"], row_y - 280)

        mixes = sorted(
            mixes_by_orig.get(orig.get(ORIG_INSIDE_MARK), []),
            key=lambda m: (m.data_type, m.label or m.name),
        )
        for j, mix in enumerate(mixes):
            mix.location = (COL["mix"], row_y - j * 200)

    if gout is not None:
        gout.location = (COL["gout"], 0)


# --------------------------------------------------------------------------- #
#                                Unwrap                                       #
# --------------------------------------------------------------------------- #


def _mix_for_orig_inside(wrapper_nt, orig_name):
    return [n for n in wrapper_nt.nodes
            if n.bl_idname == "ShaderNodeMix" and n.get(MIX_INSIDE_MARK) == orig_name]


def unwrap_material(mat):
    """Dissolve mat's Patchwork wrapper: move every wrapped texture back into
    the main tree with its original UV source and downstream connections
    restored. Returns the number of textures restored."""
    wrapper_inst = find_wrapper_for_material(mat)
    if wrapper_inst is None:
        return 0
    wrapper_nt = wrapper_inst.node_tree
    main_tree = mat.node_tree
    transform_nt = bpy.data.node_groups.get(TRANSFORM_GROUP_NAME)

    uv_to_main_source = {}
    for inp in wrapper_inst.inputs:
        if inp.bl_idname != "NodeSocketVector":
            continue
        if not inp.name.startswith("UV"):
            continue
        if inp.is_linked:
            uv_to_main_source[inp.name] = inp.links[0].from_socket

    orig_names = [n.get(ORIG_INSIDE_MARK)
                  for n in wrapper_nt.nodes
                  if n.bl_idname == "ShaderNodeTexImage" and n.get(ORIG_INSIDE_MARK)]

    inst_location = wrapper_inst.location.copy()

    restored = 0
    for idx, orig_name in enumerate(orig_names):
        orig_inside = next((n for n in wrapper_nt.nodes
                            if n.bl_idname == "ShaderNodeTexImage" and n.get(ORIG_INSIDE_MARK) == orig_name), None)
        if orig_inside is None:
            continue

        main_source = None
        vec_in = orig_inside.inputs.get("Vector")
        if vec_in and vec_in.is_linked:
            upstream = vec_in.links[0].from_node
            transform_name = None
            if upstream.bl_idname == "ShaderNodeMix" and upstream.get(BYPASS_OF_MARK):
                transform_name = upstream.get(BYPASS_OF_MARK)
            elif (transform_nt is not None
                    and upstream.bl_idname == "ShaderNodeGroup"
                    and upstream.node_tree is transform_nt):
                transform_name = upstream.name
            if transform_name:
                transform_inst = wrapper_nt.nodes.get(transform_name)
                if transform_inst is not None:
                    uv_socket_name = transform_inst.get(WRAPPER_UV_BIND)
                    if uv_socket_name:
                        main_source = uv_to_main_source.get(uv_socket_name)

        new_tex = main_tree.nodes.new("ShaderNodeTexImage")
        _copy_texture_props(orig_inside, new_tex)
        new_tex.label = orig_name
        try:
            new_tex.name = orig_name
        except Exception:
            pass
        new_tex.location = (inst_location.x - 350, inst_location.y - idx * 350)

        if main_source is not None:
            main_tree.links.new(main_source, new_tex.inputs["Vector"])

        for mix in _mix_for_orig_inside(wrapper_nt, orig_name):
            dt = mix.data_type
            f_idx, a_idx, b_idx, r_idx = _MIX_SOCKETS[dt]
            a_in = mix.inputs[a_idx]
            if not a_in.is_linked:
                continue
            orig_socket_idx = list(orig_inside.outputs).index(a_in.links[0].from_socket)
            new_out_sock = new_tex.outputs[orig_socket_idx]

            for ml in list(mix.outputs[r_idx].links):
                if ml.to_node.bl_idname != "NodeGroupOutput":
                    continue
                wrapper_out_name = ml.to_socket.name
                wrapper_out_main = wrapper_inst.outputs.get(wrapper_out_name)
                if wrapper_out_main is None:
                    continue
                for main_link in list(wrapper_out_main.links):
                    target = main_link.to_socket
                    main_tree.links.remove(main_link)
                    main_tree.links.new(new_out_sock, target)
        restored += 1

    main_tree.nodes.remove(wrapper_inst)
    if wrapper_nt.users == 0:
        bpy.data.node_groups.remove(wrapper_nt)
    return restored


def unwrap_all():
    count = 0
    for mat in list(bpy.data.materials):
        try:
            n = unwrap_material(mat)
        except Exception:
            continue
        count += n
    return count


def cleanup_orphan_transform():
    nt = bpy.data.node_groups.get(TRANSFORM_GROUP_NAME)
    if nt is None or nt.users > 0:
        return False
    bpy.data.node_groups.remove(nt)
    return True


# --------------------------------------------------------------------------- #
#                       Compatibility / convenience                           #
# --------------------------------------------------------------------------- #


def is_wrapper_instance(node):
    return node.bl_idname == "ShaderNodeGroup" and bool(node.get(WRAPPER_NODE_MARK))


def material_for_tree(tree):
    for m in bpy.data.materials:
        if m.node_tree is tree:
            return m
    return None
