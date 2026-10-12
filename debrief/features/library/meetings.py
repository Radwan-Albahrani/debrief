"""Finding meetings: each subfolder (or loose audio file) of a recordings folder is one meeting."""
import re
from dataclasses import dataclass
from pathlib import Path

from debrief.core.config import AUDIO_EXTENSIONS


@dataclass(frozen=True)
class Meeting:
    path: Path
    recorded: float  # mtime of its newest audio file

    @property
    def name(self) -> str:
        return self.path.name


def _natural_key(path: Path):
    """Sort 'part-2' before 'part-10'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path.name)]


def audio_files(path: Path) -> list[Path]:
    """The audio of one meeting: the file itself, or a folder's audio files in natural order."""
    if path.is_file():
        return [path] if path.suffix.lower() in AUDIO_EXTENSIONS else []
    return sorted((f for f in path.iterdir()
                   if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS and not f.name.startswith(".")),
                  key=_natural_key)


def find_meetings(roots: list[Path]) -> list[Meeting]:
    """Every meeting in the given recordings folders, newest first."""
    found = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.iterdir():
            if path.name.startswith(".") or path in found:
                continue
            if files := audio_files(path) if path.is_dir() or path.suffix.lower() in AUDIO_EXTENSIONS else []:
                found[path] = Meeting(path, max(f.stat().st_mtime for f in files))
    return sorted(found.values(), key=lambda m: m.recorded, reverse=True)
