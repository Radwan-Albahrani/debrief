"""The command line: `debrief` opens the app, `debrief setup` downloads models, `debrief <folder>` runs one-shot."""
import argparse
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown

from debrief.core.config import DEFAULTS, LANGUAGES, MEETING_KINDS, SUMMARY_MODELS, WHISPER_MODELS
from debrief.core.errors import DebriefError
from debrief.core.formatting import clock
from debrief.features import library, summary, transcription
from debrief.pipeline import Job, Options, Step

USAGE = """local meeting transcripts with speaker labels, and summaries, all on your Mac

  debrief               the full-screen app
  debrief setup         download the models (resumable)
  debrief <folder>      transcribe and summarize one meeting, no UI

A folder is one meeting: chunks are joined in order, and simultaneous tracks (applicationAudio-000.caf +
microphone-000.caf) are overlaid. Writes transcript.md, transcript.srt and summary.md next to the audio."""


def speakers(value: str) -> str:
    """'auto', 'off', or how many people spoke."""
    if value in ("auto", "off") or (value.isdigit() and int(value) > 0):
        return value
    raise argparse.ArgumentTypeError("use auto, off, or a number of speakers")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="debrief", description=USAGE, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", help="a meeting folder or audio file, or `setup` (omit for the app)")
    ap.add_argument("-w", "--whisper", default=DEFAULTS["whisper"], choices=list(WHISPER_MODELS),
                    help="transcription model")
    ap.add_argument("-l", "--lang", default=DEFAULTS["lang"], choices=list(LANGUAGES), help="spoken language")
    ap.add_argument("-s", "--speakers", default=DEFAULTS["speakers"], type=speakers,
                    help="number of speakers, or auto/off")
    ap.add_argument("-m", "--model", help=f"Ollama model for the summary (default: {SUMMARY_MODELS[0]})")
    ap.add_argument("-k", "--kind", default=DEFAULTS["kind"], choices=MEETING_KINDS, help="meeting type")
    ap.add_argument("-p", "--prompt", default="", help='extra context for the summary, e.g. "senior backend role"')
    ap.add_argument("--no-summary", action="store_true", help="transcript only")
    ap.add_argument("--version", action="version", version=f"debrief {_version()}")
    return ap


def _version() -> str:
    try:
        return version("debrief")
    except PackageNotFoundError:  # running from a source checkout that wasn't installed
        return "dev"


def run_once(args: argparse.Namespace) -> None:
    target = Path(args.target).expanduser().resolve()
    if not (target.exists() and library.audio_files(target)):
        raise DebriefError(f"no audio files at {target}")
    if not transcription.ready(args.whisper):
        raise DebriefError(f"whisper {args.whisper} is not set up. Run: debrief setup -w {args.whisper}")
    model = None
    if not args.no_summary:
        installed = summary.installed_models()
        if installed is None:
            raise DebriefError("Ollama is not running. Open the Ollama app, or pass --no-summary.")
        if args.model and args.model not in installed:
            raise DebriefError(f"Ollama has no model '{args.model}'. Installed: {', '.join(installed)}")
        model = args.model or summary.default_model(installed)

    def print_step(name: str, step: Step):
        print(f"✓ {name:<11} {clock(step.ended - step.started):>6}  {step.note}", flush=True)

    options = Options(whisper=args.whisper, lang=args.lang, speakers=args.speakers, model=model,
                      kind=args.kind, hint=args.prompt)
    job = Job(target, options, on_step_done=print_step)
    job.run()
    if job.error:
        raise DebriefError(job.error)
    if job.summary:
        Console().print(Markdown(job.summary))
    print(f"\nDone in {clock(job.ended - job.started)} · {clock(job.audio_seconds)} of audio · "
          f"{library.output_file(target, library.TRANSCRIPT).parent}")


def main() -> None:
    args = parser().parse_args()
    try:
        if args.target == "setup":
            from debrief.features.setup import run_setup
            run_setup(args.whisper)
        elif args.target is None:
            if not sys.stdin.isatty():
                parser().error("give a folder or file when not running in a terminal")
            from debrief.surfaces.tui import run_app  # textual loads only when the app is used
            run_app()
        else:
            run_once(args)
    except DebriefError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
