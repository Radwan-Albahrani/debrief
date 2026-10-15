"""The main screen: the meetings list on the left, the selected meeting (or a running job) on the right.

All of `app.state` is read and written on the UI thread; worker threads only compute and hand results back."""
from pathlib import Path

from rich.text import Text
from textual import work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Input, Markdown, OptionList, Static
from textual.widgets.option_list import Option
from textual.worker import get_current_worker

from debrief.core import settings
from debrief.core.formatting import clock, filesize, plural, tilde, when
from debrief.features import audio, export, library, transcription
from debrief.pipeline import Job, Options
from debrief.surfaces.tui.popups import MeetingTypePicker, SettingsPopup
from debrief.surfaces.tui.progress import process_stats, render_job


class MainScreen(Screen):
    CSS = """
    #main { height: 1fr; }
    #left { width: 42; }
    .box { border: round $panel-lighten-2; border-title-color: $text-muted; }
    .box:focus-within, .box:focus { border: round $accent; border-title-color: $accent; }
    #search { display: none; border: round $accent; height: 3; }
    #meetings { height: 1fr; padding: 0 1; }
    #meetings > .option-list--option { padding: 0 0 1 0; }
    #right { width: 1fr; padding: 0 2; }
    #info { height: auto; margin: 1 0; }
    #doc { height: auto; }
    """
    BINDINGS = [
        Binding("enter", "run", "Transcribe", priority=True),
        Binding("tab", "toggle_view", "Summary ⇄ Transcript", priority=True),
        Binding("c", "copy", "Copy summary"),
        Binding("r", "resummarize", "Redo summary"),
        Binding("slash", "search", "Search"),
        Binding("comma", "settings", "Settings"),
        Binding("escape", "back", "Cancel run"),
        Binding("q", "app.quit", "Quit"),
        Binding("j", "move(1)", show=False),
        Binding("k", "move(-1)", show=False),
        Binding("space,pagedown,ctrl+d", "scroll(1)", show=False),
        Binding("pageup,ctrl+u", "scroll(-1)", show=False),
    ]

    def __init__(self):
        super().__init__()
        self.meetings: list[library.Meeting] = []  # every meeting found, newest first
        self.shown: list[Path] = []  # the ones matching the search, in list order
        self.selected: Path | None = None
        self.search_text = ""
        self.view = "summary"  # or "transcript"
        self.rerun_armed: Path | None = None  # enter on a transcribed meeting asks once before redoing it
        self.streamed_summary = None
        self.stats = None  # (cpu %, bytes) of the running whisperkit-cli

    @property
    def state(self) -> dict:
        return self.app.state

    def compose(self):
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Input(id="search", placeholder="search meetings")
                yield OptionList(id="meetings", classes="box")
            with VerticalScroll(id="right", classes="box"):
                yield Static(id="info")
                yield Markdown(id="doc")
        yield Footer()

    def on_mount(self):
        self.rescan()
        self.query_one("#meetings").focus()
        self.set_interval(0.15, self.tick)
        self.set_interval(1.0, self.sample_stats)
        self.set_interval(5.0, self.watch_library)

    # ── the meetings list ────────────────────────────────────────────────────

    def rescan(self):
        self.meetings = library.find_meetings(settings.libraries(self.state))
        self.show_list()
        self.measure_durations(list(self.meetings))

    def show_list(self, keep: Path | None = None):
        keep = keep or self.selected
        visible = [m for m in self.meetings if self.search_text.lower() in m.name.lower()]
        self.shown = [m.path for m in visible]
        meetings = self.query_one("#meetings", OptionList)
        meetings.border_title = f"Meetings · {len(visible)}"
        meetings.clear_options()
        for m in visible:
            meetings.add_option(Option(self.row(m), id=str(m.path)))
        if not visible:
            self.selected = None
            return self.show_empty()
        index = self.shown.index(keep) if keep in self.shown else 0
        meetings.highlighted = index
        self.select(self.shown[index])

    def row(self, meeting: library.Meeting) -> Text:
        if self.busy(meeting.path):
            status = ("◐ running", "cyan")
        elif library.output_file(meeting.path, library.TRANSCRIPT).exists():
            status = ("✓", "green")
        else:
            status = ("● new", "yellow")
        meta = when(meeting.recorded)
        if seconds := self.duration(meeting.path):
            meta += f" · {clock(seconds)}"
        if len(settings.libraries(self.state)) > 1:
            meta += f" · {meeting.path.parent.name}"
        return Text.assemble((meeting.name, "bold"), "  ", status, "\n", (meta, "dim"))

    def duration(self, path: Path) -> float | None:
        return self.state["durations"].get(str(path), [None, None])[1]

    @work(thread=True, exclusive=True, group="durations")
    def measure_durations(self, meetings: list[library.Meeting]):
        """Read the length of each meeting whose audio changed since it was last measured."""
        known = dict(self.state["durations"])
        updates = {}
        for m in meetings:
            if get_current_worker().is_cancelled:
                return
            if known.get(str(m.path), [None])[0] != m.recorded:
                updates[str(m.path)] = [m.recorded, audio.duration_seconds(library.audio_files(m.path))]
        if updates and not get_current_worker().is_cancelled:
            self.app.call_from_thread(self._store_durations, updates)

    def _store_durations(self, updates: dict):
        self.state["durations"].update(updates)
        settings.save(self.state)
        self.show_list()

    @work(thread=True, exclusive=True, group="watch")
    def watch_library(self):
        """Pick up new or removed recordings without a restart."""
        roots = settings.libraries(self.state)
        found = library.find_meetings(roots)
        if [m.path for m in found] != [m.path for m in self.meetings]:
            self.app.call_from_thread(self._apply_scan, found, roots)

    def _apply_scan(self, found: list[library.Meeting], roots: list[Path]):
        stale = roots != settings.libraries(self.state)  # the folders changed while it was scanning
        if not stale and not self.query_one("#search").has_focus:
            self.meetings = found
            self.show_list()
            self.measure_durations(list(found))

    # ── the right pane ───────────────────────────────────────────────────────

    def show_empty(self):
        self.query_one("#right").border_title = "debrief"
        message = (f"No meetings match “{self.search_text}”." if self.search_text else
                   "No recordings in " + ", ".join(tilde(p) for p in settings.libraries(self.state)) + " yet.")
        self.query_one("#info", Static).update(Text.assemble(
            (message, "bold"), "\n\nEach subfolder (or audio file) in a recordings folder is one meeting.\nPress ",
            (",", "bold cyan"), " then ", ("a", "bold cyan"), " to add a recordings folder."))
        self.query_one("#doc", Markdown).update("")

    def select(self, path: Path):
        self.selected, self.rerun_armed = path, None
        right = self.query_one("#right")
        right.border_title = path.name
        if self.busy(path):
            self.streamed_summary = None  # tick() takes over the pane
            return
        files = library.audio_files(path)
        seconds = self.duration(path)
        facts = [clock(seconds) if seconds else "measuring…", plural(len(files), "file"),
                 filesize(sum(f.stat().st_size for f in files))]
        transcript = library.body_of(library.read(path, library.TRANSCRIPT))
        if n := transcription.speaker_count(transcript):
            facts.append(f"{n} speakers")
        info = Text(" · ".join(facts))
        if transcript:
            info.append(f"   transcribed {when(library.output_file(path, library.TRANSCRIPT).stat().st_mtime)}",
                        style="dim")
            info.append("\n\n")
            for view in ("summary", "transcript"):
                info.append(f" {view.capitalize()} ", style="bold reverse cyan" if self.view == view else "dim")
                info.append("  ")
            info.append("tab switch", style="dim")
            if self.view == "transcript":
                doc = transcript
            else:
                saved = library.read(path, library.SUMMARY)
                doc = library.body_of(saved) or "_No summary for this meeting._"
                if footer := library.footer_of(saved):
                    info.append(f"\n{footer}", style="dim")
        else:
            doc = "Not transcribed yet. Press **enter** to start."
        self.query_one("#info", Static).update(info)
        self.query_one("#doc", Markdown).update(doc)
        right.scroll_home(animate=False)

    def tick(self):
        """Redraw the running job's progress and the summary as it streams in."""
        job = self.app.job
        if not (job and job.running and self.selected == job.target):
            return
        self.query_one("#info", Static).update(render_job(job, self.stats))
        if job.summary != self.streamed_summary:
            self.streamed_summary = job.summary
            self.query_one("#doc", Markdown).update(job.summary)
            self.query_one("#right", VerticalScroll).scroll_end(animate=False)

    @work(thread=True, exclusive=True, group="stats")
    def sample_stats(self):
        job = self.app.job
        proc = job.proc if job and job.running and self.selected == job.target else None
        self.stats = process_stats(proc.pid) if proc and proc.poll() is None else None

    # ── running jobs ─────────────────────────────────────────────────────────

    def busy(self, path: Path | None = None) -> bool:
        """A job is running (on `path`, if given)."""
        job = self.app.job
        return bool(job and job.running and (path is None or job.target == path))

    def start_job(self, path: Path, summary_only: bool = False, kind: str | None = None, hint: str = "",
                  hold_summary: bool = False) -> Job:
        options = Options(whisper=self.state["whisper"], lang=self.state["lang"], speakers=self.state["speakers"],
                          model=self.app.summary_model(), kind=kind or self.state["kind"], hint=hint,
                          summary_only=summary_only)
        self.app.job = job = Job(path, options)
        if hold_summary:
            job.hold_summary()  # until the meeting-type popup is answered or closes itself
        self.streamed_summary = None
        self.query_one("#doc", Markdown).update("")
        self.show_list(keep=path)
        self.refresh_bindings()
        self.execute(job)
        return job

    @work(thread=True)
    def execute(self, job: Job):
        job.run()
        try:
            self.app.call_from_thread(self.job_done, job)
        except RuntimeError:  # the app was closed while the job ran
            pass

    def job_done(self, job: Job):
        self.refresh_bindings()
        if job.cancelled:
            self.notify("The run was cancelled.", title="Cancelled", severity="warning", timeout=10)
        elif job.error:
            self.notify(job.error, title="Failed", severity="error", timeout=10, markup=False)
        else:
            self.notify(f"{job.target.name}: done in {clock(job.ended - job.started)}", title="Transcribed",
                        timeout=8, markup=False)
            self.app.bell()
        self.show_list()

    def ask_kind(self, note: str, then, close_when=lambda: False):
        """Ask for the meeting type and notes; the type also becomes the default for next time."""
        def chosen(result):
            if result:
                self.state["kind"] = result[0]
                settings.save(self.state)
            then(result)
        self.app.push_screen(MeetingTypePicker(self.state["kind"], note, close_when), chosen)

    # ── actions ──────────────────────────────────────────────────────────────

    def check_action(self, action, parameters):
        if action == "back":
            return self.busy() or self.query_one("#search").display
        if action == "run" and self.busy():
            return None  # shown, but disabled
        return True

    def action_run(self):
        if self.query_one("#search").has_focus:
            return self.query_one("#meetings").focus()
        if self.busy() or not self.selected:
            return
        whisper = self.state["whisper"]
        if not transcription.ready(whisper):
            return self.notify(f"Whisper {whisper} is not set up. Quit and run: debrief setup -w {whisper}",
                               severity="error", timeout=10)
        path = self.selected
        if library.output_file(path, library.TRANSCRIPT).exists() and self.rerun_armed != path:
            self.rerun_armed = path
            return self.notify("Already transcribed. Press enter again to redo it.")
        has_summary = bool(self.app.summary_model())
        job = self.start_job(path, hold_summary=has_summary)
        if not has_summary:
            return

        def chosen(result):
            try:
                if not result:
                    return
                if not job.set_kind(*result):
                    return self.notify("The run had already ended. Press r to redo the summary with these notes.")
                self.notify(f"The summary will be written as: {result[0]}" + (" + your notes" if result[1] else ""),
                            markup=False)
            finally:
                job.release_summary()
        self.ask_kind("Transcription has started in the background. Pick the meeting type and add notes if you "
                      "like (a paragraph is fine); the summary waits for you once you start typing.", chosen,
                      close_when=lambda: job.waiting_for_kind or not job.running)

    def action_resummarize(self):
        if self.busy() or not self.selected:
            return
        if not library.output_file(self.selected, library.TRANSCRIPT).exists():
            return self.notify("Transcribe it first (enter).", severity="warning")
        if not self.app.summary_model():
            return self.notify("No summary model. Press , then m, or open the Ollama app.", severity="warning")
        path = self.selected
        self.ask_kind("Redo the summary of this meeting as:",
                      lambda r: r and self.start_job(path, summary_only=True, kind=r[0], hint=r[1]))

    def action_copy(self):
        if not self.selected:
            return
        saved = library.read(self.selected, library.SUMMARY)
        if not saved:
            return self.notify("No summary for this meeting yet.", severity="warning")
        export.copy_to_clipboard(export.to_slack(saved))
        self.notify("Formatted for Slack.", title="Summary copied")

    def action_toggle_view(self):
        if not self.selected or self.busy(self.selected):
            return
        self.view = "transcript" if self.view == "summary" else "summary"
        self.select(self.selected)

    def action_search(self):
        search = self.query_one("#search", Input)
        search.display = True
        search.focus()
        self.refresh_bindings()

    def action_back(self):
        search = self.query_one("#search", Input)
        if search.display:
            search.value, search.display, self.search_text = "", False, ""
            self.show_list()
            self.query_one("#meetings").focus()
            self.refresh_bindings()
        elif self.busy():
            self.app.job.cancel()
            self.notify("Cancelling…")

    def action_settings(self):
        self.app.push_screen(SettingsPopup(), lambda _: self.rescan())

    def action_move(self, delta: int):
        meetings = self.query_one("#meetings", OptionList)
        meetings.action_cursor_down() if delta > 0 else meetings.action_cursor_up()

    def action_scroll(self, direction: int):
        right = self.query_one("#right", VerticalScroll)
        right.scroll_page_down() if direction > 0 else right.scroll_page_up()

    # ── events ───────────────────────────────────────────────────────────────

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted):
        if event.option.id:
            self.select(Path(event.option.id))

    def on_input_changed(self, event: Input.Changed):
        self.search_text = event.value
        self.show_list()
