"""Bidirectional aliases for compositor output socket names that may differ
between a Render Layer node and a multilayer EXR Image node.

Each tuple is a group of names treated as equivalent. We try the source
socket's `name` and `identifier` against the target's `name` and `identifier`,
then fall back to these alias groups (matching either field).
"""

ALIAS_GROUPS = (
    ("Image", "Combined"),
    ("Z", "Depth"),
    ("Emit", "Emission"),
    ("Env", "Environment"),
    ("DiffDir", "Diffuse Direct"),
    ("DiffInd", "Diffuse Indirect"),
    ("DiffCol", "Diffuse Color"),
    ("GlossDir", "Glossy Direct"),
    ("GlossInd", "Glossy Indirect"),
    ("GlossCol", "Glossy Color"),
    ("TransDir", "Transmission Direct"),
    ("TransInd", "Transmission Indirect"),
    ("TransCol", "Transmission Color"),
    ("VolumeDir", "Volume Direct"),
    ("VolumeInd", "Volume Indirect"),
    ("IndexOB", "Object Index"),
    ("IndexMA", "Material Index"),
    ("Shadow", "Shadow"),
    ("AO", "AO", "Ambient Occlusion"),
    ("Normal", "Normal"),
    ("UV", "UV"),
    ("Vector", "Vector", "Speed"),
    ("Mist", "Mist"),
    ("Position", "Position"),
)


def _socket_keys(sock):
    return (sock.name, sock.identifier)


def find_match(source_socket, target_outputs):
    src_keys = set(_socket_keys(source_socket))

    for sock in target_outputs:
        if sock.identifier == source_socket.identifier:
            return sock

    for sock in target_outputs:
        if sock.name == source_socket.name:
            return sock

    for group in ALIAS_GROUPS:
        gset = set(group)
        if src_keys & gset:
            for sock in target_outputs:
                if set(_socket_keys(sock)) & gset:
                    return sock

    return None
