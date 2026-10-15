"""The controller: one Job runs the features in order (Convert → Transcribe → Speakers → Summary) on a background
thread, and keeps its progress in plain fields that any surface can render."""
import shutil
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from debrief.core.errors import Cancelled, DebriefError
from debrief.core.formatting import clock
from debrief.features import audio, library, summary, transcription
from debrief.pipeline.options import Options

STEPS = ("Convert", "Transcribe", "Speakers", "Summary")


@dataclass
class Step:
    state: str = "wait"  # wait, run, done, fail or skip
    started: float | None = None
    ended: float | None = None
    progress: float | None = None  # 0 to 1, or None while it can't be measured
    note: str = ""


class Job:
    def __init__(self, target: Path, options: Options, on_step_done: Callable[[str, Step], None] | None = None):
        self.target, self.options, self.on_step_done = target, options, on_step_done
        self.steps = {name: Step() for name in STEPS}
        skipped = {"Convert": options.summary_only, "Transcribe": options.summary_only,
                   "Speakers": options.summary_only or options.speakers == "off", "Summary": not options.model}
        for name, skip in skipped.items():
            if skip:
                self.steps[name].state = "skip"
        self.started, self.ended = time.time(), None
        self.audio_seconds = 0.0
        self.log = deque(maxlen=4)  # the latest lines worth showing
        self.proc = None  # whisperkit-cli while it runs
        self.summary = ""  # streamed as it is written, then replaced by the checked version
        self.error: str | None = None
        self.cancelled = False
        self._cancel = threading.Event()
        self._stream: summary.SummaryStream | None = None
        self._summary_lock = threading.Lock()  # the meeting type can change until the summary starts
        self._summary_started = False
        self._kind_settled = threading.Event()  # cleared while someone is still deciding the meeting type
        self._kind_settled.set()
        self.waiting_for_kind = False  # the summary is ready to start but is waiting for that decision

    @property
    def running(self) -> bool:
        return self.ended is None

    def cancel(self) -> None:
        """Stop as soon as possible; safe to call from any thread."""
        self._cancel.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        if self._stream:
            self._stream.close()

    def hold_summary(self) -> None:
        """Make the summary wait (the other steps still run) until `release_summary`, e.g. while notes are typed."""
        self._kind_settled.clear()

    def release_summary(self) -> None:
        self._kind_settled.set()

    @property
    def kind_locked(self) -> bool:
        """The summary has started or the run is over, so the meeting type can no longer change."""
        return self._summary_started or not self.running

    def set_kind(self, kind: str, hint: str = "") -> bool:
        """Change what the summary is asked for. False once `kind_locked`."""
        with self._summary_lock:
            if self.kind_locked:
                return False
            self.options.kind, self.options.hint = kind, hint
            return True

    def run(self) -> None:
        """Run every step that isn't skipped. Never raises: failures end up in `self.error`."""
        try:
            self._check_cancel()
            if self.options.summary_only:
                transcript = library.body_of(library.read(self.target, library.TRANSCRIPT))
                if not transcript:
                    raise DebriefError("there is no transcript to summarize yet: transcribe the meeting first")
            else:
                with tempfile.TemporaryDirectory(prefix="debrief-") as scratch:
                    transcript = self._transcribe(self._convert(Path(scratch)), Path(scratch))
            if self.options.model:
                self._summarize(transcript)
        except Cancelled:
            self.cancelled, self.error = True, "cancelled"
        except DebriefError as e:
            self.error = str(e)
        except Exception as e:  # surface anything unexpected instead of dying silently on a background thread
            self.error = f"{type(e).__name__}: {e}"
        finally:
            for step in self.steps.values():
                if step.state == "run":
                    step.state, step.ended = "fail", time.time()
            with self._summary_lock:
                self.ended = time.time()

    # ── steps ────────────────────────────────────────────────────────────────

    def _convert(self, scratch: Path) -> Path:
        self._start("Convert")
        files = library.audio_files(self.target)
        if not files:
            raise DebriefError(f"no audio files in {self.target}")
        pcm, done = {}, 0
        for name, chunks in audio.tracks(files).items():
            pcm[name] = bytearray()
            for f in chunks:
                self._update("Convert", progress=done / len(files), note=f"{f.name}  ({done + 1}/{len(files)})")
                pcm[name] += audio.decode(f, scratch)
                done += 1
        if audio.overlaid([len(p) for p in pcm.values()]):
            self._update("Convert", note=f"overlaying {' + '.join(pcm)}…")
            mixed = pcm.popitem()[1]
            for other in pcm.values():
                mixed = audio.mix(mixed, other)
            how = f"{len(pcm) + 1} simultaneous tracks overlaid"
        else:
            mixed, how = b"".join(pcm.values()), f"{len(files)} file(s)"
        wav = scratch / "meeting.wav"
        self.audio_seconds = audio.write_wav(wav, mixed)
        self._finish("Convert", f"{how} → {clock(self.audio_seconds)} of 16 kHz mono audio")
        return wav

    def _transcribe(self, wav: Path, scratch: Path) -> str:
        o = self.options
        diarize = o.speakers != "off"
        first_progress = None
        self._start("Transcribe", f"loading whisper {o.whisper} onto the Neural Engine…")

        def started(proc):
            self.proc = proc
            if self._cancel.is_set():  # cancelled while it was starting
                proc.terminate()

        def progressed(fraction, remaining):
            nonlocal first_progress
            first_progress = first_progress or time.time()
            heard = fraction * self.audio_seconds
            speed = heard / max(time.time() - first_progress, 0.1)
            self._update("Transcribe", progress=fraction, note=f"{clock(heard)} / {clock(self.audio_seconds)} of "
                                                               f"audio · {speed:.1f}× realtime · ~{clock(remaining)} left")
            if fraction >= 1 and diarize and self.steps["Speakers"].state == "wait":
                took = time.time() - self.steps["Transcribe"].started
                self._finish("Transcribe", f"{clock(self.audio_seconds)} of audio · "
                                           f"{self.audio_seconds / took:.1f}× realtime")
                self._start("Speakers", "pyannote: segmenting voices and clustering who said what…")

        def said(line):
            if not line.startswith("SPEAKER") and len(line) < 200:
                self.log.append(line.strip())

        result = transcription.run(o.whisper, o.lang, o.speakers, wav, scratch, on_start=started,
                                   on_progress=progressed, on_line=said, cancelled=self._cancel.is_set)
        self._check_cancel()
        if self.steps["Transcribe"].state == "run":
            self._finish("Transcribe", f"{clock(self.audio_seconds)} of audio")
        if diarize:
            if self.steps["Speakers"].state == "wait":
                self._start("Speakers")
            turns = result.turns
            if turns:
                self._finish("Speakers", f"{len({t[2] for t in turns})} speakers · {len(turns)} turns")
            else:
                why = f" ({result.diarization_errors[0][:80]})" if result.diarization_errors else ""
                self._finish("Speakers", f"no speaker labels{why}")

        body = transcription.build_transcript(result.turns, result.segments)
        shutil.copyfile(result.srt, library.output_file(self.target, library.TRANSCRIPT_SRT))
        library.write_transcript(self.target, body,
                                 f"{date.today()} · {clock(self.audio_seconds)} · whisper {o.whisper} (local)")
        return body

    def _summarize(self, transcript: str) -> None:
        model = self.options.model
        if not self._kind_settled.is_set():
            self._start("Summary", "waiting for the meeting type and your notes…")
            self.waiting_for_kind = True
            while not self._kind_settled.wait(0.2):
                self._check_cancel()
            self.waiting_for_kind = False
        with self._summary_lock:  # from here on the meeting type is fixed
            self._summary_started = True
            self._stream = stream = summary.SummaryStream(model, transcript, self.options.task)
        self._start("Summary", f"{model}: loading and reading ~{len(transcript) // 3:,} tokens of transcript…")
        self._check_cancel()
        first_token = None
        try:
            for piece in stream:
                if first_token is None:
                    first_token = time.time()
                    self.log.append(f"{model} read the transcript in {clock(first_token - self.steps['Summary'].started)}")
                self.summary += piece
                rate = stream.stats["output_tokens"] / max(time.time() - first_token, 0.1)
                self._update("Summary", note=f"{model} · writing · {stream.stats['output_tokens']} tokens · "
                                            f"{rate:.0f} tok/s")
        except Exception:
            self._check_cancel()  # a cancel closes the connection: report it as a cancel, not a failure
            raise
        self.summary, dropped = summary.check_summary(summary.strip_thinking(self.summary), transcript)
        if dropped:
            self.log.append(f"removed {len(dropped)} line(s) the transcript does not support")
        footer = f"Generated locally by {model} from {library.output_file(self.target, library.TRANSCRIPT).name}"
        if dropped:
            footer += f"; {len(dropped)} unsupported statement(s) removed"
        library.write_summary(self.target, self.summary, footer + ".")
        self._finish("Summary", f"{model} · {stream.stats['prompt_tokens'] or '?'} tokens in, "
                                f"{stream.stats['output_tokens']} out")

    # ── progress ─────────────────────────────────────────────────────────────

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise Cancelled

    def _start(self, name: str, note: str = "") -> None:
        step = self.steps[name]
        step.state, step.started, step.note = "run", time.time(), note

    def _update(self, name: str, progress: float | None = None, note: str | None = None) -> None:
        self._check_cancel()
        step = self.steps[name]
        if progress is not None:
            step.progress = progress
        if note is not None:
            step.note = note

    def _finish(self, name: str, note: str | None = None) -> None:
        step = self.steps[name]
        step.state, step.ended = "done", time.time()
        if step.progress is not None:
            step.progress = 1.0
        if note is not None:
            step.note = note
        if self.on_step_done:
            self.on_step_done(name, step)
