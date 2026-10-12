"""Speech to a speaker-labelled transcript, with WhisperKit on the Neural Engine."""
from debrief.features.transcription.transcript import UNCLEAR, build_transcript, speaker_count
from debrief.features.transcription.whisperkit import WhisperKitResult, model_path, ready, run

__all__ = ["UNCLEAR", "WhisperKitResult", "build_transcript", "model_path", "ready", "run", "speaker_count"]
