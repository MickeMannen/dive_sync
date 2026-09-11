import re

import toga
from toga.sources import AccessorColumn
from toga.style import Pack
from toga.style.pack import COLUMN, ROW

from desktop import credentials, preferences
from desktop.async_utils import run_in_thread, run_polled_job
from desktop.theme import THEME
from desktop.widgets import card
from src.core import dive_cache, scheduler

# (key, heading, numeric) - numeric columns sort by their leading number
# (stripping unit suffixes like "12 kg") rather than as plain text, so "10"
# doesn't sort before "2".
ALL_COLUMNS = [
    ("date", "Date", False),
    ("time", "Time", False),
    ("dive_number", "Dive #", True),
    ("location", "Location", False),
    ("max_depth", "Max Depth", True),
    ("duration", "Duration", True),
    ("buddy", "Buddy", False),
    ("weight", "Weight", True),
    ("visibility", "Visibility", True),
]
COLUMN_HEADINGS = {key: heading for key, heading, _ in ALL_COLUMNS}
COLUMN_NUMERIC = {key: numeric for key, _, numeric in ALL_COLUMNS}
DEFAULT_VISIBLE_COLUMNS = [key for key, _, _ in ALL_COLUMNS]

# Pixel widths sized to each column's actual content, in points - toga's
# cross-platform Table API has no concept of column width (AccessorColumn
# only takes heading/accessor), and toga-cocoa 0.5.6 defaults every table to
# NSTableViewColumnAutoresizingStyle.Uniform, which splits all leftover
# space evenly across columns regardless of content. Left uncorrected, short
# columns like Buddy/Weight/Visibility end up far wider than their text
# (see _apply_column_widths, which drops to the native NSTableColumn objects
# to fix this since there's no supported Toga API for it).
COLUMN_WIDTHS = {
    "date": 100,
    "time": 90,
    "dive_number": 70,
    "location": 260,
    "max_depth": 100,
    "duration": 90,
    "buddy": 110,
    "weight": 90,
    "visibility": 90,
}
DEFAULT_COLUMN_WIDTH = 100

FORM_FIELDS = (
    "dive_number_input", "date_input", "time_input", "duration_input", "max_depth_input",
    "location_input", "weight_input", "visibility_input", "buddy_input",
)
# Read-only fields populated from cached data but never saved back (see
# on_save, which builds its own explicit field list) - cleared alongside
# FORM_FIELDS and notes_input on deselect.
READONLY_DISPLAY_FIELDS = ("avg_depth_input", "water_temp_input")


def _sort_key(value, numeric: bool):
    if numeric:
        match = re.match(r"^\s*(-?[0-9]+(?:\.[0-9]+)?)", str(value if value is not None else ""))
        return float(match.group(1)) if match else float("-inf")
    return str(value or "").lower()


class DiveEditorSection:
    """Shared table + edit-form implementation for the Garmin and Divelogs
    dive editor sections - the two are identical apart from which service's
    cache/adapter they talk to (via src.core.dive_cache)."""

    def __init__(self, app, service: str, title: str):
        self.app = app
        self.service = service
        self._selected = None
        self._busy = False
        self._dives = []

        self._visible_columns = preferences.get_visible_columns(service, DEFAULT_VISIBLE_COLUMNS)
        self._sort_column, self._sort_ascending = preferences.get_sort(service, default_column="date")

        self.table = toga.Table(columns=[], on_select=self.on_select, style=Pack(flex=1, height=240))
        self._rebuild_table_columns()

        self.refresh_button = toga.Button("Refresh", on_press=self.on_refresh, style=Pack(margin_right=8))
        self.delete_button = toga.Button(
            "Delete", on_press=self.on_delete, enabled=False, style=Pack(margin_right=8)
        )
        self.columns_button = toga.Button(
            "Columns…", on_press=self.on_choose_columns, style=Pack(margin_right=8)
        )
        list_toolbar = toga.Box(style=Pack(direction=ROW, margin_bottom=4, align_items="center"))
        list_toolbar.add(self.refresh_button)
        list_toolbar.add(self.delete_button)
        list_toolbar.add(self.columns_button)

        self.sort_select = toga.Selection(on_change=self.on_sort_column_change, style=Pack(width=140))
        self.sort_direction_button = toga.Button(
            "", on_press=self.on_toggle_sort_direction, style=Pack(width=40, margin_left=4)
        )
        self.list_status_label = toga.Label("", style=Pack(color=THEME["text_muted"], flex=1, margin_left=12))
        sort_row = toga.Box(style=Pack(direction=ROW, margin_bottom=8, align_items="center"))
        sort_row.add(toga.Label("Sort by", style=Pack(margin_right=6, color=THEME["text_muted"])))
        sort_row.add(self.sort_select)
        sort_row.add(self.sort_direction_button)
        sort_row.add(self.list_status_label)
        self._refresh_sort_choices()

        # Indeterminate; only visible/animating while Refresh is fetching -
        # a full Garmin refetch loops per-dive detail/telemetry requests
        # with a cooldown between each, so this can take a while.
        self.list_progress_bar = toga.ProgressBar(max=None, style=Pack(margin_bottom=8))

        table_col = toga.Box(style=Pack(direction=COLUMN, flex=1))
        table_col.add(list_toolbar)
        table_col.add(sort_row)
        table_col.add(self.list_progress_bar)
        table_col.add(self.table)

        # Sized to match the width of the dive list table above (which
        # naturally stretches to fill the card - see DiveSyncApp.
        # _standard_window_size()), split into 3 equal columns with a 16px
        # gutter between them - rather than the two 270px-fixed columns the
        # "Dive Data" mockup originally used, which left most of a wide
        # window's width unused.
        window_width, _ = app._standard_window_size()
        col_gap = 16
        detail_width = window_width - 200 - 2 * 16  # sidebar (200) + card margins (16+16)
        col_width = (detail_width - 2 * col_gap) // 3
        date_width = (col_width - 4) // 2
        time_width = col_width - 4 - date_width

        field_style = Pack(width=col_width)
        self.dive_number_input = toga.TextInput(style=field_style)
        self.date_input = toga.TextInput(placeholder="YYYY-MM-DD", style=Pack(width=date_width, margin_right=4))
        self.time_input = toga.TextInput(placeholder="HH:MM:SS", style=Pack(width=time_width))
        self.duration_input = toga.TextInput(style=field_style)
        self.max_depth_input = toga.TextInput(style=field_style)
        self.avg_depth_input = toga.TextInput(readonly=True, style=field_style)
        self.water_temp_input = toga.TextInput(readonly=True, style=field_style)
        self.location_input = toga.TextInput(style=field_style)
        self.notes_input = toga.MultilineTextInput(style=Pack(flex=1, height=70))
        self.weight_input = toga.TextInput(style=field_style)
        self.visibility_input = toga.TextInput(style=field_style)
        self.buddy_input = toga.TextInput(style=field_style)
        self.tanks_input = toga.MultilineTextInput(readonly=True, style=Pack(flex=1, height=50))

        self.selected_label = toga.Label(
            "No dive selected", style=Pack(font_weight="bold", margin_bottom=8)
        )
        self.save_button = toga.Button(
            "Save", on_press=self.on_save, enabled=False, style=Pack(margin_right=8)
        )
        self.status_label = toga.Label("", style=Pack(color=THEME["text_muted"]))

        def field_row(label_text, widget):
            row = toga.Box(style=Pack(direction=COLUMN, margin_bottom=6))
            row.add(toga.Label(label_text, style=Pack(font_size=11, color=THEME["text_muted"])))
            row.add(widget)
            return row

        # Date/Time is edited as two separate fields (matching how it's
        # actually entered/read) and recombined into the single "date_time"
        # string dive_cache.update_dive_fields() expects when Saving.
        date_time_row = toga.Box(style=Pack(direction=COLUMN, margin_bottom=6))
        date_time_labels = toga.Box(style=Pack(direction=ROW))
        date_time_labels.add(toga.Label("Date", style=Pack(width=131, margin_right=4, font_size=11, color=THEME["text_muted"])))
        date_time_labels.add(toga.Label("Time", style=Pack(width=131, font_size=11, color=THEME["text_muted"])))
        date_time_inputs = toga.Box(style=Pack(direction=ROW))
        date_time_inputs.add(self.date_input)
        date_time_inputs.add(self.time_input)
        date_time_row.add(date_time_labels)
        date_time_row.add(date_time_inputs)

        # Three fixed-width columns (per col_width above), not flex - an
        # unanchored flex chain here previously sent Toga's Pack layout into
        # infinite recursion (RecursionError) on relayout.
        left_col = toga.Box(style=Pack(direction=COLUMN, width=col_width, margin_right=col_gap))
        left_col.add(field_row("Dive #", self.dive_number_input))
        left_col.add(date_time_row)
        left_col.add(field_row("Duration (min)", self.duration_input))

        mid_col = toga.Box(style=Pack(direction=COLUMN, width=col_width, margin_right=col_gap))
        mid_col.add(field_row("Max Depth (m)", self.max_depth_input))
        mid_col.add(field_row("Avg Depth (m)", self.avg_depth_input))
        mid_col.add(field_row("Water Temp", self.water_temp_input))

        right_col = toga.Box(style=Pack(direction=COLUMN, width=col_width))
        right_col.add(field_row("Location", self.location_input))
        right_col.add(field_row("Buddy", self.buddy_input))
        right_col.add(field_row("Weight", self.weight_input))
        right_col.add(field_row("Visibility", self.visibility_input))

        fields_row = toga.Box(style=Pack(direction=ROW))
        fields_row.add(left_col)
        fields_row.add(mid_col)
        fields_row.add(right_col)

        tanks_box = toga.Box(style=Pack(direction=COLUMN, margin_top=6, margin_bottom=6))
        tanks_box.add(toga.Label("Tanks / Gas", style=Pack(font_size=11, color=THEME["text_muted"])))
        tanks_box.add(self.tanks_input)

        notes_box = toga.Box(style=Pack(direction=COLUMN, margin_top=6, margin_bottom=6))
        notes_box.add(toga.Label("Notes", style=Pack(font_size=11, color=THEME["text_muted"])))
        notes_box.add(self.notes_input)

        save_row = toga.Box(style=Pack(direction=ROW, align_items="center", margin_top=4))
        save_row.add(self.save_button)
        save_row.add(self.status_label)

        scroll_content = toga.Box(style=Pack(direction=COLUMN, width=detail_width))
        scroll_content.add(fields_row)
        scroll_content.add(tanks_box)
        scroll_content.add(notes_box)
        scroll_content.add(save_row)

        # "Make the bottom part scrollable up and down" - a bounded-height
        # ScrollContainer, not the unbounded flex box the rest of this file
        # otherwise avoids; a fixed height here plays the same anchoring
        # role a fixed width does elsewhere (see notes above and in app.py).
        details_scroll = toga.ScrollContainer(
            content=scroll_content, horizontal=False, vertical=True,
            style=Pack(width=detail_width + 20, height=320),
        )

        details_card = card("Dive Data", self.selected_label, details_scroll)

        # Table above, details below - not side-by-side. Both handed
        # straight to card() (itself a COLUMN box) rather than wrapped in an
        # extra intermediate COLUMN box - that redundant nesting, combined
        # with flex=1 on both levels, was enough to send Toga's Pack layout
        # into infinite recursion (RecursionError) on relayout.
        self.content = toga.Box(style=Pack(direction=COLUMN, flex=1))
        self.content.add(card(title, table_col, details_card))

        self.refresh()

    # -- columns / sorting ------------------------------------------------

    def _rebuild_table_columns(self):
        for existing in list(self.table.columns):
            self.table.remove_column(existing)
        for key in self._visible_columns:
            self.table.append_column(AccessorColumn(heading=COLUMN_HEADINGS[key], accessor=key))
        self._apply_column_widths()

    def _apply_column_widths(self):
        try:
            from toga_cocoa.libs import NSTableViewColumnAutoresizingStyle

            native_table = self.table._impl.native_table
            native_columns = self.table._impl.columns
        except (ImportError, AttributeError):
            return

        # LastColumnOnly: every column keeps its content-sized width except
        # the last visible one, which stretches to absorb whatever space is
        # left in the table (rather than leaving it blank).
        native_table.columnAutoresizingStyle = NSTableViewColumnAutoresizingStyle.LastColumnOnly
        for index, (native_column, key) in enumerate(zip(native_columns, self._visible_columns)):
            width = COLUMN_WIDTHS.get(key, DEFAULT_COLUMN_WIDTH)
            native_column.minWidth = width
            native_column.width = width
            if index < len(native_columns) - 1:
                native_column.maxWidth = width

    def _refresh_sort_choices(self):
        labels = [COLUMN_HEADINGS[key] for key in self._visible_columns]
        self.sort_select.items = labels
        if self._sort_column not in self._visible_columns and self._visible_columns:
            self._sort_column = self._visible_columns[0]
        if self._sort_column in self._visible_columns:
            self.sort_select.value = COLUMN_HEADINGS[self._sort_column]
        self.sort_direction_button.text = "▲" if self._sort_ascending else "▼"

    def on_sort_column_change(self, widget):
        if not widget.value:
            return
        key = next((k for k, label in COLUMN_HEADINGS.items() if label == widget.value), None)
        if key is None:
            return
        self._sort_column = key
        preferences.set_sort(self.service, self._sort_column, self._sort_ascending)
        self._render_table()

    def on_toggle_sort_direction(self, widget):
        self._sort_ascending = not self._sort_ascending
        self.sort_direction_button.text = "▲" if self._sort_ascending else "▼"
        preferences.set_sort(self.service, self._sort_column, self._sort_ascending)
        self._render_table()

    async def on_choose_columns(self, widget):
        popup = toga.Window(title="Columns", size=(260, 60 + 32 * len(ALL_COLUMNS)))
        box = toga.Box(style=Pack(direction=COLUMN, margin=16))

        switches = {}
        for key, heading, _ in ALL_COLUMNS:
            sw = toga.Switch(heading, value=key in self._visible_columns)
            switches[key] = sw
            box.add(sw)

        def on_done(_widget):
            selected = [key for key, _, _ in ALL_COLUMNS if switches[key].value]
            if selected:
                self._visible_columns = selected
                preferences.set_visible_columns(self.service, selected)
                self._rebuild_table_columns()
                self._refresh_sort_choices()
                self._render_table()
            popup.close()

        button_row = toga.Box(style=Pack(direction=ROW, margin_top=8))
        button_row.add(toga.Button("Done", on_press=on_done))
        box.add(button_row)

        popup.content = box
        popup.show()

    # -- listing ------------------------------------------------------

    def _list_dives(self):
        if self.service == "garmin":
            return dive_cache.list_garmin_dives()
        return dive_cache.list_divelogs_dives()

    def _render_table(self):
        numeric = COLUMN_NUMERIC.get(self._sort_column, False)
        sorted_dives = sorted(
            self._dives,
            key=lambda d: _sort_key(d.get(self._sort_column), numeric),
            reverse=not self._sort_ascending,
        )
        self.table.data = sorted_dives

    def refresh(self):
        try:
            self._dives = self._list_dives()
            self.status_label.text = ""
        except Exception as e:
            self._dives = []
            self.status_label.text = f"Failed to load dives: {e}"
        self._render_table()
        self._select_row(None)

    def _drain_to_latest_log_line(self):
        latest = None
        while not self.app.log_queue.empty():
            try:
                latest = self.app.log_queue.get_nowait()
            except Exception:
                break
        if latest:
            self.list_status_label.text = latest

    async def on_refresh(self, widget):
        if self._busy or scheduler.is_sync_running or scheduler.is_download_running:
            self.list_status_label.text = "A sync or download is currently running — try again once it finishes."
            return

        self._busy = True
        self.refresh_button.enabled = False
        self.delete_button.enabled = False
        self.list_status_label.text = "Refreshing…"
        self.list_progress_bar.start()

        # Scoped to just this section's service, so refreshing Garmin Dives
        # doesn't also hit Divelogs' API (and vice versa).
        include_garmin = self.service == "garmin"
        include_divelogs = self.service == "divelogs"

        credentials.begin_operation()
        try:
            await run_polled_job(
                scheduler.run_download_thread,
                (False, None, include_garmin, include_divelogs),
                lambda: scheduler.is_download_running,
                self._drain_to_latest_log_line,
            )
        finally:
            credentials.end_operation()

        self.list_progress_bar.stop()
        self.refresh_button.enabled = True
        self._busy = False

        results = scheduler.last_download_results
        if results.get("error"):
            self.list_status_label.text = f"Refresh failed: {results['error']}"
        elif results.get("success"):
            self.list_status_label.text = "Refreshed."
        else:
            self.list_status_label.text = "Refresh finished with errors — check the Sync tab log."

        self.refresh()

    # -- selection ------------------------------------------------------

    def _clear_form(self):
        for name in FORM_FIELDS + READONLY_DISPLAY_FIELDS:
            getattr(self, name).value = ""
        self.notes_input.value = ""
        self.tanks_input.value = ""

    def _select_row(self, row):
        self._selected = row
        if row is None:
            self._clear_form()
            self.save_button.enabled = False
            self.delete_button.enabled = False
            self.selected_label.text = "No dive selected"
            return

        self.selected_label.text = f"{row.date_time} — {row.location or 'Unnamed dive'}"
        self.dive_number_input.value = str(row.dive_number or "")
        date_part, _, time_part = str(row.date_time or "").partition(" ")
        self.date_input.value = date_part
        self.time_input.value = time_part
        self.duration_input.value = str(row.duration or "")
        self.max_depth_input.value = str(row.max_depth or "")
        self.avg_depth_input.value = str(row.avg_depth or "")
        self.water_temp_input.value = str(row.water_temp or "")
        self.location_input.value = str(row.location or "")
        self.notes_input.value = str(row.notes or "")
        self.tanks_input.value = str(row.tanks or "")
        self.weight_input.value = str(row.weight or "")
        self.visibility_input.value = str(row.visibility or "")
        self.buddy_input.value = str(row.buddy or "")

        self.save_button.enabled = True
        self.delete_button.enabled = True

    def on_select(self, widget):
        self._select_row(widget.selection)

    # -- helpers ------------------------------------------------------

    def _combined_date_time(self) -> str:
        """Recombines the separate Date/Time input fields into the single
        "date_time" string dive_cache.update_dive_fields() expects."""
        date_val = self.date_input.value.strip()
        time_val = self.time_input.value.strip()
        if date_val and time_val:
            return f"{date_val} {time_val}"
        return date_val or time_val

    @staticmethod
    def _parse_optional_int(text):
        text = (text or "").strip()
        if not text:
            return None
        try:
            return int(float(text))
        except ValueError:
            return None

    @staticmethod
    def _parse_optional_float(text):
        text = (text or "").strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    # -- save / delete --------------------------------------------------

    async def on_save(self, widget):
        if self._selected is None or self._busy:
            return
        if scheduler.is_sync_running or scheduler.is_download_running:
            self.status_label.text = "A sync or download is currently running — try again once it finishes."
            return

        self._busy = True
        self.save_button.enabled = False
        self.status_label.text = "Saving…"

        filename = self._selected.filename
        fields = dict(
            dive_number=self.dive_number_input.value,
            date_time=self._combined_date_time(),
            duration=self._parse_optional_int(self.duration_input.value),
            max_depth=self._parse_optional_float(self.max_depth_input.value),
            location=self.location_input.value,
            notes=self.notes_input.value,
            weight=self.weight_input.value,
            visibility=self.visibility_input.value,
            buddy=self.buddy_input.value,
        )

        def do_save():
            filepath = dive_cache.update_dive_fields(self.service, filename, **fields)
            return dive_cache.push_remote_update(self.service, filepath)

        # Materializes the keychain credentials into the file push_remote_update
        # reads, only for the duration of this save (see desktop/credentials.py).
        credentials.begin_operation()
        try:
            result = await run_in_thread(do_save)
        finally:
            credentials.end_operation()

        self._busy = False
        self.save_button.enabled = True
        if result.get("ok"):
            pushed = result.get("value")
            self.status_label.text = (
                "Saved." if pushed else "Saved locally (remote push failed — check credentials/log)."
            )
            self.refresh()
        else:
            self.status_label.text = f"Failed: {result.get('error')}"

    async def on_delete(self, widget):
        if self._selected is None or self._busy:
            return
        if scheduler.is_sync_running or scheduler.is_download_running:
            self.status_label.text = "A sync or download is currently running — try again once it finishes."
            return

        confirmed = await self.app.main_window.dialog(
            toga.ConfirmDialog(
                "Delete dive",
                f"Delete this dive from {self.service.capitalize()} "
                "(removes the local cache file and the remote dive)?",
            )
        )
        if not confirmed:
            return

        self._busy = True
        self.delete_button.enabled = False
        self.status_label.text = "Deleting…"
        filename = self._selected.filename

        def do_delete():
            filepath, external_id = dive_cache.delete_dive_local(self.service, filename)
            if not external_id:
                return False
            return dive_cache.push_remote_delete(self.service, external_id, filepath=filepath)

        credentials.begin_operation()
        try:
            result = await run_in_thread(do_delete)
        finally:
            credentials.end_operation()

        self._busy = False
        if result.get("ok"):
            pushed = result.get("value")
            self.status_label.text = (
                "Deleted." if pushed else "Deleted locally (remote delete failed, skipped, or had no linked ID)."
            )
            self.refresh()
        else:
            self.delete_button.enabled = True
            self.status_label.text = f"Failed: {result.get('error')}"
