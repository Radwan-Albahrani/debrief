"""The pipeline wiring, settings and the command line, with the models replaced by fakes."""
import argparse
import io
import json
import tempfile
import unittest
from array import array
from pathlib import Path
from unittest import mock

from rich.console import Console

from debrief.core import settings
from debrief.features import library
from debrief.features.transcription import WhisperKitResult
from debrief.pipeline import Job, Options
from debrief.surfaces import cli
from debrief.surfaces.tui.progress import render_job


def fake_whisperkit(whisper, lang, speakers, audio_path, report_dir, *, on_start, on_progress, on_line, cancelled):
    on_progress(0.5, 3.0)
    on_line("SPEAKER meeting 1 0.0 2.0 hi <NA> A <NA> <NA>")
    on_progress(1.0, 0.0)
    (report_dir / "meeting.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\nhello there\n")
    return WhisperKitResult(segments=[{"start": 0.0, "end": 2.0, "text": "hello there", "temperature": 0.0,
                                       "compressionRatio": 1.2}],
                            turns=[(0.0, 2.0, "A")], srt=report_dir / "meeting.srt", diarization_errors=[])


class FakeStream:
    def __init__(self, model, transcript, task):
        self.task = task
        self.stats = {"prompt_tokens": 12, "output_tokens": 0}

    def __iter__(self):
        for piece in ("## Overview\n", "They said hello there.\n\n", "## Decisions\nNone recorded."):
            self.stats["output_tokens"] += 1
            yield piece

    def close(self):
        pass


class JobTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.target = Path(self.dir.name)
        (self.target / "audio-000.wav").touch()
        self.speaker_model = mock.patch("debrief.pipeline.options.SPEAKER_MODEL_DIR", self.target)  # "installed"
        self.speaker_model.start()

    def tearDown(self):
        self.speaker_model.stop()
        self.dir.cleanup()

    @mock.patch("debrief.features.summary.SummaryStream", FakeStream)
    @mock.patch("debrief.features.transcription.run", fake_whisperkit)
    @mock.patch("debrief.features.audio.decode", lambda f, scratch: array("h", [0] * 32000).tobytes())
    def test_full_run_writes_all_three_documents(self):
        done = []
        job = Job(self.target, Options(model="qwen"), on_step_done=lambda name, step: done.append(name))
        job.run()
        self.assertIsNone(job.error)
        self.assertEqual(done, ["Convert", "Transcribe", "Speakers", "Summary"])
        self.assertEqual(job.audio_seconds, 2.0)
        self.assertEqual(library.body_of(library.read(self.target, library.TRANSCRIPT)),
                         "**Speaker A** `0:00`\nhello there")
        self.assertTrue(library.output_file(self.target, library.TRANSCRIPT_SRT).exists())
        saved = library.read(self.target, library.SUMMARY)
        self.assertEqual(library.body_of(saved), "## Overview\nThey said hello there.")  # placeholder section dropped
        self.assertIn("Generated locally by qwen from transcript.md", library.footer_of(saved))

    @mock.patch("debrief.features.summary.SummaryStream", FakeStream)
    def test_summary_only_uses_the_saved_transcript(self):
        library.write_transcript(self.target, "**Speaker A** `0:00`\nhello there", "meta")
        job = Job(self.target, Options(model="qwen", summary_only=True, kind="Job interview"))
        job.run()
        self.assertIsNone(job.error)
        self.assertEqual([n for n, s in job.steps.items() if s.state == "done"], ["Summary"])

    def test_summary_only_without_a_transcript_fails(self):
        job = Job(self.target, Options(model="qwen", summary_only=True))
        job.run()
        self.assertIn("no transcript", job.error)

    def test_cancel_before_start(self):
        job = Job(self.target, Options(model=None))
        job.cancel()
        job.run()
        self.assertTrue(job.cancelled)
        self.assertFalse(job.running)

    def test_kind_can_change_until_the_summary_starts(self):
        job = Job(self.target, Options(model="qwen"))
        self.assertTrue(job.set_kind("Job interview", "backend role"))
        self.assertIn("## Depth check", job.options.task)
        job.ended = 1.0  # the run is over
        self.assertFalse(job.set_kind("1:1"))

    def test_skipped_steps(self):
        job = Job(self.target, Options(model=None, speakers="off"))
        self.assertEqual({n: s.state for n, s in job.steps.items()},
                         {"Convert": "wait", "Transcribe": "wait", "Speakers": "skip", "Summary": "skip"})

    def test_missing_speaker_model_turns_speakers_off(self):
        with mock.patch("debrief.pipeline.options.SPEAKER_MODEL_DIR", self.target / "missing"):
            self.assertEqual(Options(speakers="auto").speakers, "off")

    def test_render_job(self):
        job = Job(self.target, Options(model="qwen"))
        out = io.StringIO()
        Console(file=out, width=120).print(render_job(job))
        self.assertIn("Transcribe", out.getvalue())
        self.assertIn("Running", out.getvalue())


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.dir.name) / "state.json"
        self.patch = mock.patch("debrief.core.settings.STATE_FILE", self.state_file)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.dir.cleanup()

    def test_defaults_when_missing(self):
        state = settings.load()
        self.assertEqual((state["whisper"], state["kind"], state["durations"]), ("large-v3", "General meeting", {}))

    def test_invalid_values_fall_back(self):
        self.state_file.write_text(json.dumps({"lang": ["en"], "speakers": "seven", "kind": "1:1"}))
        state = settings.load()
        self.assertEqual((state["lang"], state["speakers"], state["kind"]), ("auto", "auto", "1:1"))

    def test_corrupt_file_never_crashes(self):
        for text in ("{not json", "[1, 2]"):
            self.state_file.write_text(text)
            self.assertEqual(settings.load()["whisper"], "large-v3")

    def test_save_round_trip(self):
        state = settings.load()
        state["libraries"] = ["~/Calls"]
        settings.save(state)
        self.assertEqual(settings.libraries(settings.load()), [Path("~/Calls").expanduser()])


class CliTest(unittest.TestCase):
    def test_speakers_argument(self):
        self.assertEqual(cli.speakers("7"), "7")
        self.assertEqual(cli.speakers("off"), "off")
        with self.assertRaises(argparse.ArgumentTypeError):
            cli.speakers("many")

    def test_parser_defaults(self):
        args = cli.parser().parse_args(["meeting"])
        self.assertEqual((args.whisper, args.speakers, args.kind), ("large-v3", "auto", "General meeting"))


if __name__ == "__main__":
    unittest.main()
