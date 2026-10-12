"""The files debrief writes next to a meeting's audio, and how to read them back.

A folder gets transcript.md, transcript.srt and summary.md; a single audio file gets <stem>.transcript.md and so on.
Both Markdown files start with a '# Title' line; the transcript adds an italic meta line and the summary ends with an
italic footer after a '---' rule. `body_of` strips all three, so readers never depend on that layout.
"""
import re
from pathlib import Path

TRANSCRIPT, TRANSCRIPT_SRT, SUMMARY = "transcript.md", "transcript.srt", "summary.md"
_FOOTER_RE = re.compile(r"\n+---\n_([^\n]*)_\s*\Z")


def output_file(target: Path, name: str) -> Path:
    return target / name if target.is_dir() else target.parent / f"{target.stem}.{name}"


def write_transcript(target: Path, body: str, meta: str) -> None:
    output_file(target, TRANSCRIPT).write_text(f"# Transcript: {target.stem}\n\n_{meta}_\n\n{body}\n")


def write_summary(target: Path, summary: str, footer: str) -> None:
    output_file(target, SUMMARY).write_text(f"# Summary: {target.stem}\n\n{summary}\n\n---\n_{footer}_\n")


def body_of(markdown: str) -> str:
    """A document without its title, meta line and footer."""
    return re.sub(r"\A# .*\n+(_.*_\n+)?", "", _FOOTER_RE.sub("", markdown)).strip()


def footer_of(markdown: str) -> str:
    """The summary's footer text (which model wrote it, and how many unsupported lines were removed)."""
    m = _FOOTER_RE.search(markdown)
    return m[1] if m else ""


def read(target: Path, name: str) -> str:
    """One of a meeting's documents, or '' if it doesn't exist yet."""
    path = output_file(target, name)
    return path.read_text() if path.exists() else ""
