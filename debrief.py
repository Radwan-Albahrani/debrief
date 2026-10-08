#!/usr/bin/env python3
"""debrief — local meeting transcripts (WhisperKit + speaker labels) and summaries (Ollama).

  debrief               full-screen app
  debrief setup         download the models (resumable)
  debrief <folder>      one-shot, no UI

A folder is ONE meeting split into chunks, joined in natural sort order. Simultaneous tracks
(e.g. applicationAudio-000.caf + microphone-000.caf) are overlaid into one timeline.
Writes transcript.md, transcript.srt and summary.md next to the audio.
"""
import argparse, json, os, pty, re, shutil, subprocess, sys, tempfile, threading, time, urllib.request, wave
from array import array
from collections import deque
from datetime import date, datetime
from pathlib import Path

from rich.console import Console, Group
from rich.markdown import Markdown as RichMarkdown
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

HF = Path(os.environ.get("DEBRIEF_MODELS", "~/Documents/huggingface")).expanduser()
WHISPERKIT = HF / "models/argmaxinc/whisperkit-coreml"
WHISPER_MODELS = {"large-v3": "openai_whisper-large-v3", "turbo": "openai_whisper-large-v3-v20240930_turbo"}
WHISPER_LABELS = {"large-v3": "large-v3 (most accurate)", "turbo": "large-v3 turbo (faster)"}
SPEAKERS = HF / "models/argmaxinc/speakerkit-coreml"
TOKENIZER = HF / "models/openai/whisper-large-v3"
STATE = Path(os.environ.get("DEBRIEF_HOME", "~/.local/share/debrief")).expanduser() / "state.json"
DEFAULT_LIBRARY = "~/Documents/Meetings"
OLLAMA = "http://localhost:11434"
PREFERRED_MODELS = ["qwen3.5:9b", "qwen3-vl:8b-instruct", "qwen3-vl:8b"]
AUDIO_EXT = {".caf", ".wav", ".m4a", ".mp3", ".mp4", ".aac", ".aiff", ".aif", ".flac"}
LANGS = {"auto": "Auto-detect", "en": "English", "ar": "Arabic"}
SPEAKER_CHOICES = {"auto": "Auto-detect", "2": "2 people", "3": "3 people", "4": "4 people", "5": "5 people", "off": "Off"}
CONTEXTS = {
    "General meeting": "",
    "Job interview": "This is a job interview; the summary will be posted under the candidate in Slack. Replace "
                     "'Key points' with '## Strengths' and '## Concerns' (3-5 bullets each, each backed by something "
                     "said). Include stated logistics (availability, "
                     "salary expectation, start date) as one bullet under Next steps.",
    "1:1": "This is a one-on-one. Highlight feedback given, concerns raised and agreed follow-ups.",
    "Client call": "This is a client call. Highlight the client's needs, objections, commitments and follow-ups.",
}
SUMMARY_SYSTEM = """You summarize meeting transcripts produced by local speech recognition.
Meetings may mix languages (e.g. Arabic and English): expect misheard names and terms, and speaker labels
(Speaker A, B, ...) that are occasionally wrong. Use only what the transcript says; never invent.
Do not state outcomes, decisions or judgements (e.g. "passed", "self-taught", "below market") that
nobody in the meeting actually said. Refer to people by name or role, not by gendered pronouns.
The summary gets pasted into Slack: 300-450 words, scannable, specific, no filler.
Write in English, in Markdown, with exactly these sections:
## Overview
(2-3 sentences: who met, about what, the gist)
## Key points
(5-8 bullets, each one or two lines with the concrete detail that was said)
## Next steps
(1-3 bullets of what was agreed; omit the section if nothing was)
Names: use a person, company or product name only if it is clearly said and makes sense in context.
Never build a name out of garbled words; write "a previous employer", "the interviewer" etc. instead.
If a person, place or organization name looks misheard (odd spelling, not a real name), drop it:
"a university student", not a guessed university. Silently fix obvious misspellings of well-known
technologies (write Proxmox, not "Broxmox"), without mentioning the fix. Never compare numbers like
salary to the market; just report them.
If a passage is garbled, leave it out. Do not speculate about what it meant.
Passages marked [unclear audio] could not be transcribed: skip them.
No preamble, no closing remarks, no participant list."""
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class Fail(Exception):
    pass


class Cancelled(Exception):
    pass


# ── helpers ──────────────────────────────────────────────────────────────────

def fmt(s):
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def human(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def natural(p):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def audio_files(p):
    if p.is_file():
        return [p] if p.suffix.lower() in AUDIO_EXT else []
    return sorted((f for f in p.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXT
                   and not f.name.startswith(".")), key=natural)


def tracks(files):
    """Group chunk files by track: `applicationAudio-000.caf` and `microphone-000.caf` are two tracks."""
    groups = {}
    for f in files:
        groups.setdefault(re.sub(r"[-_ ]*\d+$", "", f.stem) or f.stem, []).append(f)
    return groups


def overlaid(lengths):
    """Tracks of about the same length were recorded at the same time; anything else is sequential."""
    return len(lengths) > 1 and min(lengths) >= 0.9 * max(lengths)


def audio_seconds(files):
    def secs(f):
        m = re.search(r"estimated duration: ([\d.]+)", subprocess.run(["afinfo", str(f)], capture_output=True, text=True).stdout)
        return float(m[1]) if m else 0.0
    per_track = [sum(map(secs, group)) for group in tracks(files).values()]
    return max(per_track) if overlaid(per_track) else sum(per_track)


def mix(a, b):
    """Overlay two 16-bit mono PCM tracks by summing samples (clipped), padding the shorter one with silence.
    A sum, not an average, keeps each side at full volume while the other is quiet, which is most of a call."""
    # ponytail: pure-Python loop, ~2 s per 48 min of audio; numpy if that ever matters
    n = max(len(a), len(b)) // 2
    x, y = array("h", bytes(a).ljust(2 * n, b"\0")), array("h", bytes(b).ljust(2 * n, b"\0"))
    return array("h", [max(-32768, min(32767, p + q)) for p, q in zip(x, y)]).tobytes()


def whisper_path(name):
    return WHISPERKIT / WHISPER_MODELS[name]


def whisper_ready(name):
    return whisper_path(name).exists() and shutil.which("whisperkit-cli") is not None


def dir_size(p):
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


def outputs(target):
    """(outdir, filename prefix) — a folder gets transcript.md, a file gets <stem>.transcript.md."""
    return (target.parent, f"{target.stem}.") if target.is_file() else (target, "")


def load_state():
    old = Path.home() / ".local/share/transcribe/state.json"  # pre-rename location
    if not STATE.exists() and old.exists():
        STATE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(old, STATE)
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))


def ollama_models():
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=3) as r:
            return [m["name"] for m in json.load(r)["models"]]
    except OSError:
        return None


def default_model(models):
    return next((m for m in PREFERRED_MODELS if m in models), models[0] if models else None)


def libraries(state):
    return [Path(p).expanduser() for p in state.get("libraries") or [DEFAULT_LIBRARY]]


def tilde(p):
    return str(p).replace(str(Path.home()), "~", 1)


def recorded_at(p):
    return max(f.stat().st_mtime for f in audio_files(p))


def meetings(state):
    """Every subfolder (or loose audio file) of every recordings folder that holds audio, newest first."""
    found = {}
    for root in libraries(state):
        if root.is_dir():
            for p in root.iterdir():
                if not p.name.startswith(".") and (p.is_dir() or p.suffix.lower() in AUDIO_EXT) and audio_files(p):
                    found[p] = None
    return sorted(found, key=recorded_at, reverse=True)


def when(ts):
    d = datetime.fromtimestamp(ts)
    days = (date.today() - d.date()).days
    return f"{'today' if days == 0 else 'yesterday' if days == 1 else f'{d:%b %-d}'} {d:%H:%M}"


def to_slack(md):
    """Markdown summary → Slack mrkdwn: headings and **bold** become *bold*, bullets become •."""
    md = re.sub(r"\A# .*\n+", "", md.split("\n---\n")[0])
    lines = []
    for line in md.strip().splitlines():
        line = line.rstrip()
        if m := re.match(r"#+\s*(.+)", line):
            line = f"*{m[1].strip()}*"
        else:
            line = re.sub(r"^(\s*)[-*]\s+", r"\1• ", line)
            line = re.sub(r"\*\*(.+?)\*\*", r"*\1*", line)
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def copy(text):
    subprocess.run(["pbcopy"], input=text, text=True, check=True)


def speaker_count(transcript):
    return len(set(re.findall(r"^\*\*Speaker (\w+)\*\*", transcript, flags=re.M)))


# ── pipeline ─────────────────────────────────────────────────────────────────

PROGRESS = re.compile(r"(\d+)% \| Elapsed Time: [\d.]+ s \| Remaining: ([\d.]+) s")


UNCLEAR = "_[unclear audio]_"


def parse_speaker(line):
    """`SPEAKER <file> 1 <start> <dur> <text...> <NA> <label> <NA> <NA>` -> (start, end, label)."""
    t = line.split()
    if len(t) < 10 or t[0] != "SPEAKER":
        return None
    return float(t[3]), float(t[3]) + float(t[4]), t[-3]


def reliable(seg):
    """Whisper's own failure signals: every temperature fallback used up, or looping (repetitive) output."""
    return seg.get("temperature", 0) < 1.0 and seg.get("compressionRatio", 0) <= 2.4


def speaker_at(turns, a, b):
    """Label of the speaker turn overlapping [a, b] the most (a negative overlap is a gap, so else the nearest)."""
    return max(turns, key=lambda t: min(b, t[1]) - max(a, t[0]))[2]


def build_transcript(turns, segments):
    """Text from whisper's segments (unreliable ones become [unclear audio]), speakers from the diarization turns."""
    blocks = []
    for s in sorted(segments, key=lambda s: s["start"]):
        text = re.sub(r"<\|[^|]*\|>", "", s["text"]).strip()
        if not text:
            continue
        text = text if reliable(s) else UNCLEAR
        label = speaker_at(turns, s["start"], s["end"]) if turns else None
        if not (blocks and blocks[-1][1] == label and (turns or s["start"] - blocks[-1][0] <= 60)):
            blocks.append([s["start"], label, []])
        if not (text == UNCLEAR and blocks[-1][2][-1:] == [UNCLEAR]):
            blocks[-1][2].append(text)
    return "\n\n".join((f"**Speaker {lbl}** `{fmt(start)}`" if lbl else f"`{fmt(start)}`") + "\n" + " ".join(txt)
                       for start, lbl, txt in blocks)


class Job:
    """Shared state between the pipeline thread and whatever renders it."""
    STEPS = ("Convert", "Transcribe", "Speakers", "Summary")

    def __init__(self, target, files, opts, echo=False):
        self.target, self.files, self.opts, self.echo = target, files, opts, echo
        self.outdir, self.prefix = outputs(target)
        self.steps = {s: {"state": "wait", "t0": None, "t1": None, "pct": None, "note": ""} for s in self.STEPS}
        if opts["speakers"] == "off" or opts.get("summary_only"):
            self.steps["Speakers"]["state"] = "skip"
        if opts.get("summary_only"):
            self.steps["Convert"]["state"] = self.steps["Transcribe"]["state"] = "skip"
        if not opts["model"]:
            self.steps["Summary"]["state"] = "skip"
        self.t0, self.t1, self.audio = time.time(), None, 0.0
        self.log = deque(maxlen=4)
        self.proc, self.summary, self.error = None, "", None
        self.cancelled = threading.Event()

    @property
    def running(self):
        return self.t1 is None

    def start(self, step, note=""):
        self.steps[step].update(state="run", t0=time.time(), note=note)

    def update(self, step, **kw):
        if self.cancelled.is_set():
            raise Cancelled
        self.steps[step].update(kw)

    def finish(self, step, note=None):
        st = self.steps[step]
        st.update(state="done", t1=time.time(), pct=1.0 if st["pct"] is not None else None)
        if note is not None:
            st["note"] = note
        if self.echo:
            print(f"✓ {step:<11} {fmt(st['t1'] - st['t0']):>6}  {st['note']}", flush=True)

    def cancel(self):
        self.cancelled.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def run(self):
        try:
            if self.opts.get("summary_only"):
                text = (self.outdir / f"{self.prefix}transcript.md").read_text()
                return self._summarize(text.split("\n\n", 2)[-1])
            with tempfile.TemporaryDirectory(prefix="transcribe-") as d:
                tmp = Path(d)
                wav = self._convert(tmp)
                turns = self._transcribe(wav, tmp)
                body = build_transcript(turns, json.loads((tmp / "meeting.json").read_text())["segments"])
                shutil.copyfile(tmp / "meeting.srt", self.outdir / f"{self.prefix}transcript.srt")
            (self.outdir / f"{self.prefix}transcript.md").write_text(
                f"# Transcript: {self.target.stem}\n\n_{date.today()} · {fmt(self.audio)} · whisper {self.opts['whisper']} (local)_\n\n{body}\n")
            if self.opts["model"]:
                self._summarize(body)
        except Cancelled:
            self.error = "cancelled"
        except Fail as e:
            self.error = str(e)
        except Exception as e:  # surface anything unexpected in the UI instead of dying silently
            self.error = f"{type(e).__name__}: {e}"
        finally:
            for st in self.steps.values():
                if st["state"] == "run":
                    st.update(state="fail", t1=time.time())
            self.t1 = time.time()

    def _convert(self, tmp):
        self.start("Convert")
        pcm, done, part = {}, 0, tmp / "part.wav"
        for name, files in tracks(self.files).items():
            pcm[name] = bytearray()
            for f in files:
                self.update("Convert", pct=done / len(self.files), note=f"{f.name}  ({done + 1}/{len(self.files)})")
                r = subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(f), str(part)],
                                   capture_output=True, text=True)
                if r.returncode:
                    raise Fail(f"afconvert could not read {f.name}: {r.stderr.strip()}")
                with wave.open(str(part)) as w:
                    pcm[name] += w.readframes(w.getnframes())
                done += 1
        if overlaid([len(b) for b in pcm.values()]):
            self.update("Convert", note=f"overlaying {' + '.join(pcm)}…")
            audio = pcm.popitem()[1]
            for other in pcm.values():
                audio = mix(audio, other)
            how = f"{len(pcm) + 1} simultaneous tracks overlaid"
        else:
            audio, how = b"".join(pcm.values()), f"{len(self.files)} file(s)"
        out = tmp / "meeting.wav"
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
            w.writeframes(audio)
        self.audio = len(audio) / 2 / 16000
        self.finish("Convert", f"{how} → {fmt(self.audio)} of 16 kHz mono audio")
        return out

    def _transcribe(self, wav, tmp):
        cmd = ["whisperkit-cli", "transcribe", "--verbose", "--model-path", str(whisper_path(self.opts["whisper"])),
               "--download-tokenizer-path", str(HF), "--audio-path", str(wav), "--report", "--report-path", str(tmp)]
        if self.opts["lang"] != "auto":
            cmd += ["--language", self.opts["lang"]]
        speakers = self.opts["speakers"] != "off"
        if speakers:
            cmd += ["--diarization", "--diarization-model-path", str(SPEAKERS)]
            if self.opts["speakers"] != "auto":
                cmd += ["--diarization-num-speakers", self.opts["speakers"]]
        self.start("Transcribe", f"loading whisper {self.opts['whisper']} onto the Neural Engine…")
        # a pty, because whisperkit block-buffers its progress bar when writing to a pipe
        master, slave = pty.openpty()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave)
        os.close(slave)
        out, buf, first = [], "", None
        try:
            while True:
                try:
                    chunk = os.read(master, 65536).decode(errors="replace")
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                *parts, buf = re.split(r"\r|\n|\x1b\[K", buf)
                for p in parts:
                    if m := PROGRESS.search(p):
                        pct = int(m[1]) / 100
                        first = first or time.time()
                        done = pct * self.audio
                        speed = done / max(time.time() - first, 0.1)
                        self.update("Transcribe", pct=pct, note=f"{fmt(done)} / {fmt(self.audio)} of audio · "
                                                                 f"{speed:.1f}× realtime · ~{fmt(float(m[2]))} left")
                        if pct >= 1 and speakers and self.steps["Speakers"]["state"] == "wait":
                            self.finish("Transcribe", f"{fmt(self.audio)} of audio · {self.audio / (time.time() - self.steps['Transcribe']['t0']):.1f}× realtime")
                            self.start("Speakers", "pyannote: segmenting voices and clustering who said what…")
                    elif p.strip():
                        out.append(p)
                        if not p.startswith("SPEAKER") and len(p) < 200:
                            self.log.append(p.strip())
            if self.proc.wait() and not self.cancelled.is_set():
                raise Fail("whisperkit-cli failed:\n" + "\n".join(out[-15:]))
            if self.cancelled.is_set():
                raise Cancelled
        finally:
            os.close(master)
            if self.proc.poll() is None:
                self.proc.terminate()
        if self.steps["Transcribe"]["state"] == "run":
            self.finish("Transcribe", f"{fmt(self.audio)} of audio")
        turns = [s for s in map(parse_speaker, out + [buf]) if s]
        if speakers:
            if self.steps["Speakers"]["state"] == "wait":
                self.start("Speakers")
            if turns:
                self.finish("Speakers", f"{len({t[2] for t in turns})} speakers · {len(turns)} turns")
            else:
                errs = [l for l in out if "diarization" in l.lower() and "error" in l.lower()]
                self.finish("Speakers", "no speaker labels" + (f" ({errs[0][:80]})" if errs else ""))
        return turns

    def _summarize(self, transcript):
        model, context = self.opts["model"], self.opts["context"]
        est = len(transcript) // 3
        ctx = min(131072, max(8192, (est + 6000) // 1024 * 1024))
        user = (f"Context: {context}\n\n" if context else "") + "Transcript:\n\n" + transcript
        body = json.dumps({"model": model, "stream": True, "think": False, "options": {"num_ctx": ctx, "temperature": 0.3,
                           # ponytail: hard cap; small models loop on long transcripts without it
                           "num_predict": 1100, "repeat_penalty": 1.15},
                           "messages": [{"role": "system", "content": SUMMARY_SYSTEM}, {"role": "user", "content": user}]})
        req = urllib.request.Request(f"{OLLAMA}/api/chat", body.encode(), {"Content-Type": "application/json"})
        self.start("Summary", f"{model}: loading and reading ~{est:,} tokens of transcript…")
        n, first, stats = 0, None, {}
        try:
            with urllib.request.urlopen(req, timeout=1800) as r:
                for line in r:
                    msg = json.loads(line)
                    if "error" in msg:
                        raise Fail(f"Ollama: {msg['error']}")
                    if piece := msg.get("message", {}).get("content", ""):
                        if first is None:
                            first = time.time()
                            self.log.append(f"{model} read the transcript in {fmt(first - self.steps['Summary']['t0'])}")
                        n += 1
                        self.summary += piece
                        self.update("Summary", note=f"{model} · writing · {n} tokens · {n / max(time.time() - first, 0.1):.0f} tok/s")
                    if msg.get("done"):
                        stats = msg
        except OSError as e:
            raise Fail(f"Ollama request failed: {e}")
        self.summary = re.sub(r"<think>.*?</think>", "", self.summary, flags=re.S).strip()
        (self.outdir / f"{self.prefix}summary.md").write_text(
            f"# Summary: {self.target.stem}\n\n{self.summary}\n\n---\n_Generated locally by {model} "
            f"from {self.prefix}transcript.md._\n")
        self.finish("Summary", f"{model} · {stats.get('prompt_eval_count', '?')} tokens in, "
                               f"{stats.get('eval_count', n)} out")


def proc_stats(pid):
    r = subprocess.run(["ps", "-o", "%cpu=,rss=", "-p", str(pid)], capture_output=True, text=True)
    parts = r.stdout.split()
    return (float(parts[0]), int(parts[1]) * 1024) if len(parts) == 2 else None


def render_job(job, stats=None):
    now = time.time()
    frame = SPIN[int(now * 10) % len(SPIN)]
    grid = Table.grid(padding=(0, 2))
    for _ in range(5):
        grid.add_column()
    icons = {"wait": "[dim]·", "run": f"[bold cyan]{frame}", "done": "[green]✓", "fail": "[red]✗", "skip": "[dim]–"}
    for name, st in job.steps.items():
        el = fmt((st["t1"] or now) - st["t0"]) if st["t0"] else ""
        if st["state"] == "skip":
            bar, note = Text(""), Text("off", style="dim")
        else:
            bar = ProgressBar(total=1, completed=st["pct"] or (1 if st["state"] == "done" else 0), width=26,
                              pulse=st["pct"] is None and st["state"] == "run",
                              complete_style="cyan", finished_style="green", style="grey23")
            note = Text(st["note"] or ("waiting" if st["state"] == "wait" else ""), style="dim" if st["state"] != "run" else "")
        name_style = "bold" if st["state"] == "run" else ("dim" if st["state"] in ("wait", "skip") else "")
        grid.add_row(icons[st["state"]], Text(name, style=name_style), bar, Text(el, style="dim"), note)

    total = fmt((job.t1 or now) - job.t0)
    if job.running:
        head = Text.assemble(("● ", "cyan"), ("Running ", "bold"), (f"{total} elapsed", "dim"))
    elif job.error:
        head = Text.assemble(("✗ ", "red"), (job.error.splitlines()[0][:120], "bold red"))
    else:
        head = Text.assemble(("✓ ", "green"), (f"Done in {total}", "bold"),
                             (f" · {fmt(job.audio)} of audio · {job.audio / max(job.t1 - job.t0, 1):.1f}× realtime"
                              if job.audio else "", "dim"))
    parts = [head, "", grid]
    if stats:
        cpu, rss = stats
        parts += ["", Text.assemble(("whisperkit  ", "dim"), f"CPU {cpu:.0f}%  ·  RAM {human(rss)}  ·  ", ("pid ", "dim"), str(job.proc.pid))]
    if job.log and job.running:
        parts += ["", Text("\n".join(job.log), style="grey50", overflow="ellipsis", no_wrap=True)]
    return Group(*parts)


# ── full-screen app ──────────────────────────────────────────────────────────

def tui():
    from textual import work
    from textual.app import App
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import DirectoryTree, Footer, Input, Markdown, OptionList, Static
    from textual.widgets.option_list import Option

    class Folders(DirectoryTree):
        def filter_paths(self, paths):
            return [p for p in paths if p.is_dir() and not p.name.startswith(".")]

    class FolderPicker(ModalScreen):
        """Browse folders in-app; space picks the highlighted one."""
        CSS = """
        FolderPicker { align: center middle; }
        #picker { width: 80; height: 80%; border: round $accent; border-title-color: $accent; background: $surface; }
        """
        BINDINGS = [Binding("a,space", "choose", "Add this folder", priority=True), Binding("escape,q", "dismiss", "Close")]

        def compose(self):
            yield Folders(Path.home(), id="picker")
            yield Footer()

        def on_mount(self):
            tree = self.query_one("#picker")
            tree.border_title = "Pick a recordings folder · ↑↓ move · enter open · a add"
            tree.focus()

        def action_choose(self):
            node = self.query_one("#picker", DirectoryTree).cursor_node
            if node and node.data:
                self.dismiss(Path(node.data.path))

    class Settings(ModalScreen):
        CSS = """
        Settings { align: center middle; }
        #panel { width: 76; height: auto; border: round $accent; border-title-color: $accent;
                 background: $surface; padding: 1 2; }
        #folders { height: auto; max-height: 8; border: round $panel-lighten-2; border-title-color: $text-muted;
                   margin-bottom: 1; }
        #folders:focus { border: round $accent; border-title-color: $accent; }
        """
        BINDINGS = [
            Binding("a", "add", "Add folder"),
            Binding("d", "remove", "Remove folder"),
            Binding("w", "cycle('whisper')", "Whisper"),
            Binding("l", "cycle('lang')", "Language"),
            Binding("s", "cycle('speakers')", "Speakers"),
            Binding("m", "cycle('model')", "Model"),
            Binding("t", "cycle('kind')", "Meeting type"),
            Binding("escape,comma,q", "dismiss", "Close"),
        ]

        def compose(self):
            with Vertical(id="panel"):
                yield OptionList(id="folders")
                yield Static(id="body")
            yield Footer()

        def on_mount(self):
            self.query_one("#panel").border_title = "Settings"
            self.query_one("#folders").border_title = "Recordings folders · a add · d remove"
            self.paint()
            self.query_one("#folders").focus()

        def paint(self):
            ol = self.query_one("#folders", OptionList)
            ol.clear_options()
            for p in libraries(self.app.state):
                n = len(meetings({"libraries": [str(p)]})) if p.is_dir() else 0
                ol.add_option(Option(Text.assemble(tilde(p), (f"   {n} meeting{'s' * (n != 1)}" if p.is_dir()
                                                              else "   missing", "dim" if p.is_dir() else "red")),
                                     id=str(p)))
            app, s = self.app, self.app.state
            model = app.model()
            t = Table.grid(padding=(0, 2))
            t.add_column(style="bold cyan")
            t.add_column(style="dim")
            t.add_column()
            t.add_row("w", "Whisper", WHISPER_LABELS[s["whisper"]])
            t.add_row("l", "Language", LANGS[s["lang"]])
            t.add_row("s", "Speakers", SPEAKER_CHOICES[s["speakers"]])
            t.add_row("m", "Summary", model or "off")
            t.add_row("t", "Meeting type", s["kind"] if model else "–")
            m = Table.grid(padding=(0, 2))
            for name in WHISPER_MODELS:
                path = whisper_path(name)
                m.add_row(Text("●", style="green" if whisper_ready(name) else "red"), f"Whisper {name}",
                          Text(human(dir_size(path)) if path.exists() else f"missing · debrief setup -w {name}", style="dim"))
            m.add_row(Text("●", style="green" if SPEAKERS.exists() else "red"), "Speaker separation",
                      Text(human(dir_size(SPEAKERS)) if SPEAKERS.exists() else "missing", style="dim"))
            m.add_row(Text("●", style="green" if app.models else "red"), "Ollama",
                      Text(", ".join(app.models) if app.models else "not running (open the Ollama app)", style="dim"))
            self.query_one("#body", Static).update(Group(t, "", Text("Models", style="bold"), m, "",
                                                         Text(f"Models live in {HF}".replace(str(Path.home()), "~"), style="dim")))

        def action_cycle(self, key):
            self.app.cycle_setting(key)
            self.paint()

        def action_add(self):
            def added(path):
                if path:
                    self.app.set_libraries(libraries(self.app.state) + [path])
                    self.paint()
            self.app.push_screen(FolderPicker(), added)

        def action_remove(self):
            libs = libraries(self.app.state)
            idx = self.query_one("#folders", OptionList).highlighted
            if idx is None or len(libs) == 1:
                return self.notify("Keep at least one recordings folder.", severity="warning")
            self.app.set_libraries(libs[:idx] + libs[idx + 1:])
            self.paint()

    class Transcribe(App):
        TITLE = "debrief"
        CSS = """
        #main { height: 1fr; }
        #left { width: 42; }
        .box { border: round $panel-lighten-2; border-title-color: $text-muted; }
        .box:focus-within, .box:focus { border: round $accent; border-title-color: $accent; }
        #search { display: none; border: round $accent; height: 3; }
        #meetings { height: 1fr; padding: 0 1; }
        #meetings > .option-list--option { padding: 0 0 1 0; }
        #right { width: 1fr; padding: 0 2; }
        #info { height: auto; margin: 1 0; }
        #doc { height: auto; }
        """
        BINDINGS = [
            Binding("enter", "run", "Transcribe", priority=True),
            Binding("tab", "toggle", "Summary ⇄ Transcript", priority=True),
            Binding("c", "copy", "Copy summary"),
            Binding("r", "resummarize", "Redo summary"),
            Binding("slash", "search", "Search"),
            Binding("comma", "settings", "Settings"),
            Binding("escape", "back", "Cancel run"),
            Binding("q", "quit", "Quit"),
            Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
            Binding("j", "move(1)", show=False),
            Binding("k", "move(-1)", show=False),
            Binding("space,pagedown,ctrl+d", "scroll(1)", show=False),
            Binding("pageup,ctrl+u", "scroll(-1)", show=False),
        ]

        def __init__(self):
            super().__init__()
            self.state = load_state()
            self.state.setdefault("lang", "auto")
            self.state.setdefault("speakers", "auto")
            self.state.setdefault("kind", "General meeting")
            if self.state.get("whisper") not in WHISPER_MODELS:
                self.state["whisper"] = next((w for w in WHISPER_MODELS if whisper_path(w).exists()), "large-v3")
            self.durations = self.state.setdefault("durations", {})
            self.models = ollama_models()
            if self.models and self.state.get("model") not in self.models + ["none"]:
                self.state["model"] = default_model(self.models)
            self.paths, self.selected, self.filter, self.view = [], None, "", "summary"
            self.job, self.shown_summary, self.stats, self.rerun_armed = None, None, None, None

        def compose(self):
            with Horizontal(id="main"):
                with Vertical(id="left"):
                    yield Input(id="search", placeholder="search meetings")
                    yield OptionList(id="meetings", classes="box")
                with VerticalScroll(id="right", classes="box"):
                    yield Static(id="info")
                    yield Markdown(id="doc")
            yield Footer()

        def on_mount(self):
            self.reload()
            self.query_one("#meetings").focus()
            self.set_interval(0.15, self.tick)
            self.set_interval(1.0, self.sample_stats)
            self.set_interval(5.0, self.watch_library)
            self.measure_all()

        # ── state ──
        def model(self):
            m = self.state.get("model")
            return m if self.models and m in self.models else None

        def cycle_setting(self, key):
            if key == "model":
                if not self.models:
                    return self.notify("Ollama is not running. Open the Ollama app.", severity="warning")
                values = self.models + ["none"]
            else:
                values = list({"lang": LANGS, "speakers": SPEAKER_CHOICES, "kind": CONTEXTS, "whisper": WHISPER_MODELS}[key])
            cur = self.state.get(key)
            self.state[key] = values[(values.index(cur) + 1) % len(values)] if cur in values else values[0]
            save_state(self.state)

        def set_libraries(self, paths):
            self.state["libraries"] = list(dict.fromkeys(tilde(p) for p in paths))
            save_state(self.state)
            self.reload()
            self.measure_all()

        def running(self):
            return bool(self.job and self.job.running)

        # ── meeting list ──
        def reload(self, keep=None):
            keep = keep or self.selected
            self.paths = [p for p in meetings(self.state) if self.filter.lower() in p.name.lower()]
            ol = self.query_one("#meetings", OptionList)
            ol.border_title = f"Meetings · {len(self.paths)}"
            ol.clear_options()
            for p in self.paths:
                ol.add_option(Option(self.row(p), id=str(p)))
            if not self.paths:
                self.selected = None
                return self.show_empty()
            idx = self.paths.index(keep) if keep in self.paths else 0
            ol.highlighted = idx
            self.select(self.paths[idx])

        def row(self, p):
            outdir, prefix = outputs(p)
            if self.job and self.job.running and p == self.job.target:
                status = ("◐ running", "cyan")
            elif (outdir / f"{prefix}transcript.md").exists():
                status = ("✓", "green")
            else:
                status = ("● new", "yellow")
            secs = self.durations.get(str(p), [None, None])[1]
            meta = when(recorded_at(p)) + (f" · {fmt(secs)}" if secs else "")
            if len(libraries(self.state)) > 1:
                meta += f" · {p.parent.name}"
            return Text.assemble((p.name, "bold"), "  ", status, "\n", (meta, "dim"))

        @work(thread=True, exclusive=True, group="measure")
        def measure_all(self):
            changed = False
            for p in meetings(self.state):
                mtime = recorded_at(p)
                if self.durations.get(str(p), [None])[0] != mtime:
                    self.durations[str(p)] = [mtime, audio_seconds(audio_files(p))]
                    changed = True
            if changed:
                save_state(self.state)
                self.call_from_thread(self.reload)

        def watch_library(self):
            """Pick up new recordings without a restart."""
            if not self.query_one("#search").has_focus and [p for p in meetings(self.state)
                                                             if self.filter.lower() in p.name.lower()] != self.paths:
                self.reload()
                self.measure_all()

        # ── right pane ──
        def show_empty(self):
            right = self.query_one("#right")
            right.border_title = "debrief"
            msg = (f"No meetings match “{self.filter}”." if self.filter else
                   "No recordings in " + ", ".join(tilde(p) for p in libraries(self.state)) + " yet.")
            self.query_one("#info", Static).update(Text.assemble(
                (msg, "bold"), "\n\nEach subfolder (or audio file) in a recordings folder is one meeting.\nPress ",
                (",", "bold cyan"), " then ", ("a", "bold cyan"), " to add a recordings folder."))
            self.query_one("#doc", Markdown).update("")

        def select(self, path):
            self.selected, self.rerun_armed = path, None
            right = self.query_one("#right")
            right.border_title = path.name
            if self.job and self.job.target == path and self.job.running:
                self.shown_summary = None
                return
            outdir, prefix = outputs(path)
            summary, transcript = outdir / f"{prefix}summary.md", outdir / f"{prefix}transcript.md"
            files = audio_files(path)
            secs = self.durations.get(str(path), [None, None])[1]
            bits = [fmt(secs) if secs else "measuring…", f"{len(files)} file{'s' * (len(files) != 1)}",
                    human(sum(f.stat().st_size for f in files))]
            text = transcript.read_text() if transcript.exists() else ""
            if text and (n := speaker_count(text)):
                bits.append(f"{n} speakers")
            info = Text(" · ".join(bits))
            if text:
                info.append(f"   transcribed {when(transcript.stat().st_mtime)}", style="dim")
                info.append("\n\n")
                for name in ("summary", "transcript"):
                    active = self.view == name
                    info.append(f" {name.capitalize()} ", style="bold reverse cyan" if active else "dim")
                    info.append("  ")
                info.append("tab switch", style="dim")
                doc = (summary.read_text() if summary.exists() else "_No summary for this meeting._") \
                    if self.view == "summary" else text
            else:
                doc = "Not transcribed yet. Press **enter** to start."
            self.query_one("#info", Static).update(info)
            self.query_one("#doc", Markdown).update(re.sub(r"\A# .*\n+(_.*_\n+)?", "", doc))
            right.scroll_home(animate=False)

        def tick(self):
            if not self.running() or self.selected != self.job.target:
                return
            self.query_one("#info", Static).update(render_job(self.job, self.stats if self.job.running else None))
            if self.job.summary != self.shown_summary:
                self.shown_summary = self.job.summary
                self.query_one("#doc", Markdown).update(self.job.summary)
                self.query_one("#right", VerticalScroll).scroll_end(animate=False)

        def sample_stats(self):
            proc = self.job.proc if self.job else None
            self.stats = proc_stats(proc.pid) if proc and proc.poll() is None else None

        # ── events / actions ──
        def check_action(self, action, parameters):
            if action == "back":
                return self.running() or self.query_one("#search").display
            if action == "run" and self.running():
                return None
            return True

        def on_option_list_option_highlighted(self, event):
            if event.option.id:
                self.select(Path(event.option.id))

        def on_input_changed(self, event):
            self.filter = event.value
            self.reload()

        def action_move(self, delta):
            ol = self.query_one("#meetings", OptionList)
            ol.action_cursor_down() if delta > 0 else ol.action_cursor_up()

        def action_search(self):
            search = self.query_one("#search", Input)
            search.display = True
            search.focus()
            self.refresh_bindings()

        def action_back(self):
            search = self.query_one("#search", Input)
            if search.display:
                search.value, search.display, self.filter = "", False, ""
                self.reload()
                self.query_one("#meetings").focus()
                self.refresh_bindings()
            elif self.running():
                self.job.cancel()
                self.notify("Cancelling…")

        def action_settings(self):
            self.push_screen(Settings(), lambda _: self.reload())

        def action_run(self):
            if self.query_one("#search").has_focus:
                return self.query_one("#meetings").focus()
            if self.running() or not self.selected:
                return
            if not whisper_ready(self.state["whisper"]):
                return self.notify(f"Whisper {self.state['whisper']} is not set up. Quit and run: "
                                   f"debrief setup -w {self.state['whisper']}", severity="error", timeout=10)
            path = self.selected
            outdir, prefix = outputs(path)
            if (outdir / f"{prefix}transcript.md").exists() and self.rerun_armed != path:
                self.rerun_armed = path
                return self.notify("Already transcribed. Press enter again to redo it.")
            self.start_job(path)

        def action_resummarize(self):
            if self.running() or not self.selected:
                return
            outdir, prefix = outputs(self.selected)
            if not (outdir / f"{prefix}transcript.md").exists():
                return self.notify("Transcribe it first (enter).", severity="warning")
            if not self.model():
                return self.notify("No summary model. Press , then m, or open the Ollama app.", severity="warning")
            self.start_job(self.selected, summary_only=True)

        def start_job(self, path, summary_only=False):
            s, model = self.state, self.model()
            opts = {"whisper": s["whisper"], "lang": s["lang"],
                    "speakers": s["speakers"] if SPEAKERS.exists() else "off", "model": model,
                    "context": CONTEXTS.get(s["kind"], "") if model else "", "summary_only": summary_only}
            self.job, self.shown_summary = Job(path, audio_files(path), opts), None
            self.query_one("#doc", Markdown).update("")
            self.reload(keep=path)
            self.refresh_bindings()
            self.execute(self.job)

        @work(thread=True)
        def execute(self, job):
            job.run()
            self.call_from_thread(self.job_done, job)

        def job_done(self, job):
            self.refresh_bindings()
            if job.error:
                cancelled = job.error == "cancelled"
                self.notify(job.error, title="Cancelled" if cancelled else "Failed",
                            severity="warning" if cancelled else "error", timeout=10)
            else:
                self.notify(f"{job.target.name}: done in {fmt(job.t1 - job.t0)}", title="Transcribed", timeout=8)
                self.bell()
            self.reload()

        def action_copy(self):
            if not self.selected:
                return
            outdir, prefix = outputs(self.selected)
            summary = outdir / f"{prefix}summary.md"
            if not summary.exists():
                return self.notify("No summary for this meeting yet.", severity="warning")
            copy(to_slack(summary.read_text()))
            self.notify("Formatted for Slack.", title="Summary copied")

        def action_toggle(self):
            if not self.selected or (self.job and self.job.target == self.selected and self.job.running):
                return
            self.view = "transcript" if self.view == "summary" else "summary"
            self.select(self.selected)

        def action_scroll(self, direction):
            right = self.query_one("#right", VerticalScroll)
            right.scroll_page_down() if direction > 0 else right.scroll_page_up()

        def action_quit(self):
            if self.running():
                self.job.cancel()
            self.exit()

    Transcribe().run()


# ── entry ────────────────────────────────────────────────────────────────────

def selftest():
    assert parse_speaker("SPEAKER meeting 1 45.220 11.320 so yeah ok <NA> B <NA> <NA>") == (45.22, 56.54, "B")
    assert parse_speaker("Transcription Performance:") is None
    assert PROGRESS.search("\x1b[K[====] 33% | Elapsed Time: 5.18 s | Remaining: 10.37 s")[1] == "33"
    assert fmt(3725) == "1:02:05" and fmt(65) == "1:05"
    turns = [(0.0, 35.0, "A"), (38.0, 60.0, "B")]
    seg = lambda a, b, text, **kw: {"start": a, "end": b, "text": text, "temperature": 0.0, "compressionRatio": 1.5, **kw}
    segs = [seg(30, 34, "two"), seg(0, 5, "<|0.00|>one<|5.00|>"), seg(40, 45, "three"),
            seg(46, 50, "jackal", temperature=1.0), seg(50, 52, "plan plan", compressionRatio=13.2), seg(53, 55, "ok")]
    assert build_transcript(turns, segs) == \
        f"**Speaker A** `0:00`\none two\n\n**Speaker B** `0:40`\nthree {UNCLEAR} ok", build_transcript(turns, segs)
    assert build_transcript([], segs[:3]) == "`0:00`\none two three"
    fs = [Path(n) for n in ("applicationAudio-000.caf", "applicationAudio-001.caf", "microphone-000.caf")]
    assert {k: len(v) for k, v in tracks(fs).items()} == {"applicationAudio": 2, "microphone": 1}
    assert overlaid([100, 95]) and not overlaid([100, 40]) and not overlaid([100])
    pcm = lambda *s: array("h", s).tobytes()
    assert mix(pcm(1000, -32000), pcm(500, -1000, 7)) == pcm(1500, -32768, 7)
    md = "# Summary: X\n\n## Overview\nHi **there**.\n\n## Key points\n- **Infra:** good\n  - nested\n\n---\n_Generated locally_"
    assert to_slack(md) == "*Overview*\nHi *there*.\n\n*Key points*\n• *Infra:* good\n  • nested", to_slack(md)
    Console(file=open(os.devnull, "w")).print(render_job(Job(Path("/tmp/x"), [], {"speakers": "auto", "model": "m", "lang": "auto", "context": "", "whisper": "turbo"})))
    print("ok")


# ── setup ────────────────────────────────────────────────────────────────────

HUB = "https://huggingface.co"


def hf_listing(repo, prefix=""):
    url = f"{HUB}/api/models/{repo}/tree/main" + (f"/{prefix}" if prefix else "") + "?recursive=true"
    with urllib.request.urlopen(url, timeout=60) as r:
        return [(f["path"], f["size"]) for f in json.load(r) if f["type"] == "file"]


def download(repo, files, root, progress, task):
    """Resumable and stall-proof: 30 s without data drops the connection and resumes with a Range request."""
    have = {p: (root / p).stat().st_size if (root / p).exists() else 0 for p, _ in files}
    total = sum(s for _, s in files)

    def done():
        return sum(min(have[p], s) for p, s in files)

    progress.update(task, total=total, completed=done())
    for p, size in files:
        dest = root / p
        dest.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(10):
            if have[p] == size:
                break
            if have[p] > size:
                dest.unlink()
                have[p] = 0
            headers = {"Range": f"bytes={have[p]}-"} if have[p] else {}
            try:
                with urllib.request.urlopen(urllib.request.Request(f"{HUB}/{repo}/resolve/main/{p}", headers=headers),
                                            timeout=30) as r:
                    if have[p] and r.status != 206:
                        have[p] = 0
                    with open(dest, "ab" if have[p] else "wb") as f:
                        while chunk := r.read(1 << 20):
                            f.write(chunk)
                            have[p] += len(chunk)
                            progress.update(task, completed=done())
            except OSError:
                time.sleep(min(2 * attempt, 10))
        if have[p] != size:
            raise Fail(f"could not download {repo}/{p}. Run `debrief setup` again to resume.")


def setup(whisper):
    from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TimeRemainingColumn, TransferSpeedColumn
    console = Console()
    console.print(f"[bold]debrief setup[/]  [dim]models go to {tilde(HF)} (set DEBRIEF_MODELS to change)[/]\n")
    if not shutil.which("whisperkit-cli"):
        console.print("[yellow]whisperkit-cli is missing.[/] Install it with: [bold]brew install whisperkit-cli[/]\n")
    parts = [
        (f"Whisper {whisper}", "argmaxinc/whisperkit-coreml", WHISPER_MODELS[whisper], None, WHISPERKIT),
        ("Tokenizer", "openai/whisper-large-v3", "", r"^(config|tokenizer|tokenizer_config)\.json$", TOKENIZER),
        ("Speaker separation", "argmaxinc/speakerkit-coreml", "", r"pyannote-v[34]/|\.json$", SPEAKERS),
    ]
    columns = (TextColumn("{task.description:<20}"), BarColumn(bar_width=30), DownloadColumn(), TransferSpeedColumn(),
               TimeRemainingColumn())
    with Progress(*columns, console=console) as progress:
        for label, repo, prefix, pattern, root in parts:
            task = progress.add_task(label, total=None)
            try:
                files = [(p, s) for p, s in hf_listing(repo, prefix) if not pattern or re.search(pattern, p)]
            except OSError as e:
                raise Fail(f"could not reach Hugging Face ({e}). Check your connection and run setup again.")
            download(repo, files, root, progress, task)

    summary_model = PREFERRED_MODELS[0]
    if not shutil.which("ollama"):
        console.print(f"\n[yellow]Summaries need Ollama:[/] brew install ollama, then run [bold]debrief setup[/] again.")
    elif (models := ollama_models()) is None:
        console.print("\n[yellow]Ollama is installed but not running.[/] Start it (open the app, or "
                      "[bold]brew services start ollama[/]) and run [bold]debrief setup[/] again.")
    elif summary_model not in models:
        console.print(f"\nPulling the summary model [bold]{summary_model}[/]…")
        subprocess.run(["ollama", "pull", summary_model], check=False)
    console.print("\n[green]✓ Ready.[/] Run [bold]debrief[/].")


def main():
    ap = argparse.ArgumentParser(prog="debrief", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", help="folder of audio chunks, one audio file, or `setup` (omit for the app)")
    ap.add_argument("-w", "--whisper", default="large-v3", choices=list(WHISPER_MODELS), help="transcription model")
    ap.add_argument("-l", "--lang", default="auto", choices=list(LANGS), help="spoken language (default: auto)")
    ap.add_argument("-s", "--speakers", default="auto", help="number of speakers, auto (default) or off")
    ap.add_argument("-m", "--model", help=f"Ollama model for the summary (default: {PREFERRED_MODELS[0]})")
    ap.add_argument("-p", "--prompt", default="", help='context for the summary, e.g. "job interview, assess the candidate"')
    ap.add_argument("--no-summary", action="store_true", help="transcript only")
    ap.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.target == "setup":
        return setup(args.whisper)
    if args.target is None:
        if not sys.stdin.isatty():
            ap.error("give a folder or file when not running in a terminal")
        return tui()

    target = Path(args.target).expanduser().resolve()
    files = audio_files(target) if target.exists() else []
    if not files:
        raise Fail(f"no audio files at {target}")
    if not whisper_ready(args.whisper):
        raise Fail(f"whisper {args.whisper} is not set up. Run: debrief setup -w {args.whisper}")
    model = None
    if not args.no_summary:
        models = ollama_models()
        if models is None:
            raise Fail("Ollama is not running. Open the Ollama app, or pass --no-summary.")
        if args.model and args.model not in models:
            raise Fail(f"Ollama has no model '{args.model}'. Installed: {', '.join(models)}")
        model = args.model or default_model(models)
    job = Job(target, files, {"whisper": args.whisper, "lang": args.lang, "speakers": args.speakers if SPEAKERS.exists() else "off",
                              "model": model, "context": args.prompt}, echo=True)
    job.run()
    if job.error:
        raise Fail(job.error)
    if job.summary:
        Console().print(RichMarkdown(job.summary))
    print(f"\nDone in {fmt(job.t1 - job.t0)} · {fmt(job.audio)} of audio · {job.outdir}")


if __name__ == "__main__":
    try:
        main()
    except Fail as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
