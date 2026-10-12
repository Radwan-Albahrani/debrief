"""A debrief of the meeting from a local Ollama model, checked against the transcript."""
from debrief.features.summary.checks import check_summary
from debrief.features.summary.ollama import SummaryStream, default_model, installed_models, strip_thinking
from debrief.features.summary.prompts import summary_context

__all__ = ["SummaryStream", "check_summary", "default_model", "installed_models", "strip_thinking", "summary_context"]
