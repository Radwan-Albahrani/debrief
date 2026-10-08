# debrief

Meeting transcripts with speaker labels, plus a short summary you can paste into Slack. Everything runs on
your Mac: no API keys, no uploads, no subscription.

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

- **Whisper large-v3** on the Neural Engine via [WhisperKit](https://github.com/argmaxinc/WhisperKit)
  (large-v3 turbo is available when speed matters more)
- **Speaker labels** (Speaker A, B, C…) via pyannote, which adds about 5 seconds per meeting
- **Summaries** from a local [Ollama](https://ollama.com) model (`qwen3.5:9b`), with a "Job interview" mode
  (strengths, concerns, logistics)
- Built for **mixed-language** meetings: we use it for interviews that switch between Arabic and English
- A full-screen terminal app that is keyboard driven, with live progress and a clean exit

## Install

Needs an Apple Silicon Mac and [Homebrew](https://brew.sh).

```sh
git clone https://github.com/Radwan-Albahrani/debrief.git
cd debrief
./install.sh
```

`install.sh` does the following:
1. Installs `whisperkit-cli` and `ollama` with Homebrew.
2. Creates a local `.venv`.
3. Puts a `debrief` command in `~/.local/bin`.
4. Downloads the models: Whisper large-v3 (about 3 GB), speaker separation (36 MB) and `qwen3.5:9b`
   (6.6 GB).

Downloads resume where they stopped, so if one stalls on a slow connection just run `debrief setup` again.

## Use

Put each meeting in its own folder inside `~/Documents/Meetings`. A folder of chunks (for example
`audio-000.caf`, `audio-001.caf`, …) counts as one meeting, and separate simultaneous tracks such as
`applicationAudio-000.caf` + `microphone-000.caf` are overlaid into one timeline. A single audio file works too.
Then run:

```sh
debrief
```

| Key | |
|---|---|
| `enter` | transcribe and summarize the selected meeting |
| `tab` | switch between summary and transcript |
| `c` | copy the summary, formatted for Slack |
| `r` | redo just the summary (about a minute, no re-transcription) |
| `/` | search |
| `,` | settings: recordings folders, Whisper model, language, speakers, summary model, meeting type |
| `esc` | cancel a run |
| `q` | quit |

Each meeting folder gets `transcript.md`, `transcript.srt` and `summary.md`.

For scripts, there is also a one-shot mode with no UI:

```sh
debrief ~/Documents/Meetings/standup -w turbo -p "weekly standup"
```

## Benchmarks

Measured on two real interviews on an M5 MacBook Pro with 32 GB of RAM:

| | 48:20 interview | 39:00 interview |
|---|---|---|
| Whisper large-v3 | 8:37 (5.6× realtime) | 8:23 (4.6×) |
| Whisper large-v3 turbo | 2:08 (22.6×) | 2:17 (17.1×) |
| Speaker labels | 0:05 | 0:04 |
| Summary (`qwen3.5:9b`) | about 0:55 | about 0:50 |

**Why large-v3 is the default:** on Arabic speech, large-v3 writes an English translation while turbo writes
the Arabic down word for word. Turbo is 4× faster, but any Arabic name it mishears ends up in the summary as
fact. Summaries built from the large-v3 transcripts were noticeably more accurate. On English-only meetings,
turbo is an easy win: switch it under `,` → `w` after running `debrief setup -w turbo`.

Treat summaries as a first draft. A 9B model still occasionally keeps a misheard name, so skim names and
numbers before you post.

## Configuration

| | |
|---|---|
| `DEBRIEF_MODELS` | where models are stored (default `~/Documents/huggingface`, the same place WhisperKit uses) |
| `DEBRIEF_BIN` | where `install.sh` puts the command (default `~/.local/bin`) |
| `~/.local/share/debrief/state.json` | your settings and recordings folders |

## How it works

1. `afconvert` (built into macOS) joins the chunks into one 16 kHz mono file.
2. `whisperkit-cli` transcribes it and separates the speakers in a single pass.
3. Ollama writes the summary from the speaker-labelled transcript.

The whole app is one Python file, `debrief.py`.

## License

MIT
