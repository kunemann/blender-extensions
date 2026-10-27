"""Lock idprop schema access and the locked-tree registry.

The idprop on the NodeTree datablock is the single source of truth — it
travels with the .blend through save/append/link. The registry is only a
session cache so the watchdog can idle cheaply when nothing is locked.
"""

from . import trees

IDPROP_KEY = "locknodes"
SCHEMA_VERSION = 1
ADDON_VERSION = "1.1.0"

MODE_HARD = "hard"


_entries = []
_dirty = True

pending_finalize = set()


def get_lock(tree):
    if tree is None:
        return None
    return tree.get(IDPROP_KEY)


def is_locked(tree) -> bool:
    return get_lock(tree) is not None


def stamp_lock(tree, data: dict) -> None:
    tree[IDPROP_KEY] = data
    mark_dirty()


def clear_lock(tree) -> None:
    if IDPROP_KEY in tree.keys():
        del tree[IDPROP_KEY]
    pending_finalize.discard(tree.name)
    mark_dirty()


def mark_dirty() -> None:
    global _dirty
    _dirty = True


def has_locks() -> bool:
    global _dirty, _entries
    if _dirty:
        _entries = []
        for tree in trees.iter_all_trees():
            if get_lock(tree) is not None:
                _entries.append(tree.name)
        _dirty = False
    return bool(_entries)
