"""Every knob debrief has, in one place: where things live, which models it uses, and the choices it offers."""
import os
from pathlib import Path

# ── where things live (override with environment variables) ──────────────────
MODELS_DIR = Path(os.environ.get("DEBRIEF_MODELS", "~/Documents/huggingface")).expanduser()
STATE_FILE = Path(os.environ.get("DEBRIEF_HOME", "~/.local/share/debrief")).expanduser() / "state.json"
DEFAULT_LIBRARY = "~/Documents/Meetings"

# ── models ───────────────────────────────────────────────────────────────────
WHISPERKIT_DIR = MODELS_DIR / "models/argmaxinc/whisperkit-coreml"
SPEAKER_MODEL_DIR = MODELS_DIR / "models/argmaxinc/speakerkit-coreml"
TOKENIZER_DIR = MODELS_DIR / "models/openai/whisper-large-v3"
WHISPER_MODELS = {  # setting -> (WhisperKit model folder, label)
    "large-v3": ("openai_whisper-large-v3", "large-v3 (most accurate)"),
    "turbo": ("openai_whisper-large-v3-v20240930_turbo", "large-v3 turbo (faster)"),
}
SUMMARY_MODELS = ["qwen3.5:9b", "qwen3-vl:8b-instruct", "qwen3-vl:8b"]  # the first one installed is the default
OLLAMA_URL = "http://localhost:11434"
HUGGINGFACE_URL = "https://huggingface.co"

# ── choices ──────────────────────────────────────────────────────────────────
AUDIO_EXTENSIONS = {".caf", ".wav", ".m4a", ".mp3", ".mp4", ".aac", ".aiff", ".aif", ".flac"}
LANGUAGES = {"auto": "Auto-detect", "en": "English", "ar": "Arabic"}
SPEAKER_CHOICES = {"auto": "Auto-detect", "2": "2 people", "3": "3 people", "4": "4 people", "5": "5 people",
                   "off": "Off"}
MEETING_KINDS = ["General meeting", "Job interview", "1:1", "Client call"]
DEFAULTS = {"whisper": "large-v3", "lang": "auto", "speakers": "auto", "kind": "General meeting"}
