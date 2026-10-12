"""Talking to the local Ollama server. Everything about its HTTP API stays in this module."""
import json
import re
import socket
import urllib.request
from collections.abc import Iterator

from debrief.core.config import OLLAMA_URL, SUMMARY_MODELS
from debrief.core.errors import DebriefError
from debrief.features.summary.prompts import SUMMARY_SYSTEM

# Ollama is on this machine: never route transcripts through an HTTP(S)_PROXY from the environment.
_local = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def installed_models() -> list[str] | None:
    """The models Ollama has, or None when it isn't running."""
    try:
        with _local.open(f"{OLLAMA_URL}/api/tags", timeout=3) as r:
            return [m["name"] for m in json.load(r)["models"]]
    except OSError:
        return None


def default_model(installed: list[str]) -> str | None:
    return next((m for m in SUMMARY_MODELS if m in installed), installed[0] if installed else None)


class SummaryStream:
    """One summary, streamed. Iterate it for the text as it is written; afterwards `stats` holds the token counts.
    `close()` may be called from another thread to abandon it, even while the model is still reading."""

    def __init__(self, model: str, transcript: str, task: str):
        context = min(131072, max(8192, (len(transcript) // 3 + 2500) // 1024 * 1024))
        self._body = json.dumps({
            "model": model, "stream": True, "think": False,
            # ponytail: hard cap on output; small models can loop on long transcripts without it
            "options": {"num_ctx": context, "temperature": 0, "num_predict": 1000},
            # the transcript goes before the task, so the long part of the prompt is the same for every meeting type
            "messages": [{"role": "system", "content": SUMMARY_SYSTEM},
                         {"role": "user", "content": f"Transcript:\n{transcript}\n\nTask: {task}"}],
        }).encode()
        self._response = None
        self.stats = {"prompt_tokens": None, "output_tokens": 0}

    def __iter__(self) -> Iterator[str]:
        request = urllib.request.Request(f"{OLLAMA_URL}/api/chat", self._body, {"Content-Type": "application/json"})
        try:
            with _local.open(request, timeout=1800) as self._response:
                for line in self._response:
                    message = json.loads(line)
                    if "error" in message:
                        raise DebriefError(f"Ollama: {message['error']}")
                    if piece := message.get("message", {}).get("content", ""):
                        self.stats["output_tokens"] += 1
                        yield piece
                    if message.get("done"):
                        self.stats = {"prompt_tokens": message.get("prompt_eval_count"),
                                      "output_tokens": message.get("eval_count", self.stats["output_tokens"])}
        except OSError as e:
            raise DebriefError(f"Ollama request failed: {e}") from e

    def close(self) -> None:
        """Shut the connection down; a read blocked on it in another thread fails immediately."""
        sock = getattr(getattr(getattr(self._response, "fp", None), "raw", None), "_sock", None)
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def strip_thinking(text: str) -> str:
    """`think: False` is requested, but some models still emit a reasoning block."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
