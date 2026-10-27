# SPDX-License-Identifier: GPL-3.0-or-later
"""Pass definitions, view-layer flag mapping and robust socket-name aliasing.

The whole add-on is name-tolerant: we never assume a fixed set of sockets exists.
Whatever a Render Layers / multilayer-EXR node exposes is normalised to a canonical
key here, and the builder only wires up the keys that are actually present.
"""

import re

# --- Canonical pass keys -----------------------------------------------------
# Light passes (Cycles)
DIFF_DIR, DIFF_IND, DIFF_COL = "DIFF_DIR", "DIFF_IND", "DIFF_COL"
GLOSS_DIR, GLOSS_IND, GLOSS_COL = "GLOSS_DIR", "GLOSS_IND", "GLOSS_COL"
TRANS_DIR, TRANS_IND, TRANS_COL = "TRANS_DIR", "TRANS_IND", "TRANS_COL"
SSS_DIR, SSS_IND, SSS_COL = "SSS_DIR", "SSS_IND", "SSS_COL"
VOL_DIR, VOL_IND = "VOL_DIR", "VOL_IND"
EMIT, ENV = "EMIT", "ENV"
# Light passes (EEVEE)
DIFF_LIGHT, SPEC_LIGHT, SPEC_COL, VOL_LIGHT = "DIFF_LIGHT", "SPEC_LIGHT", "SPEC_COL", "VOL_LIGHT"
# Core image
COMBINED, ALPHA = "COMBINED", "ALPHA"
# Extra / data passes (passed through, not summed)
AO, SHADOW = "AO", "SHADOW"
DEPTH, MIST, NORMAL, VECTOR, UV, POSITION = "DEPTH", "MIST", "NORMAL", "VECTOR", "UV", "POSITION"
INDEX_OB, INDEX_MA = "INDEX_OB", "INDEX_MA"

# Human-readable label used for the generated group's socket names.
CANON_DISPLAY = {
    DIFF_DIR: "Diffuse Direct", DIFF_IND: "Diffuse Indirect", DIFF_COL: "Diffuse Color",
    GLOSS_DIR: "Glossy Direct", GLOSS_IND: "Glossy Indirect", GLOSS_COL: "Glossy Color",
    TRANS_DIR: "Transmission Direct", TRANS_IND: "Transmission Indirect", TRANS_COL: "Transmission Color",
    SSS_DIR: "Subsurface Direct", SSS_IND: "Subsurface Indirect", SSS_COL: "Subsurface Color",
    VOL_DIR: "Volume Direct", VOL_IND: "Volume Indirect",
    EMIT: "Emission", ENV: "Environment",
    DIFF_LIGHT: "Diffuse Light", SPEC_LIGHT: "Specular Light", SPEC_COL: "Specular Color",
    VOL_LIGHT: "Volume Light",
    COMBINED: "Image", ALPHA: "Alpha",
    AO: "Ambient Occlusion", SHADOW: "Shadow",
    DEPTH: "Depth", MIST: "Mist", NORMAL: "Normal", VECTOR: "Vector", UV: "UV",
    POSITION: "Position", INDEX_OB: "Object Index", INDEX_MA: "Material Index",
}

# Socket type kind per canonical key ("COLOR" / "FLOAT" / "VECTOR").
CANON_KIND = {
    DEPTH: "FLOAT", MIST: "FLOAT", ALPHA: "FLOAT", INDEX_OB: "FLOAT", INDEX_MA: "FLOAT",
    NORMAL: "VECTOR", VECTOR: "VECTOR", UV: "VECTOR", POSITION: "VECTOR",
}  # everything else defaults to COLOR


def kind_of(key):
    return CANON_KIND.get(key, "COLOR")


# --- Alias table: normalised socket name -> canonical key --------------------
def _norm(name):
    """Lowercase, drop everything but a-z/0-9 so 'Diffuse Direct', 'DiffDir' and
    'diffuse.direct' all collapse to the same token."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


_ALIASES = {
    DIFF_DIR: ["diffusedirect", "diffdir"],
    DIFF_IND: ["diffuseindirect", "diffind"],
    DIFF_COL: ["diffusecolor", "diffcol", "diffusecol"],
    GLOSS_DIR: ["glossydirect", "glossdir"],
    GLOSS_IND: ["glossyindirect", "glossind"],
    GLOSS_COL: ["glossycolor", "glosscol"],
    TRANS_DIR: ["transmissiondirect", "transdir", "transmdir"],
    TRANS_IND: ["transmissionindirect", "transind", "transmind"],
    TRANS_COL: ["transmissioncolor", "transcol", "transmcol"],
    SSS_DIR: ["subsurfacedirect", "sssdir", "subsurfdir"],
    SSS_IND: ["subsurfaceindirect", "sssind", "subsurfind"],
    SSS_COL: ["subsurfacecolor", "ssscol", "subsurfcol"],
    VOL_DIR: ["volumedirect", "voldir", "volumedir"],
    VOL_IND: ["volumeindirect", "volind", "volumeind"],
    EMIT: ["emission", "emit"],
    ENV: ["environment", "env"],
    DIFF_LIGHT: ["diffuselight", "difflight"],
    SPEC_LIGHT: ["specularlight", "speclight", "glossylight"],
    SPEC_COL: ["specularcolor", "speccol"],
    VOL_LIGHT: ["volumelight", "vollight"],
    COMBINED: ["image", "combined"],
    ALPHA: ["alpha"],
    AO: ["ambientocclusion", "ao"],
    SHADOW: ["shadow"],
    DEPTH: ["depth", "z"],
    MIST: ["mist"],
    NORMAL: ["normal"],
    VECTOR: ["vector", "speed"],
    UV: ["uv"],
    POSITION: ["position"],
    INDEX_OB: ["objectindex", "indexob"],
    INDEX_MA: ["materialindex", "indexma"],
}

_NORM_TO_KEY = {}
for _key, _names in _ALIASES.items():
    for _n in _names:
        _NORM_TO_KEY[_n] = _key


def classify_socket(socket_name):
    """Return (canonical_key, crypto_kind) for a socket display name.

    crypto_kind is None for ordinary passes, else one of 'object' / 'material' /
    'asset' / 'other' for Cryptomatte sockets (CryptoObject00, CryptoMaterial00, …).
    """
    n = _norm(socket_name)
    if n.startswith("crypto"):
        rest = n[len("crypto"):]
        for kind in ("object", "material", "asset"):
            if rest.startswith(kind):
                return None, kind
        return None, "other"
    return _NORM_TO_KEY.get(n), None


# --- Light-group recipes -----------------------------------------------------
# (label, direct_key, indirect_key, color_key)  ->  (Direct + Indirect) * Color
LIGHT_GROUPS_CYCLES = [
    ("Diffuse", DIFF_DIR, DIFF_IND, DIFF_COL),
    ("Glossy", GLOSS_DIR, GLOSS_IND, GLOSS_COL),
    ("Transmission", TRANS_DIR, TRANS_IND, TRANS_COL),
    ("Subsurface", SSS_DIR, SSS_IND, SSS_COL),
]
# (label, light_key, color_key) -> Light * Color
LIGHT_GROUPS_EEVEE = [
    ("Diffuse", DIFF_LIGHT, DIFF_COL),
    ("Specular", SPEC_LIGHT, SPEC_COL),
]
# (label, direct_key, indirect_key) -> Direct + Indirect (no colour multiply)
VOLUME_CYCLES = ("Volume", VOL_DIR, VOL_IND)
# additive single passes
ADDITIVE = [EMIT, ENV]

# Extra passes exposed (passed through) when running in "full" scope.
EXTRA_PASSTHROUGH = [AO, SHADOW, DEPTH, MIST, NORMAL, VECTOR, UV, POSITION, INDEX_OB, INDEX_MA]

# --- view_layer flag mapping -------------------------------------------------
# key -> (holder, attribute)   holder in {"vl", "cycles", "eevee"}
VIEW_LAYER_FLAGS = {
    DIFF_DIR: ("vl", "use_pass_diffuse_direct"),
    DIFF_IND: ("vl", "use_pass_diffuse_indirect"),
    DIFF_COL: ("vl", "use_pass_diffuse_color"),
    GLOSS_DIR: ("vl", "use_pass_glossy_direct"),
    GLOSS_IND: ("vl", "use_pass_glossy_indirect"),
    GLOSS_COL: ("vl", "use_pass_glossy_color"),
    TRANS_DIR: ("vl", "use_pass_transmission_direct"),
    TRANS_IND: ("vl", "use_pass_transmission_indirect"),
    TRANS_COL: ("vl", "use_pass_transmission_color"),
    SSS_DIR: ("vl", "use_pass_subsurface_direct"),
    SSS_IND: ("vl", "use_pass_subsurface_indirect"),
    SSS_COL: ("vl", "use_pass_subsurface_color"),
    VOL_DIR: ("cycles", "use_pass_volume_direct"),
    VOL_IND: ("cycles", "use_pass_volume_indirect"),
    EMIT: ("vl", "use_pass_emit"),
    ENV: ("vl", "use_pass_environment"),
    AO: ("vl", "use_pass_ambient_occlusion"),
    SHADOW: ("vl", "use_pass_shadow"),
    DEPTH: ("vl", "use_pass_z"),
    MIST: ("vl", "use_pass_mist"),
    NORMAL: ("vl", "use_pass_normal"),
    VECTOR: ("vl", "use_pass_vector"),
    UV: ("vl", "use_pass_uv"),
    POSITION: ("vl", "use_pass_position"),
    INDEX_OB: ("vl", "use_pass_object_index"),
    INDEX_MA: ("vl", "use_pass_material_index"),
}
