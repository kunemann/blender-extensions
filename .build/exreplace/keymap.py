import bpy


addon_keymaps = []


def _read_prefs():
    try:
        prefs = bpy.context.preferences.addons[__package__].preferences
        return prefs.key_type, prefs.use_shift, prefs.use_ctrl, prefs.use_alt
    except (KeyError, AttributeError):
        return "E", True, False, False


def register_keymap():
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc is None:
        return
    key, shift, ctrl, alt = _read_prefs()
    km = kc.keymaps.new(name="Node Editor", space_type="NODE_EDITOR")
    kmi = km.keymap_items.new(
        "exreplace.swap_connections",
        key,
        "PRESS",
        shift=shift,
        ctrl=ctrl,
        alt=alt,
    )
    addon_keymaps.append((km, kmi))


def unregister_keymap():
    for km, kmi in addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except (RuntimeError, ReferenceError):
            pass
    addon_keymaps.clear()
