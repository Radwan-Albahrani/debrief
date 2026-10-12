"""The user's saved settings: one small JSON file, merged over the defaults and validated on load."""
import json
import os
from pathlib import Path

from debrief.core.config import (DEFAULT_LIBRARY, DEFAULTS, LANGUAGES, MEETING_KINDS, SPEAKER_CHOICES, STATE_FILE,
                                 WHISPER_MODELS)

ALLOWED = {"whisper": WHISPER_MODELS, "lang": LANGUAGES, "speakers": SPEAKER_CHOICES, "kind": MEETING_KINDS}


def load() -> dict:
    """The saved settings over the defaults. A missing, corrupt or hand-broken file never stops the app."""
    try:
        saved = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        saved = {}
    state = {**DEFAULTS, **(saved if isinstance(saved, dict) else {})}
    for key, allowed in ALLOWED.items():
        if not (isinstance(state[key], str) and state[key] in allowed):
            state[key] = DEFAULTS[key]
    if not isinstance(state.get("durations"), dict):
        state["durations"] = {}  # meeting path -> [newest audio mtime, seconds]
    return state


def save(state: dict) -> None:
    """Write atomically, so a crash mid-write can't leave a truncated file behind."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_FILE)


def libraries(state: dict) -> list[Path]:
    """The recordings folders to look for meetings in."""
    return [Path(p).expanduser() for p in state.get("libraries") or [DEFAULT_LIBRARY]]
