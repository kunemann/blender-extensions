"""Addon preferences: master password (stored in plaintext so locking never
re-prompts) and enforcement/backup settings.

The plaintext lives only in the local userpref, never in a shipped .blend, and
is the key used to encrypt every lock. Unlock still requires typing the
password — that gate is what protects a distributed .blend."""

import hmac

import bpy

from .core import crypto


def get_prefs():
    return bpy.context.preferences.addons[__package__].preferences


def master_password_set() -> bool:
    try:
        prefs = get_prefs()
    except KeyError:
        return False
    return bool(prefs.master_password)


def get_master_password() -> str:
    return get_prefs().master_password


def verify_master_password(password: str) -> bool:
    return hmac.compare_digest(password, get_prefs().master_password)


class LockNodesPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    master_password: bpy.props.StringProperty(subtype="PASSWORD", options={"HIDDEN"})
    kdf_iters: bpy.props.IntProperty(
        name="KDF Iterations",
        description="PBKDF2 iterations for password hashing and encryption keys",
        default=crypto.DEFAULT_ITERS,
        min=100_000,
        max=5_000_000,
    )
    backup_on_hard_lock: bpy.props.BoolProperty(
        name="Write Backup Before Hard Lock",
        description=(
            "Write a plaintext JSON backup of the locked nodes next to the "
            ".blend before deleting them. Never ship this file with a Lite pack"
        ),
        default=True,
    )
    placeholder_color: bpy.props.FloatVectorProperty(
        name="Base Color",
        description="Flat base colour a locked group outputs where the pro nodes were",
        subtype="COLOR",
        size=4,
        min=0.0,
        max=1.0,
        default=(0.4, 0.4, 0.4, 1.0),
    )
    placeholder_label: bpy.props.StringProperty(
        name="Watermark Text",
        description="Text tiled as a watermark over the locked group's output, "
        "e.g. 'Full version on SuperHive'. Leave empty for none",
        default="",
    )
    watchdog_interval: bpy.props.FloatProperty(
        name="Watchdog Interval",
        description="Seconds between enforcement checks while locks exist",
        default=0.15,
        min=0.05,
        max=1.0,
        subtype="TIME_ABSOLUTE",
    )
    show_advanced: bpy.props.BoolProperty(name="Advanced", default=False)

    def draw(self, context):
        layout = self.layout

        box = layout.box()
        box.label(text="Master Password", icon="LOCKED")
        if self.master_password:
            box.label(text="A master password is set — locking uses it automatically.")
            box.operator("locknodes.change_master_password", icon="FILE_REFRESH")
        else:
            box.label(text="No master password set — required for locking.", icon="ERROR")
            box.operator("locknodes.set_master_password", icon="KEYINGSET")
        box.label(
            text="Stored in plaintext in this local userpref only — never in a "
            "shipped .blend. Unlocking still requires the password.",
            icon="INFO",
        )
        box.label(
            text="Changing it does not affect already-locked files — "
            "they keep the password used at lock time.",
            icon="INFO",
        )

        row = layout.row()
        row.prop(self, "backup_on_hard_lock")
        if not self.backup_on_hard_lock:
            warn = layout.row()
            warn.alert = True
            warn.label(
                text="Without a backup, a deleted lock property means the "
                "hard-locked nodes are gone forever.",
                icon="ERROR",
            )

        box = layout.box()
        box.label(text="Placeholder shown after locking", icon="NODE")
        box.prop(self, "placeholder_color", text="Base color")
        box.prop(self, "placeholder_label", text="Watermark text")

        layout.prop(self, "show_advanced", toggle=True)
        if self.show_advanced:
            box = layout.box()
            box.prop(self, "kdf_iters")
            box.prop(self, "watchdog_interval")


class LOCKNODES_OT_set_master_password(bpy.types.Operator):
    bl_idname = "locknodes.set_master_password"
    bl_label = "Set Master Password"
    bl_description = "Set the LockNodes master password (stored as salted hash only)"
    bl_options = {"INTERNAL"}

    password: bpy.props.StringProperty(
        name="Password", subtype="PASSWORD", options={"SKIP_SAVE"}
    )
    confirm: bpy.props.StringProperty(
        name="Confirm", subtype="PASSWORD", options={"SKIP_SAVE"}
    )

    @classmethod
    def poll(cls, context):
        return not master_password_set()

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "password")
        layout.prop(self, "confirm")

    def execute(self, context):
        if not self.password:
            self.report({"ERROR"}, "Enter a password")
            return {"CANCELLED"}
        if self.password != self.confirm:
            self.report({"ERROR"}, "Passwords do not match")
            return {"CANCELLED"}
        get_prefs().master_password = self.password
        bpy.ops.wm.save_userpref()
        self.report({"INFO"}, "Master password set")
        return {"FINISHED"}


class LOCKNODES_OT_change_master_password(bpy.types.Operator):
    bl_idname = "locknodes.change_master_password"
    bl_label = "Change Master Password"
    bl_description = "Change the master password — requires the current one"
    bl_options = {"INTERNAL"}

    old_password: bpy.props.StringProperty(
        name="Current Password", subtype="PASSWORD", options={"SKIP_SAVE"}
    )
    new_password: bpy.props.StringProperty(
        name="New Password", subtype="PASSWORD", options={"SKIP_SAVE"}
    )
    confirm: bpy.props.StringProperty(
        name="Confirm New", subtype="PASSWORD", options={"SKIP_SAVE"}
    )

    @classmethod
    def poll(cls, context):
        return master_password_set()

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "old_password")
        layout.prop(self, "new_password")
        layout.prop(self, "confirm")

    def execute(self, context):
        if not verify_master_password(self.old_password):
            self.report({"ERROR"}, "Current password is wrong")
            return {"CANCELLED"}
        if not self.new_password:
            self.report({"ERROR"}, "Enter a new password")
            return {"CANCELLED"}
        if self.new_password != self.confirm:
            self.report({"ERROR"}, "New passwords do not match")
            return {"CANCELLED"}
        get_prefs().master_password = self.new_password
        bpy.ops.wm.save_userpref()
        self.report({"INFO"}, "Master password changed")
        return {"FINISHED"}


classes = (
    LockNodesPreferences,
    LOCKNODES_OT_set_master_password,
    LOCKNODES_OT_change_master_password,
)
