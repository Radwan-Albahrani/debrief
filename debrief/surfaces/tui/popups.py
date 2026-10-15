"""The popups: choosing a folder, choosing the meeting type, and settings."""
from collections.abc import Callable
from pathlib import Path

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import work
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DirectoryTree, Footer, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from debrief.core.config import (LANGUAGES, MEETING_KINDS, MODELS_DIR, SPEAKER_CHOICES, SPEAKER_MODEL_DIR,
                                 WHISPER_MODELS)
from debrief.core import settings
from debrief.core.formatting import filesize, plural, tilde
from debrief.features import library, transcription

POPUP_CSS = "align: center middle;"
PANEL_CSS = "border: round $accent; border-title-color: $accent; background: $surface;"


class FolderPicker(ModalScreen[Path]):
    """Browse folders in the app; `a` picks the highlighted one."""

    class _Folders(DirectoryTree):
        def filter_paths(self, paths):
            return [p for p in paths if p.is_dir() and not p.name.startswith(".")]

    CSS = f"FolderPicker {{ {POPUP_CSS} }} #picker {{ width: 80; height: 80%; {PANEL_CSS} }}"
    BINDINGS = [Binding("a,space", "choose", "Add this folder", priority=True),
                Binding("escape,q", "dismiss", "Close")]

    def compose(self):
        yield self._Folders(Path.home(), id="picker")
        yield Footer()

    def on_mount(self):
        tree = self.query_one("#picker")
        tree.border_title = "Pick a recordings folder · ↑↓ move · enter open · a add"
        tree.focus()

    def action_choose(self):
        node = self.query_one("#picker", DirectoryTree).cursor_node
        if node and node.data:
            self.dismiss(Path(node.data.path))


class MeetingTypePicker(ModalScreen[tuple[str, str]]):
    """The meeting type and optional notes for the summary: a paragraph is fine (ratings, impressions, the role).

    It can open while transcription already runs. Left untouched, it closes itself once `close_when()` is true (the
    summary is ready to start). Once you pick a type or start typing it stays open until you confirm or skip, and
    the caller holds the summary back meanwhile, so nothing you typed is lost."""

    CSS = f"""
    MeetingTypePicker {{ {POPUP_CSS} }}
    #kind-panel {{ width: 84; height: auto; padding: 1 2; {PANEL_CSS} }}
    #kinds {{ height: auto; margin: 1 0; }}
    #notes {{ height: 9; }}
    """
    BINDINGS = [Binding("ctrl+s", "confirm", "Use this", priority=True), Binding("escape", "dismiss", "Skip")]

    def __init__(self, current: str, note: str, close_when: Callable[[], bool] = lambda: False):
        super().__init__()
        self.current, self.note, self.close_when = current, note, close_when
        self.first_choice = MEETING_KINDS.index(current) if current in MEETING_KINDS else 0
        self.touched = False

    def compose(self):
        with Vertical(id="kind-panel"):
            yield Static(Text(self.note, style="dim"))
            yield OptionList(*[Option(k, id=k) for k in MEETING_KINDS], id="kinds")
            yield TextArea(id="notes", tab_behavior="focus",
                           placeholder="Optional notes for the summary: the role, your ratings, what to focus on…")
        yield Footer()

    def on_mount(self):
        self.query_one("#kind-panel").border_title = "What kind of meeting?  ↑↓ type · tab notes · ctrl+s use"
        kinds = self.query_one("#kinds", OptionList)
        kinds.highlighted = self.first_choice
        kinds.focus()
        self.set_interval(0.3, self._close_if_untouched)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted):
        if event.option_index != self.first_choice:
            self.touched = True

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        self.action_confirm()  # enter on a type confirms; in the notes box enter is a new line

    def on_text_area_changed(self, event: TextArea.Changed):
        self.touched = True

    def _close_if_untouched(self):
        if not self.touched and self.close_when():
            self.notify(f"Summary started as: {self.current}. Press r afterwards to redo it as something else.")
            self.dismiss(None)

    def action_confirm(self):
        kind = MEETING_KINDS[self.query_one("#kinds", OptionList).highlighted or 0]
        self.dismiss((kind, self.query_one("#notes", TextArea).text.strip()))


class SettingsPopup(ModalScreen[None]):
    """Recordings folders, transcription and summary settings, and the state of each model."""

    CSS = f"""
    SettingsPopup {{ {POPUP_CSS} }}
    #panel {{ width: 76; height: auto; padding: 1 2; {PANEL_CSS} }}
    #folders {{ height: auto; max-height: 8; border: round $panel-lighten-2; border-title-color: $text-muted;
               margin-bottom: 1; }}
    #folders:focus {{ border: round $accent; border-title-color: $accent; }}
    """
    BINDINGS = [
        Binding("a", "add", "Add folder"),
        Binding("d", "remove", "Remove folder"),
        Binding("w", "cycle('whisper')", "Whisper"),
        Binding("l", "cycle('lang')", "Language"),
        Binding("s", "cycle('speakers')", "Speakers"),
        Binding("m", "cycle('model')", "Model"),
        Binding("t", "cycle('kind')", "Meeting type"),
        Binding("escape,comma,q", "dismiss", "Close"),
    ]

    def compose(self):
        with Vertical(id="panel"):
            yield OptionList(id="folders")
            yield Static(id="settings")
            yield Static(id="models")
        yield Footer()

    def on_mount(self):
        self.query_one("#panel").border_title = "Settings"
        self.query_one("#folders").border_title = "Recordings folders · a add · d remove"
        self._show_folders()
        self._show_settings()
        self.query_one("#models", Static).update(Text("\nChecking models…", style="dim"))
        self._measure_models()  # multi-GB folders: measured off the UI thread, once per opening
        self.query_one("#folders").focus()

    def _show_folders(self):
        folders = self.query_one("#folders", OptionList)
        folders.clear_options()
        for root in settings.libraries(self.app.state):
            status = (f"   {plural(len(library.find_meetings([root])), 'meeting')}", "dim") if root.is_dir() \
                else ("   missing", "red")
            folders.add_option(Option(Text.assemble(tilde(root), status), id=str(root)))

    def _show_settings(self):
        state, model = self.app.state, self.app.summary_model()
        rows = [("w", "Whisper", WHISPER_MODELS[state["whisper"]][1]), ("l", "Language", LANGUAGES[state["lang"]]),
                ("s", "Speakers", SPEAKER_CHOICES[state["speakers"]]), ("m", "Summary", model or "off"),
                ("t", "Meeting type", state["kind"] if model else "–")]
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold cyan")
        table.add_column(style="dim")
        table.add_column()
        for row in rows:
            table.add_row(*row)
        self.query_one("#settings", Static).update(table)

    @work(thread=True, exclusive=True)
    def _measure_models(self):
        def size(path):
            return filesize(sum(f.stat().st_size for f in path.rglob("*") if f.is_file()))

        rows = []
        for name in WHISPER_MODELS:
            path = transcription.model_path(name)
            rows.append((transcription.ready(name), f"Whisper {name}",
                         size(path) if path.exists() else f"missing · debrief setup -w {name}"))
        rows.append((SPEAKER_MODEL_DIR.exists(), "Speaker separation",
                     size(SPEAKER_MODEL_DIR) if SPEAKER_MODEL_DIR.exists() else "missing"))
        models = self.app.models
        rows.append((bool(models), "Ollama", ", ".join(models) if models else "not running (open the Ollama app)"))
        self.app.call_from_thread(self._show_models, rows)

    def _show_models(self, rows: list[tuple[bool, str, str]]):
        table = Table.grid(padding=(0, 2))
        for ok, label, detail in rows:
            table.add_row(Text("●", style="green" if ok else "red"), label, Text(detail, style="dim"))
        self.query_one("#models", Static).update(
            Group("", Text("Models", style="bold"), table, "", Text(f"Models live in {tilde(MODELS_DIR)}", style="dim")))

    def action_cycle(self, key: str):
        self.app.cycle_setting(key)
        self._show_settings()

    def action_add(self):
        def added(path):
            if path:
                self.app.set_libraries(settings.libraries(self.app.state) + [path])
                self._show_folders()
        self.app.push_screen(FolderPicker(), added)

    def action_remove(self):
        roots = settings.libraries(self.app.state)
        index = self.query_one("#folders", OptionList).highlighted
        if index is None or len(roots) == 1:
            return self.notify("Keep at least one recordings folder.", severity="warning")
        self.app.set_libraries(roots[:index] + roots[index + 1:])
        self._show_folders()
