"""Small display helpers shared by every surface."""
from datetime import date, datetime
from pathlib import Path


def clock(seconds: float) -> str:
    """65 -> '1:05', 3725 -> '1:02:05'."""
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def filesize(n: float) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def tilde(path) -> str:
    return str(path).replace(str(Path.home()), "~", 1)


def when(timestamp: float) -> str:
    """'today 14:03', 'yesterday 09:30' or 'Oct 2 09:30'."""
    d = datetime.fromtimestamp(timestamp)
    days = (date.today() - d.date()).days
    day = "today" if days == 0 else "yesterday" if days == 1 else f"{d:%b} {d.day}"
    return f"{day} {d:%H:%M}"


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' * (n != 1)}"
