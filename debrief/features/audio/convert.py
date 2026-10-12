"""Turning a meeting's audio files into one 16 kHz mono recording.

Recorders split long meetings into chunks (audio-000.caf, audio-001.caf, …) and some record each side of a call as
its own track (applicationAudio-000.caf + microphone-000.caf). Chunks of one track are joined in order; tracks of
about the same length were recorded at the same time, so they are overlaid instead.
"""
import re
import subprocess
import wave
from array import array
from pathlib import Path

from debrief.core.errors import DebriefError

SAMPLE_RATE = 16000  # what Whisper expects
_SAMPLE_BYTES = 2  # 16-bit


def tracks(files: list[Path]) -> dict[str, list[Path]]:
    """Group chunk files by track name: the part before the trailing chunk number."""
    groups = {}
    for f in files:
        groups.setdefault(re.sub(r"[-_ ]*\d+$", "", f.stem) or f.stem, []).append(f)
    return groups


def overlaid(lengths: list[float]) -> bool:
    """Tracks of about the same length were recorded at the same time; anything else is sequential."""
    return len(lengths) > 1 and min(lengths) >= 0.9 * max(lengths)


def duration_seconds(files: list[Path]) -> float:
    """How long the meeting is, read from file headers with afinfo (no decoding)."""
    def seconds(f):
        try:
            out = subprocess.run(["afinfo", str(f.resolve())], capture_output=True, text=True, timeout=60).stdout
        except subprocess.TimeoutExpired:
            return 0.0
        m = re.search(r"estimated duration: ([\d.]+)", out)
        return float(m[1]) if m else 0.0
    per_track = [sum(map(seconds, group)) for group in tracks(files).values()]
    return max(per_track) if overlaid(per_track) else sum(per_track)


def decode(file: Path, scratch: Path) -> bytes:
    """One audio file as 16 kHz mono 16-bit PCM, via macOS's built-in afconvert."""
    out = scratch / "part.wav"
    r = subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{SAMPLE_RATE}", "-c", "1", str(file.resolve()), str(out)],
                       capture_output=True, text=True)
    if r.returncode:
        raise DebriefError(f"afconvert could not read {file.name}: {r.stderr.strip()}")
    with wave.open(str(out)) as w:
        return w.readframes(w.getnframes())


def mix(a: bytes, b: bytes) -> bytes:
    """Overlay two PCM tracks by summing samples (clipped), padding the shorter one with silence.
    A sum, not an average, keeps each side at full volume while the other is quiet, which is most of a call."""
    # ponytail: pure-Python loop, a few seconds per hour of audio; numpy if that ever matters
    n = max(len(a), len(b)) // _SAMPLE_BYTES
    x = array("h", bytes(a).ljust(_SAMPLE_BYTES * n, b"\0"))
    y = array("h", bytes(b).ljust(_SAMPLE_BYTES * n, b"\0"))
    # a generator, not a list: a list of an hour's samples as Python ints would take gigabytes
    return array("h", (max(-32768, min(32767, p + q)) for p, q in zip(x, y))).tobytes()


def write_wav(path: Path, pcm: bytes) -> float:
    """Write mono PCM as a WAV file; returns its length in seconds."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(_SAMPLE_BYTES)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return len(pcm) / _SAMPLE_BYTES / SAMPLE_RATE
