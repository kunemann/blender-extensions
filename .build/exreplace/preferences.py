import bpy
from bpy.props import EnumProperty, BoolProperty


KEY_ITEMS = [
    ('A', 'A', ''), ('B', 'B', ''), ('C', 'C', ''), ('D', 'D', ''),
    ('E', 'E', ''), ('F', 'F', ''), ('G', 'G', ''), ('H', 'H', ''),
    ('I', 'I', ''), ('J', 'J', ''), ('K', 'K', ''), ('L', 'L', ''),
    ('M', 'M', ''), ('N', 'N', ''), ('O', 'O', ''), ('P', 'P', ''),
    ('Q', 'Q', ''), ('R', 'R', ''), ('S', 'S', ''), ('T', 'T', ''),
    ('U', 'U', ''), ('V', 'V', ''), ('W', 'W', ''), ('X', 'X', ''),
    ('Y', 'Y', ''), ('Z', 'Z', ''),
    ('F1', 'F1', ''), ('F2', 'F2', ''), ('F3', 'F3', ''), ('F4', 'F4', ''),
    ('F5', 'F5', ''), ('F6', 'F6', ''), ('F7', 'F7', ''), ('F8', 'F8', ''),
    ('F9', 'F9', ''), ('F10', 'F10', ''), ('F11', 'F11', ''), ('F12', 'F12', ''),
    ('SPACE', 'Space', ''),
    ('TAB', 'Tab', ''),
    ('ZERO', '0', ''), ('ONE', '1', ''), ('TWO', '2', ''), ('THREE', '3', ''),
    ('FOUR', '4', ''), ('FIVE', '5', ''), ('SIX', '6', ''), ('SEVEN', '7', ''),
    ('EIGHT', '8', ''), ('NINE', '9', ''),
]


def _refresh_keymap(self, context):
    from . import keymap as km
    km.unregister_keymap()
    km.register_keymap()


class EXREPLACE_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    key_type: EnumProperty(
        name="Key",
        items=KEY_ITEMS,
        default='E',
        update=_refresh_keymap,
    )
    use_shift: BoolProperty(
        name="Shift",
        default=True,
        update=_refresh_keymap,
    )
    use_ctrl: BoolProperty(
        name="Ctrl",
        default=False,
        update=_refresh_keymap,
    )
    use_alt: BoolProperty(
        name="Alt",
        default=False,
        update=_refresh_keymap,
    )

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="Compositor Shortcut", icon='KEYINGSET')
        row = box.row(align=True)
        row.prop(self, "use_ctrl", text="Ctrl", toggle=True)
        row.prop(self, "use_shift", text="Shift", toggle=True)
        row.prop(self, "use_alt", text="Alt", toggle=True)
        row.prop(self, "key_type", text="")
