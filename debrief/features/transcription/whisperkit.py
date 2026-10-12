"""Running whisperkit-cli. Everything about its command line and its output format stays in this module."""
import json
import os
import pty
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from debrief.core.config import MODELS_DIR, SPEAKER_MODEL_DIR, WHISPER_MODELS, WHISPERKIT_DIR
from debrief.core.errors import DebriefError

PROGRESS_RE = re.compile(r"(\d+)% \| Elapsed Time: [\d.]+ s \| Remaining: ([\d.]+) s")

Turn = tuple[float, float, str]  # (start, end, speaker label)


@dataclass
class WhisperKitResult:
    segments: list[dict]  # Whisper's segments, with timing and its own confidence signals
    turns: list[Turn]  # who spoke when; empty when diarization was off or failed
    srt: Path  # subtitles, ready to copy next to the audio
    diarization_errors: list[str]


def model_path(whisper: str) -> Path:
    return WHISPERKIT_DIR / WHISPER_MODELS[whisper][0]


def ready(whisper: str) -> bool:
    """The model is downloaded and whisperkit-cli is installed."""
    return model_path(whisper).exists() and shutil.which("whisperkit-cli") is not None


def run(whisper: str, lang: str, speakers: str, audio: Path, report_dir: Path, *,
        on_start: Callable[[subprocess.Popen], None] = lambda proc: None,
        on_progress: Callable[[float, float], None] = lambda fraction, remaining: None,
        on_line: Callable[[str], None] = lambda line: None,
        cancelled: Callable[[], bool] = lambda: False) -> WhisperKitResult:
    """Transcribe `audio` (and, unless speakers is 'off', label who spoke when).

    Reports the process as it starts, progress as (fraction done, seconds left), and every other output line.
    Any exception raised by a callback stops the process and propagates."""
    proc, fd = _spawn(_command(whisper, lang, speakers, audio, report_dir))
    on_start(proc)
    lines = []
    try:
        for line in _output_lines(fd):
            if m := PROGRESS_RE.search(line):
                on_progress(int(m[1]) / 100, float(m[2]))
            elif line.strip():
                lines.append(line)
                on_line(line)
        code = proc.wait()
    finally:
        os.close(fd)
        stop(proc)
    if code and not cancelled():
        raise DebriefError("whisperkit-cli failed:\n" + "\n".join(lines[-15:]))
    report = json.loads((report_dir / f"{audio.stem}.json").read_text())
    return WhisperKitResult(segments=report["segments"],
                            turns=[t for t in map(parse_speaker, lines) if t],
                            srt=report_dir / f"{audio.stem}.srt",
                            diarization_errors=[l for l in lines if "diarization" in l.lower() and "error" in l.lower()])


def stop(proc: subprocess.Popen) -> None:
    """Terminate the process and reap it, killing it if it ignores the request."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def parse_speaker(line: str) -> Turn | None:
    """`SPEAKER <file> 1 <start> <dur> <text...> <NA> <label> <NA> <NA>` -> (start, end, label)."""
    t = line.split()
    if len(t) < 10 or t[0] != "SPEAKER":
        return None
    return float(t[3]), float(t[3]) + float(t[4]), t[-3]


def _command(whisper: str, lang: str, speakers: str, audio: Path, report_dir: Path) -> list[str]:
    cmd = ["whisperkit-cli", "transcribe", "--verbose", "--model-path", str(model_path(whisper)),
           "--download-tokenizer-path", str(MODELS_DIR), "--audio-path", str(audio),
           "--report", "--report-path", str(report_dir)]
    if lang != "auto":
        cmd += ["--language", lang]
    if speakers != "off":
        cmd += ["--diarization", "--diarization-model-path", str(SPEAKER_MODEL_DIR)]
        if speakers != "auto":
            cmd += ["--diarization-num-speakers", speakers]
    return cmd


def _spawn(cmd: list[str]) -> tuple[subprocess.Popen, int]:
    """Start the command on a pseudo-terminal: whisperkit-cli block-buffers its progress bar when writing to a pipe.
    Returns the process and the terminal's file descriptor, which the caller must close."""
    master, slave = pty.openpty()
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave)
    os.close(slave)
    return proc, master


def _output_lines(fd: int) -> Iterator[str]:
    """Each line of terminal output, splitting on the carriage returns and line clears a progress bar redraws with."""
    pending = ""
    while True:
        try:
            chunk = os.read(fd, 65536).decode(errors="replace")
        except OSError:  # the process closed its end
            break
        if not chunk:
            break
        *lines, pending = re.split(r"\r|\n|\x1b\[K", pending + chunk)
        yield from lines
    if pending:
        yield pending
