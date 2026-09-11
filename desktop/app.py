import atexit

import toga
from toga.style import Pack
from toga.style.pack import COLUMN, ROW

from desktop import credentials as creds_store
from desktop import logging_bridge
from desktop.theme import SECTIONS, THEME
from desktop.sections import sync as sync_section
from desktop.sections import garmin as garmin_section
from desktop.sections import divelogs as divelogs_section
from desktop.sections import settings as settings_section

SECTION_BUILDERS = {
    "Sync": sync_section.build,
    "Garmin Dives": garmin_section.build,
    "Divelogs Dives": divelogs_section.build,
    "Settings": settings_section.build,
}


class DiveSyncApp(toga.App):
    def startup(self):
        # Installed before any section is built so a section's on-press
        # handlers can rely on self.app.log_queue existing immediately.
        self.log_queue = logging_bridge.install()

        # Belt-and-braces: begin_operation()/end_operation() (see
        # desktop/credentials.py) are what actually keep the materialized
        # credentials file cleaned up around each sync/edit operation. This
        # only catches the unbalanced-counter edge case (e.g. a crash
        # mid-operation) - it won't fire on a real Cmd+Q/Quit-menu exit,
        # since toga-cocoa 0.5.6 doesn't route those through Python's normal
        # shutdown (App.on_exit/request_exit, or even atexit) at all.
        atexit.register(creds_store.clear_local_cache)

        self.sections = {name: builder(self) for name, builder in SECTION_BUILDERS.items()}

        self.main_content_area = toga.Box(style=Pack(direction=COLUMN, flex=1))

        main_column = toga.Box(style=Pack(direction=COLUMN, flex=1))
        main_column.add(self._build_topbar())
        main_column.add(self.main_content_area)

        root = toga.Box(style=Pack(direction=ROW))
        root.add(self._build_sidebar())
        root.add(main_column)

        # Fixed size, not resizable - per the redesign spec, a "standard
        # size based on the screen size" rather than a user-resizable window.
        self.main_window = toga.MainWindow(
            title=self.formal_name, size=self._standard_window_size(), resizable=False
        )
        self.main_window.content = root
        self.main_window.show()

        # First-run: land on Settings instead of Sync until credentials exist.
        initial_section = SECTIONS[0] if creds_store.has_any_credentials() else "Settings"
        self._select_section(initial_section)

    def _standard_window_size(self):
        """85% of the primary screen's size, clamped to a sensible range -
        wide/tall enough for the dive tables (9 possible columns) and the
        "Dive Data" detail card, but not comically large on a big display."""
        try:
            screen_width, screen_height = self.screens[0].size
        except Exception:
            return (1200, 800)
        width = max(1100, min(int(screen_width * 0.85), 1600))
        height = max(750, min(int(screen_height * 0.85), 1000))
        return (width, height)

    def _build_sidebar(self):
        sidebar = toga.Box(
            style=Pack(direction=COLUMN, width=200, background_color=THEME["sidebar_bg"])
        )
        sidebar.add(
            toga.Label(
                "DiveSync",
                style=Pack(
                    margin=(18, 16),
                    font_weight="bold",
                    font_size=16,
                    color=THEME["sidebar_text"],
                    background_color=THEME["sidebar_bg"],
                ),
            )
        )

        self.nav_buttons = {}
        for name in SECTIONS:
            button = toga.Button(
                name,
                on_press=self._nav_handler(name),
                style=Pack(
                    width=200,
                    margin_bottom=2,
                    background_color=THEME["sidebar_bg"],
                    color=THEME["sidebar_text"],
                ),
            )
            self.nav_buttons[name] = button
            sidebar.add(button)

        return sidebar

    def _nav_handler(self, name):
        def handler(widget):
            self._select_section(name)

        return handler

    def _select_section(self, name):
        self.active_section = name
        self.main_content_area.clear()
        self.main_content_area.add(self.sections[name])

        for section_name, button in self.nav_buttons.items():
            selected = section_name == name
            button.style.background_color = (
                THEME["nav_active_bg"] if selected else THEME["sidebar_bg"]
            )
            button.style.color = THEME["nav_active_text"] if selected else THEME["sidebar_text"]
        self.section_title_label.text = name

    def _build_topbar(self):
        self.section_title_label = toga.Label(
            SECTIONS[0],
            style=Pack(flex=1, font_weight="bold", font_size=14, color=THEME["nav_active_bg"]),
        )
        topbar = toga.Box(style=Pack(direction=ROW, align_items="center", margin=(14, 16)))
        topbar.add(self.section_title_label)
        return topbar


def main():
    # Packaged (Briefcase) runs pick these up from the app's dist-info
    # metadata automatically; explicit values here are what make
    # `python -m desktop` work from a plain source checkout too, where that
    # metadata doesn't exist.
    return DiveSyncApp(formal_name="DiveSync", app_id="com.mikaelchristersson.desktop")
