# debrief

Meeting transcripts with speaker labels, and debriefs you can act on weeks later. Everything runs on your Mac:
no API keys, no uploads, no subscription.

```
╭ Meetings · 3 ──────────────────╮╭ Candidate B - Interview ────────────────────────╮
│ Candidate A - Interview  ● new ││ 48:20 · 49 files · 1.1 GB · 3 speakers           │
│ today 14:03 · 41:12            ││                                                  │
│ Candidate B - Interview  ✓     ││  Summary   Transcript   tab switch               │
│ today 11:20 · 48:20            ││                                                  │
│ Weekly sync  ✓                 ││ ## Overview                                      │
│ Oct 2 09:30 · 31:10            ││ ...                                              │
╰────────────────────────────────╯╰──────────────────────────────────────────────────╯
 enter Transcribe  tab Summary ⇄ Transcript  c Copy summary  r Redo summary  / Search  , Settings  q Quit
```

- **Transcription** with Whisper large-v3 on the Neural Engine, through
  [WhisperKit](https://github.com/argmaxinc/WhisperKit). Large-v3 turbo is there when speed matters more.
- **Speaker labels** (Speaker A, B, C…) with pyannote, which adds about 5 seconds per meeting.
- **Debriefs** from a local [Ollama](https://ollama.com) model (`qwen3.5:9b`):
  - for meetings: decisions, action items with owners, and open questions;
  - for interviews: how the candidate handled each technical follow-up (Clear, Vague or Couldn't explain) and
    a Strong/Mixed/Weak signal.
- **Hallucination checks** in code. Before a summary is saved:
  - any number the transcript doesn't contain is removed;
  - so is any name that doesn't roughly match something said;
  - empty sections are dropped;
  - an interview's signal is computed from its verdicts, so the two can't disagree.
- **Mixed-language meetings.** We use it for interviews that switch between Arabic and English.
- **Messy recordings.** Chunked recordings are joined, and separate app and microphone tracks are overlaid
  into one timeline. Lines Whisper failed to decode show as *[unclear audio]* instead of invented text.

## Install

You need an Apple Silicon Mac, [Homebrew](https://brew.sh) and Python 3.10 or newer.

```sh
git clone https://github.com/Radwan-Albahrani/debrief.git
cd debrief
./install.sh            # or ./install.sh -w turbo to also fetch the faster model
```

`install.sh` does five things:
1. Installs `whisperkit-cli` and `ollama` with Homebrew.
2. Creates a `.venv` and installs debrief into it in editable mode, so a `git pull` updates the command too.
3. Links a `debrief` command into `~/.local/bin`.
4. Downloads the models:
   - Whisper large-v3, about 3 GB;
   - speaker separation, 36 MB;
   - `qwen3.5:9b`, 6.6 GB.
5. Resumes interrupted downloads: if one stalls on a slow connection, just run `debrief setup` again.

## Use

Put each meeting in its own folder inside `~/Documents/Meetings`, then run `debrief`. What counts as one meeting:
- a folder of chunks (`audio-000.caf`, `audio-001.caf`, …);
- a folder of separate simultaneous tracks (`applicationAudio-000.caf` + `microphone-000.caf`);
- a single audio file.

| Key | |
|---|---|
| `enter` | transcribe and summarize the selected meeting |
| `tab` | switch between summary and transcript |
| `c` | copy the summary, formatted for Slack |
| `r` | redo just the summary (about a minute, no re-transcription) |
| `/` | search |
| `,` | settings: recordings folders, Whisper model, language, speakers, summary model, meeting type |
| `esc` | close the search, or cancel a run |
| `q` | quit |

Pressing `enter` starts transcription straight away and opens a small window where you can:
- pick the meeting type: general meeting, job interview, 1:1 or client call;
- add a hint such as "senior backend role".

You can ignore it: it closes by itself when the summary starts and uses your last choice.

Next to the audio you get `transcript.md`, `transcript.srt` and `summary.md`. A single audio file `call.m4a` gets
`call.transcript.md` and so on.

For scripts there is a one-shot mode with no UI:

```sh
debrief ~/Documents/Meetings/standup -w turbo -k "General meeting" -p "weekly standup"
debrief --help
```

## Benchmarks

Measured on two real interviews on an M5 MacBook Pro with 32 GB of RAM:

| | 48:20 interview | 39:00 interview |
|---|---|---|
| Whisper large-v3 | 8:37 (5.6× realtime) | 8:23 (4.6×) |
| Whisper large-v3 turbo | 2:08 (22.6×) | 2:17 (17.1×) |
| Speaker labels | 0:05 | 0:04 |
| Summary (`qwen3.5:9b`) | about 1:00 | about 1:00 |

**Why large-v3 is the default:** on Arabic speech, large-v3 writes an English translation, while turbo writes the
Arabic down word for word. Turbo is 4× faster, but any Arabic name it mishears ends up in the summary as fact.
Summaries built from large-v3 transcripts were noticeably more accurate. On English-only meetings turbo is an easy
win: run `debrief setup -w turbo`, then switch it under `,` → `w`.

**Why one model call:** we also tried multi-step pipelines (extract claims, grade each answer, then write).
Qwen writes about 10 tokens a second on this hardware, so those took 3 to 5 minutes. Grading answers one at a time
also made the 9B model either too lenient or too strict. One focused call plus code checks was both the fastest and
the most accurate, judged against the actual interviews.

Treat summaries as a first draft. A 9B model can still keep a misheard name, so skim names and numbers before you
post.

## Configuration

| | |
|---|---|
| `DEBRIEF_MODELS` | where models are stored (default `~/Documents/huggingface`, the same place WhisperKit uses) |
| `DEBRIEF_HOME` | where your settings live (default `~/.local/share/debrief`) |
| `DEBRIEF_BIN` | where `install.sh` links the command (default `~/.local/bin`) |

Everything else (model names, choices, defaults) is in [`debrief/core/config.py`](debrief/core/config.py).

## How it works

A run is a `Job` that goes through four steps:
1. **Convert:** `afconvert`, which is built into macOS, decodes each file to 16 kHz mono. Chunks are joined and
   simultaneous tracks are overlaid.
2. **Transcribe:** `whisperkit-cli` transcribes the audio.
3. **Speakers:** WhisperKit separates the speakers in the same pass.
4. **Summary:** Ollama writes the debrief in one call, then the checks remove anything the transcript can't back up.

The code is in layers, and each layer only uses the ones below it:

```
debrief/
├── surfaces/       how you use it
│   ├── cli.py      `debrief`, `debrief setup`, `debrief <folder>`
│   └── tui/        the full-screen app (Textual): main screen, popups, progress view
├── pipeline/       the controller: Options and the Job that runs the steps on a background thread
├── features/       self-contained capabilities, each with a small public API in its __init__
│   ├── library/        finding meetings; reading and writing transcript.md / summary.md
│   ├── audio/          decoding, joining and overlaying tracks
│   ├── transcription/  running WhisperKit; building the speaker-labelled transcript
│   ├── summary/        the prompts, the Ollama client, and the hallucination checks
│   ├── export/         Slack formatting and the clipboard
│   └── setup/          resumable model downloads
└── core/           config, saved settings, errors, formatting
```

The summary prompts in [`features/summary/prompts.py`](debrief/features/summary/prompts.py) were tuned on real
interviews. A 9B model is sensitive to their wording, so test changes on real transcripts.

## Development

```sh
.venv/bin/python -m unittest discover -s tests
```

The tests never load a model: WhisperKit and Ollama are replaced by fakes, so the whole suite runs in well under a
second.

## License

MIT
