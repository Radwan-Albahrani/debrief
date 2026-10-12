"""Building the speaker-labelled transcript from Whisper's segments and the diarization turns."""
import re

from debrief.core.formatting import clock

UNCLEAR = "_[unclear audio]_"


def reliable(segment: dict) -> bool:
    """Whisper's own failure signals: every temperature fallback used up, or looping (repetitive) output.
    Such segments are hallucinations on unclear audio, so they are replaced instead of printed."""
    return segment.get("temperature", 0) < 1.0 and segment.get("compressionRatio", 0) <= 2.4


def speaker_at(turns: list[tuple[float, float, str]], start: float, end: float) -> str:
    """The speaker whose turn overlaps [start, end] the most (a negative overlap is a gap, so else the nearest)."""
    return max(turns, key=lambda t: min(end, t[1]) - max(start, t[0]))[2]


def build_transcript(turns: list[tuple[float, float, str]], segments: list[dict]) -> str:
    """Markdown blocks of consecutive speech by one speaker; without speakers, one block per minute."""
    blocks = []  # [start, speaker, [text, ...]]
    for seg in sorted(segments, key=lambda s: s["start"]):
        text = re.sub(r"<\|[^|]*\|>", "", seg["text"]).strip()
        if not text:
            continue
        text = text if reliable(seg) else UNCLEAR
        speaker = speaker_at(turns, seg["start"], seg["end"]) if turns else None
        same_block = blocks and blocks[-1][1] == speaker and (turns or seg["start"] - blocks[-1][0] <= 60)
        if not same_block:
            blocks.append([seg["start"], speaker, []])
        if not (text == UNCLEAR and blocks[-1][2][-1:] == [UNCLEAR]):
            blocks[-1][2].append(text)
    return "\n\n".join((f"**Speaker {who}** `{clock(start)}`" if who else f"`{clock(start)}`") + "\n" + " ".join(texts)
                       for start, who, texts in blocks)


def speaker_count(transcript: str) -> int:
    return len(set(re.findall(r"^\*\*Speaker (\w+)\*\*", transcript, flags=re.M)))
