"""Unit tests for the features: everything that doesn't need a model. Run with `python -m unittest`."""
import os
import tempfile
import unittest
from array import array
from pathlib import Path

from debrief.core.formatting import clock, plural
from debrief.features import audio, export, library, summary, transcription
from debrief.features.transcription import UNCLEAR
from debrief.features.transcription import whisperkit


class FormattingTest(unittest.TestCase):
    def test_clock(self):
        self.assertEqual(clock(65), "1:05")
        self.assertEqual(clock(3725), "1:02:05")

    def test_plural(self):
        self.assertEqual(plural(1, "file"), "1 file")
        self.assertEqual(plural(3, "file"), "3 files")


class WhisperKitTest(unittest.TestCase):
    def test_parse_speaker(self):
        line = "SPEAKER meeting 1 45.220 11.320 so yeah ok <NA> B <NA> <NA>"
        self.assertEqual(whisperkit.parse_speaker(line), (45.22, 56.54, "B"))
        self.assertIsNone(whisperkit.parse_speaker("Transcription Performance:"))

    def test_progress_line(self):
        match = whisperkit.PROGRESS_RE.search("\x1b[K[====] 33% | Elapsed Time: 5.18 s | Remaining: 10.37 s")
        self.assertEqual((match[1], match[2]), ("33", "10.37"))

    def test_command_flags(self):
        audio_path, report = Path("/tmp/a.wav"), Path("/tmp/r")
        plain = whisperkit._command("large-v3", "auto", "off", audio_path, report)
        self.assertNotIn("--language", plain)
        self.assertNotIn("--diarization", plain)
        full = whisperkit._command("turbo", "ar", "3", audio_path, report)
        self.assertEqual(full[full.index("--language") + 1], "ar")
        self.assertEqual(full[full.index("--diarization-num-speakers") + 1], "3")
        self.assertIn("openai_whisper-large-v3-v20240930_turbo", full[full.index("--model-path") + 1])

    def test_output_lines_split_progress_redraws(self):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"loading\r10% bar\x1b[K20% bar\nSPEAKER x\npartial")
        os.close(write_fd)
        try:
            self.assertEqual(list(whisperkit._output_lines(read_fd)),
                             ["loading", "10% bar", "20% bar", "SPEAKER x", "partial"])
        finally:
            os.close(read_fd)


class TranscriptTest(unittest.TestCase):
    def test_build_transcript(self):
        turns = [(0.0, 35.0, "A"), (38.0, 60.0, "B")]

        def seg(start, end, text, **kw):
            return {"start": start, "end": end, "text": text, "temperature": 0.0, "compressionRatio": 1.5, **kw}

        segments = [seg(30, 34, "two"), seg(0, 5, "<|0.00|>one<|5.00|>"), seg(40, 45, "three"),
                    seg(46, 50, "jackal", temperature=1.0),  # every fallback used up: a hallucination
                    seg(50, 52, "plan plan", compressionRatio=13.2),  # looping output
                    seg(53, 55, "ok")]
        self.assertEqual(transcription.build_transcript(turns, segments),
                         f"**Speaker A** `0:00`\none two\n\n**Speaker B** `0:40`\nthree {UNCLEAR} ok")
        self.assertEqual(transcription.build_transcript([], segments[:3]), "`0:00`\none two three")

    def test_speaker_count(self):
        self.assertEqual(transcription.speaker_count("**Speaker A** `0:00`\nhi\n\n**Speaker B** `0:05`\nyo"), 2)


class AudioTest(unittest.TestCase):
    def test_tracks(self):
        files = [Path(n) for n in ("applicationAudio-000.caf", "applicationAudio-001.caf", "microphone-000.caf")]
        self.assertEqual({k: len(v) for k, v in audio.tracks(files).items()}, {"applicationAudio": 2, "microphone": 1})

    def test_overlaid(self):
        self.assertTrue(audio.overlaid([100, 95]))
        self.assertFalse(audio.overlaid([100, 40]))
        self.assertFalse(audio.overlaid([100]))

    def test_mix_sums_clips_and_pads(self):
        pcm = lambda *samples: array("h", samples).tobytes()
        self.assertEqual(audio.mix(pcm(1000, -32000), pcm(500, -1000, 7)), pcm(1500, -32768, 7))

    def test_write_wav_reports_length(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(audio.write_wav(Path(d) / "a.wav", bytes(2 * 16000)), 1.0)


class LibraryTest(unittest.TestCase):
    def test_output_file(self):
        with tempfile.TemporaryDirectory() as d:
            folder, file = Path(d), Path(d) / "call.m4a"
            file.touch()
            self.assertEqual(library.output_file(folder, library.SUMMARY), folder / "summary.md")
            self.assertEqual(library.output_file(file, library.SUMMARY), folder / "call.summary.md")

    def test_documents_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            library.write_transcript(target, "**Speaker A** `0:00`\nhello", "2026-10-12 · 0:05 · whisper large-v3")
            library.write_summary(target, "## Overview\nShort.", "Generated locally; 2 removed.")
            self.assertEqual(library.body_of(library.read(target, library.TRANSCRIPT)), "**Speaker A** `0:00`\nhello")
            saved = library.read(target, library.SUMMARY)
            self.assertEqual(library.body_of(saved), "## Overview\nShort.")
            self.assertEqual(library.footer_of(saved), "Generated locally; 2 removed.")
            self.assertEqual(library.read(target / "missing", library.SUMMARY), "")

    def test_a_dash_rule_inside_a_transcript_is_not_a_footer(self):
        doc = "# Transcript: x\n\n_meta_\n\n**Speaker B** `0:05`\n---\nstill talking\n"
        self.assertEqual(library.body_of(doc), "**Speaker B** `0:05`\n---\nstill talking")

    def test_find_meetings(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "standup").mkdir()
            (root / "standup" / "audio-001.caf").touch()
            (root / "standup" / "audio-000.caf").touch()
            (root / "notes").mkdir()  # no audio: not a meeting
            (root / "call.m4a").touch()
            self.assertEqual(sorted(m.name for m in library.find_meetings([root])), ["call.m4a", "standup"])
            self.assertEqual([f.name for f in library.audio_files(root / "standup")], ["audio-000.caf", "audio-001.caf"])


class SummaryChecksTest(unittest.TestCase):
    def test_interview(self):
        transcript = "Speaker A: we pay 4500 per month to the Naqshat team"
        draft = ("## Overview\nThey met. Salary is 9000 riyals.\n\n## Depth check\n- **A**: x **Vague**\n"
                 "- **B**: y **Couldn't explain**\n- **C**: z 4500 **Clear**\n\n## Strengths\nGreat at Naqshatt.\n\n"
                 "## Next steps\nSecond round.\n\n## Logistics\nNot discussed.")
        checked, dropped = summary.check_summary(draft, transcript)
        self.assertEqual(checked, "## Overview\nThey met.\n\n## Signal\n**Weak**: 1 of 3 probed topics answered "
                                  "clearly.\n\n## Depth check\n- **A**: x **Vague**\n- **B**: y **Couldn't explain**\n"
                                  "- **C**: z 4500 **Clear**\n\n## Strengths\nGreat at Naqshatt.")
        self.assertEqual(dropped, ["Salary is 9000 riyals."])  # a number the transcript never mentions

    def test_meeting_drops_invented_names_and_empty_sections(self):
        draft = "## Overview\nMet. They hired Zorblax.\n\n## Next steps\nShip it.\n\n## Decisions\nNone mentioned."
        checked, _ = summary.check_summary(draft, "met ship")
        self.assertEqual(checked, "## Overview\nMet.\n\n## Next steps\nShip it.")

    def test_placeholder_sections_are_dropped(self):
        draft = "## Overview\nMet.\n\n## Decisions\nNone recorded.\n\n## Open questions\nNone raised."
        self.assertEqual(summary.check_summary(draft, "met")[0], "## Overview\nMet.")

    def test_no_clear_answer_means_no_strengths(self):
        checked, _ = summary.check_summary("## Depth check\n- **A**: x **Vague**\n\n## Strengths\nBuilt Pulsar.", "a")
        self.assertTrue(checked.endswith("## Strengths\nNone demonstrated."), checked)

    def test_strip_thinking(self):
        self.assertEqual(summary.strip_thinking("<think>hmm</think>\n## Overview\nx"), "## Overview\nx")

    def test_summary_context(self):
        self.assertIn("## Depth check", summary.summary_context("Job interview"))
        self.assertIn("one-on-one", summary.summary_context("1:1"))
        self.assertTrue(summary.summary_context("General meeting", "q3 plan")
                        .endswith("Extra context from the user: q3 plan"))


class ExportTest(unittest.TestCase):
    def test_to_slack(self):
        md = ("# Summary: X\n\n## Overview\nHi **there**.\n\n## Key points\n- **Infra:** good\n  - nested\n\n"
              "---\n_Generated locally_\n")
        self.assertEqual(export.to_slack(md), "*Overview*\nHi *there*.\n\n*Key points*\n• *Infra:* good\n  • nested")


if __name__ == "__main__":
    unittest.main()
