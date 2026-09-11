import toga
from toga.style import Pack
from toga.style.pack import COLUMN, ROW

from desktop import credentials
from desktop.async_utils import run_polled_job
from desktop.theme import THEME
from desktop.widgets import card
from src.core import scheduler

DIRECTION_LABELS = {
    "bidirectional": "Bidirectional",
    "to_divelogs": "To Divelogs",
    "to_garmin": "To Garmin",
}
DIRECTION_VALUES = {label: value for value, label in DIRECTION_LABELS.items()}


class SyncSection:
    def __init__(self, app):
        self.app = app
        self._running = False
        self._downloading = False

        self.direction_select = toga.Selection(
            items=list(DIRECTION_LABELS.values()), style=Pack(width=180)
        )
        self.dry_run_switch = toga.Switch("Dry run", value=False, style=Pack(margin_right=16))
        self.only_new_switch = toga.Switch("Only new dives", value=True, style=Pack(margin_right=16))
        self.sync_gases_switch = toga.Switch("Sync gases", value=True, style=Pack(margin_right=16))
        self.sync_fit_switch = toga.Switch("Sync FIT files", value=False)

        self.run_button = toga.Button("Sync now", on_press=self.on_run, style=Pack(width=160))
        self.status_label = toga.Label(
            "Idle", style=Pack(margin_left=16, color=THEME["text_muted"])
        )
        self.progress_bar = toga.ProgressBar(max=None, style=Pack(flex=1, margin_top=8))

        self.log_output = toga.MultilineTextInput(
            readonly=True, style=Pack(flex=1, height=280, margin_top=8)
        )

        # Download: the Garmin/Divelogs dive-editor tables only ever show
        # what's been cached to local per-dive JSON files, and a normal sync
        # never writes those (it fetches into memory for matching/pushing
        # only) - this has to run at least once before there's anything to
        # browse/edit.
        self.overwrite_switch = toga.Switch("Overwrite existing cache", value=False)
        self.download_button = toga.Button(
            "Download dives", on_press=self.on_download, style=Pack(width=160, margin_left=8)
        )

        direction_row = toga.Box(style=Pack(direction=ROW, align_items="center", margin_bottom=8))
        direction_row.add(toga.Label("Direction", style=Pack(margin_right=8)))
        direction_row.add(self.direction_select)

        switches_row = toga.Box(style=Pack(direction=ROW, margin_bottom=8, align_items="center"))
        switches_row.add(self.dry_run_switch)
        switches_row.add(self.only_new_switch)
        switches_row.add(self.sync_gases_switch)
        switches_row.add(self.sync_fit_switch)

        run_row = toga.Box(style=Pack(direction=ROW, align_items="center"))
        run_row.add(self.run_button)
        run_row.add(self.download_button)
        run_row.add(self.status_label)

        download_row = toga.Box(style=Pack(direction=ROW, align_items="center", margin_bottom=8))
        download_row.add(self.overwrite_switch)
        download_row.add(
            toga.Label(
                "Download dives fetches and caches raw dive data locally - run this at least once "
                "before browsing/editing dives in the Garmin/Divelogs Dives tabs.",
                style=Pack(color=THEME["text_muted"], font_size=10, margin_left=8, flex=1),
            )
        )

        self.content = toga.Box(style=Pack(direction=COLUMN, flex=1))
        self.content.add(
            card(
                "Sync",
                direction_row,
                switches_row,
                download_row,
                run_row,
                self.progress_bar,
                self.log_output,
            )
        )

    def _custom_settings(self):
        return {
            "directionality": DIRECTION_VALUES.get(self.direction_select.value, "bidirectional"),
            "only_new": self.only_new_switch.value,
            "sync_gases": self.sync_gases_switch.value,
            "sync_fit": self.sync_fit_switch.value,
        }

    def append_log(self, text: str):
        self.log_output.value += text
        try:
            self.log_output.scroll_to_bottom()
        except Exception:
            pass

    def _drain_log_queue(self):
        while not self.app.log_queue.empty():
            try:
                line = self.app.log_queue.get_nowait()
            except Exception:
                break
            self.append_log(line + "\n")

    def _busy(self) -> bool:
        return self._running or self._downloading or scheduler.is_sync_running or scheduler.is_download_running

    def _set_buttons_enabled(self, enabled: bool):
        self.run_button.enabled = enabled
        self.download_button.enabled = enabled

    async def _run_background_job(self, target, args, is_running_attr: str, results_attr: str):
        """Shared plumbing for on_run/on_download: starts `target` in a
        thread, polls the scheduler module's `is_running_attr` flag while
        draining the log queue, and returns scheduler's `results_attr`."""
        credentials.begin_operation()
        try:
            await run_polled_job(
                target, args, lambda: getattr(scheduler, is_running_attr), self._drain_log_queue
            )
        finally:
            credentials.end_operation()

        return getattr(scheduler, results_attr)

    async def on_run(self, widget):
        if self._busy():
            self.status_label.text = "A sync or download is already running."
            return

        self._running = True
        self._set_buttons_enabled(False)
        self.status_label.text = "Running…"
        self.log_output.value = ""
        self.progress_bar.start()

        dry_run = self.dry_run_switch.value
        custom_settings = self._custom_settings()

        results = await self._run_background_job(
            scheduler.run_sync_thread, (dry_run, custom_settings), "is_sync_running", "last_sync_results"
        )

        self.progress_bar.stop()
        self._set_buttons_enabled(True)
        self._running = False

        if results.get("error"):
            self.status_label.text = f"Failed: {results['error']}"
        else:
            self.status_label.text = "Dry run complete." if dry_run else "Sync complete."

    async def on_download(self, widget):
        if self._busy():
            self.status_label.text = "A sync or download is already running."
            return

        self._downloading = True
        self._set_buttons_enabled(False)
        self.status_label.text = "Downloading…"
        self.log_output.value = ""
        self.progress_bar.start()

        overwrite = self.overwrite_switch.value

        results = await self._run_background_job(
            scheduler.run_download_thread, (overwrite,), "is_download_running", "last_download_results"
        )

        self.progress_bar.stop()
        self._set_buttons_enabled(True)
        self._downloading = False

        if results.get("error"):
            self.status_label.text = f"Download failed: {results['error']}"
        elif results.get("success"):
            self.status_label.text = "Download complete."
        else:
            self.status_label.text = "Download finished with errors — check the log."


def build(app) -> toga.Box:
    section = SyncSection(app)
    app.sync_section = section
    return section.content
