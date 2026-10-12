"""What a run should do."""
from dataclasses import dataclass

from debrief.core.config import DEFAULTS, SPEAKER_MODEL_DIR
from debrief.features.summary import summary_context


@dataclass
class Options:
    whisper: str = DEFAULTS["whisper"]
    lang: str = DEFAULTS["lang"]
    speakers: str = DEFAULTS["speakers"]  # "auto", a number of people, or "off"
    model: str | None = None  # the Ollama model for the summary; None skips the summary
    kind: str = DEFAULTS["kind"]  # the meeting type, which picks what the summary asks for
    hint: str = ""  # extra context for the summary, e.g. "senior backend role"
    summary_only: bool = False  # redo the summary from an existing transcript

    def __post_init__(self):
        if not SPEAKER_MODEL_DIR.exists():  # nothing to separate speakers with
            self.speakers = "off"

    @property
    def task(self) -> str:
        """What the summary model is asked to write."""
        return summary_context(self.kind, self.hint)
