"""The full-screen app: owns the settings, the summary models and the one job that can run at a time."""
from pathlib import Path

from textual.app import App
from textual.binding import Binding

from debrief.core import settings
from debrief.core.config import LANGUAGES, MEETING_KINDS, SPEAKER_CHOICES, WHISPER_MODELS
from debrief.core.formatting import tilde
from debrief.features import summary, transcription
from debrief.pipeline import Job
from debrief.surfaces.tui.main_screen import MainScreen

CYCLES = {"whisper": list(WHISPER_MODELS), "lang": list(LANGUAGES), "speakers": list(SPEAKER_CHOICES),
          "kind": MEETING_KINDS}


class DebriefApp(App):
    TITLE = "debrief"
    BINDINGS = [Binding("ctrl+c", "quit", "Quit", show=False, priority=True)]

    def __init__(self):
        super().__init__()
        self.state = settings.load()
        if not transcription.ready(self.state["whisper"]):  # e.g. only turbo was set up: use what is there
            self.state["whisper"] = next((w for w in WHISPER_MODELS if transcription.ready(w)), self.state["whisper"])
        self.models = summary.installed_models()  # None when Ollama isn't running
        if self.models and self.state.get("model") not in [*self.models, "none"]:
            self.state["model"] = summary.default_model(self.models)
        self.job: Job | None = None

    def on_mount(self):
        self.push_screen(MainScreen())

    def summary_model(self) -> str | None:
        """The chosen Ollama model, or None when summaries are off or Ollama isn't available."""
        model = self.state.get("model")
        return model if self.models and model in self.models else None

    def cycle_setting(self, key: str):
        if key == "model":
            if not self.models:
                return self.notify("Ollama is not running. Open the Ollama app.", severity="warning")
            values = [*self.models, "none"]
        else:
            values = CYCLES[key]
        current = self.state.get(key)
        self.state[key] = values[(values.index(current) + 1) % len(values)] if current in values else values[0]
        settings.save(self.state)

    def set_libraries(self, paths: list[Path]):
        self.state["libraries"] = list(dict.fromkeys(tilde(p) for p in paths))
        settings.save(self.state)

    def action_quit(self):
        if self.job and self.job.running:
            self.job.cancel()
        self.exit()
