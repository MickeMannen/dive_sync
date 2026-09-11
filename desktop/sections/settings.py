import toga
from toga.style import Pack
from toga.style.pack import COLUMN, ROW

from desktop import credentials as creds_store
from desktop.async_utils import run_in_thread
from desktop.theme import THEME
from desktop.widgets import card


def _field_row(label_text, widget, hint=None):
    row = toga.Box(style=Pack(direction=COLUMN, margin_bottom=8))
    row.add(toga.Label(label_text, style=Pack(font_size=11, color=THEME["text_muted"])))
    row.add(widget)
    if hint:
        row.add(toga.Label(hint, style=Pack(font_size=10, color=THEME["text_muted"])))
    return row


class SettingsSection:
    def __init__(self, app):
        self.app = app

        model = creds_store.load_credentials_model()
        garmin_accounts = model.get_garmin_accounts()
        divelogs_accounts = model.get_divelogs_accounts()
        self._garmin = garmin_accounts[0] if garmin_accounts else None
        self._divelogs = divelogs_accounts[0] if divelogs_accounts else None

        field_style = Pack(flex=1)
        password_hint = "Leave blank to keep the currently saved password."

        # -- Garmin --------------------------------------------------
        self.garmin_username_input = toga.TextInput(
            value=self._garmin.username if self._garmin else "", style=field_style
        )
        self.garmin_password_input = toga.PasswordInput(style=field_style)
        self.garmin_token_dir_input = toga.TextInput(
            value=(self._garmin.token_dir if self._garmin else "") or creds_store.DEFAULT_GARMIN_TOKEN_DIR,
            style=field_style,
        )
        self.garmin_status_label = toga.Label("", style=Pack(color=THEME["text_muted"], margin_top=4))

        garmin_box = toga.Box(style=Pack(direction=COLUMN))
        garmin_box.add(_field_row("Username", self.garmin_username_input))
        garmin_box.add(_field_row("Password", self.garmin_password_input, hint=password_hint))
        garmin_box.add(_field_row("Token directory", self.garmin_token_dir_input))
        garmin_test_row = toga.Box(style=Pack(direction=ROW, align_items="center"))
        garmin_test_row.add(toga.Button("Test", on_press=self.on_test_garmin, style=Pack(margin_right=8)))
        garmin_test_row.add(self.garmin_status_label)
        garmin_box.add(garmin_test_row)

        # -- Divelogs --------------------------------------------------
        self.divelogs_username_input = toga.TextInput(
            value=self._divelogs.username if self._divelogs else "", style=field_style
        )
        self.divelogs_password_input = toga.PasswordInput(style=field_style)
        self.divelogs_status_label = toga.Label("", style=Pack(color=THEME["text_muted"], margin_top=4))

        divelogs_box = toga.Box(style=Pack(direction=COLUMN))
        divelogs_box.add(_field_row("Username", self.divelogs_username_input))
        divelogs_box.add(_field_row("Password", self.divelogs_password_input, hint=password_hint))
        divelogs_test_row = toga.Box(style=Pack(direction=ROW, align_items="center"))
        divelogs_test_row.add(
            toga.Button("Test", on_press=self.on_test_divelogs, style=Pack(margin_right=8))
        )
        divelogs_test_row.add(self.divelogs_status_label)
        divelogs_box.add(divelogs_test_row)

        # -- Save --------------------------------------------------
        self.save_button = toga.Button(
            "Save credentials", on_press=self.on_save, style=Pack(margin_right=8)
        )
        self.save_status_label = toga.Label("", style=Pack(color=THEME["text_muted"]))
        save_row = toga.Box(style=Pack(direction=ROW, align_items="center", margin_top=8))
        save_row.add(self.save_button)
        save_row.add(self.save_status_label)

        note_text = (
            "Credentials are stored in your OS keychain, not in a plain file."
            if creds_store.has_any_credentials()
            else (
                "Welcome! Enter your Garmin Connect and/or Divelogs.org credentials "
                "below to get started - they're stored in your OS keychain, not in a plain file."
            )
        )
        note = toga.Label(note_text, style=Pack(color=THEME["text_muted"], margin_bottom=12, font_size=11))

        self.content = toga.Box(style=Pack(direction=COLUMN, flex=1))
        self.content.add(
            card(
                "Settings",
                note,
                card("Garmin Connect", garmin_box),
                card("Divelogs.org", divelogs_box),
                save_row,
            )
        )

    # -- test buttons --------------------------------------------------

    async def on_test_garmin(self, widget):
        username = self.garmin_username_input.value
        password = self.garmin_password_input.value or (self._garmin.password if self._garmin else "")
        token_dir = self.garmin_token_dir_input.value or creds_store.DEFAULT_GARMIN_TOKEN_DIR
        if not username or not password:
            self.garmin_status_label.text = "Enter a username and password first."
            return
        self.garmin_status_label.text = "Testing…"

        def do_test():
            from src.core.services.garmin import GarminAdapter
            return GarminAdapter(username, password, token_dir=token_dir).login()

        result = await run_in_thread(do_test)
        if result.get("ok"):
            self.garmin_status_label.text = "Login OK." if result.get("value") else "Login failed."
        else:
            self.garmin_status_label.text = f"Error: {result.get('error')}"

    async def on_test_divelogs(self, widget):
        username = self.divelogs_username_input.value
        password = self.divelogs_password_input.value or (self._divelogs.password if self._divelogs else "")
        if not username or not password:
            self.divelogs_status_label.text = "Enter a username and password first."
            return
        self.divelogs_status_label.text = "Testing…"

        def do_test():
            from src.core.services.divelogs import DivelogsAdapter
            return DivelogsAdapter(username, password).login()

        result = await run_in_thread(do_test)
        if result.get("ok"):
            self.divelogs_status_label.text = "Login OK." if result.get("value") else "Login failed."
        else:
            self.divelogs_status_label.text = f"Error: {result.get('error')}"

    # -- save --------------------------------------------------

    def on_save(self, widget):
        from src.core.config import CredentialsModel, GarminCredentials, DivelogsCredentials

        garmin_password = self.garmin_password_input.value or (self._garmin.password if self._garmin else "")
        divelogs_password = self.divelogs_password_input.value or (
            self._divelogs.password if self._divelogs else ""
        )

        model = CredentialsModel(
            garmin=GarminCredentials(
                username=self.garmin_username_input.value,
                password=garmin_password,
                token_dir=self.garmin_token_dir_input.value or creds_store.DEFAULT_GARMIN_TOKEN_DIR,
            ),
            divelogs=DivelogsCredentials(
                username=self.divelogs_username_input.value,
                password=divelogs_password,
            ),
        )
        creds_store.save_credentials_model(model)

        garmin_accounts = model.get_garmin_accounts()
        divelogs_accounts = model.get_divelogs_accounts()
        self._garmin = garmin_accounts[0] if garmin_accounts else None
        self._divelogs = divelogs_accounts[0] if divelogs_accounts else None
        self.garmin_password_input.value = ""
        self.divelogs_password_input.value = ""
        self.save_status_label.text = "Saved to keychain."


def build(app) -> toga.Box:
    section = SettingsSection(app)
    app.settings_section = section
    return section.content
