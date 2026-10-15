"""Removing what a summary claims but the transcript cannot back up.

Speech recognition spells names freely ("Neon DB" for NeonDB), so names only have to roughly match something said,
while standalone numbers (salaries, durations, counts) have to appear exactly. For an interview the Signal is
derived from the depth-check verdicts, so it can never contradict them.
"""
import difflib
import re
from functools import cache

VERDICT_RE = re.compile(r"\*\*(Clear|Vague|Couldn't explain)\*\*")
NOT_DISCUSSED = re.compile(r"\b(not (discussed|mentioned|stated|specified|provided|recorded)"
                           r"|none (stated|mentioned|recorded|made|identified|noted|raised)|n/a)\b"
                           r"|^\W*no\b[^.]*\b(were|was) (mentioned|stated|discussed|provided|given|shared)\b", re.I)
SECTION_RE = re.compile(r"^##\s*([^\n]+)\n(.*?)(?=^##\s|\Z)", re.M | re.S)
ALWAYS_OK = {"sar", "usd", "ai", "api", "apis", "ui", "ux", "cv", "llm", "llms", "sql", "ceo", "cto", "hr", "ios",
             "android", "english", "arabic", "speaker", "clear", "vague", "none", "overview"}


@cache
def english_words() -> frozenset[str]:
    """Ordinary words are never treated as names. Loaded on first use, not at import."""
    try:
        with open("/usr/share/dict/words") as f:
            return frozenset(w.strip().lower() for w in f)
    except OSError:
        return frozenset()


class Source:
    """What a transcript contains: its exact numbers, and its words (plus joined pairs) to match names against."""

    def __init__(self, transcript: str):
        low = transcript.lower()
        self.numbers = set(re.findall(r"\d+", low.replace(",", "")))
        words = re.findall(r"[a-z؀-ۿ0-9]+", low)
        self.vocab = set(words) | {a + b for a, b in zip(words, words[1:])}
        self.by_first_letter = {}
        for v in self.vocab:
            self.by_first_letter.setdefault(v[:1], []).append(v)

    def has_name(self, token: str) -> bool:
        for part in token.split("-"):
            t = re.sub(r"[^a-z0-9]", "", part.lower())
            if not t or t in ALWAYS_OK or t in english_words() or t in self.vocab or t.rstrip("s") in self.vocab:
                continue
            if not difflib.get_close_matches(t, self.by_first_letter.get(t[:1], []), 1, 0.75):
                return False
        return True

    def supports(self, line: str, names: bool = True) -> bool:
        """Standalone numbers must appear exactly; capitalised names (after the first word) only roughly."""
        body = re.sub(r"^\s*[-*•]\s*", "", VERDICT_RE.sub("", line))
        if any(n.replace(",", "").rstrip(".") not in self.numbers
               for n in re.findall(r"(?<![\w.])\d[\d,.]*(?![\w])", body)):
            return False
        return not names or all(self.has_name(t) for t in re.findall(r"\b[A-Z][a-zA-Z'-]{2,}", body.split(" ", 1)[-1]))


def check_summary(draft: str, transcript: str) -> tuple[str, list[str]]:
    """The draft without unsupported lines and empty sections, plus the lines that were dropped."""
    source, interview = Source(transcript), "## Depth check" in draft
    sections, dropped, verdicts = [], [], []
    for m in SECTION_RE.finditer(draft):
        title, body = m[1].strip(), m[2].strip()
        is_depth = title.lower().startswith("depth")
        if interview and title.lower().startswith("next step"):
            continue  # every candidate hears the hiring process, rejected ones included
        if NOT_DISCUSSED.search(body) and len(body.split()) < 25:
            continue
        kept = []
        for line in filter(str.strip, body.splitlines()):
            if line.lstrip().startswith(("-", "*", "•")):
                ok = source.supports(line, names=not is_depth)  # a dropped depth bullet would skew the Signal
                (kept if ok else dropped).append(line)
            else:  # prose: judge sentence by sentence
                sentences = re.split(r"(?<=[a-z0-9)][.!?])\s+(?=[A-Z])", line)
                good = [s for s in sentences if source.supports(s)]
                dropped += [s for s in sentences if s not in good]
                if good:
                    kept.append(" ".join(good))
        if is_depth:
            verdicts = VERDICT_RE.findall("\n".join(kept))
        if kept:
            sections.append((title, "\n".join(kept)))
    if interview and verdicts:
        sections = _with_signal(sections, verdicts)
    return "\n\n".join(f"## {t}\n{b}" for t, b in sections), dropped


def _with_signal(sections: list[tuple[str, str]], verdicts: list[str]) -> list[tuple[str, str]]:
    clear = verdicts.count("Clear")
    if not clear:  # strengths can only come from answers that held up
        sections = [(t, b) for t, b in sections if t.lower() != "strengths"]
        at = next((i + 1 for i, (t, _) in enumerate(sections) if t.lower().startswith("depth")), len(sections))
        sections.insert(at, ("Strengths", "None demonstrated."))
    signal = "Strong" if clear > len(verdicts) / 2 else "Weak" if clear < len(verdicts) / 2 else "Mixed"
    at = 1 if sections and sections[0][0].lower() == "overview" else 0
    sections.insert(at, ("Signal", f"**{signal}**: {clear} of {len(verdicts)} probed topics answered clearly."))
    return sections
