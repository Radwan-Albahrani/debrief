"""`debrief setup`: download the models, resuming where a previous attempt stopped."""
import json
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TimeRemainingColumn, TransferSpeedColumn

from debrief.core.config import (HUGGINGFACE_URL, MODELS_DIR, SPEAKER_MODEL_DIR, SUMMARY_MODELS, TOKENIZER_DIR,
                                 WHISPER_MODELS, WHISPERKIT_DIR)
from debrief.core.errors import DebriefError
from debrief.core.formatting import tilde
from debrief.features.summary import installed_models

STALL_SECONDS = 30  # no data for this long drops the connection and resumes it


def _listing(repo: str, folder: str = "") -> list[tuple[str, int]]:
    """(path, size) of every file in a Hugging Face repo, or in one folder of it."""
    url = f"{HUGGINGFACE_URL}/api/models/{repo}/tree/main" + (f"/{folder}" if folder else "") + "?recursive=true"
    with urllib.request.urlopen(url, timeout=60) as r:
        return [(f["path"], f["size"]) for f in json.load(r) if f["type"] == "file"]


def _download(repo: str, files: list[tuple[str, int]], root: Path, progress: Progress, task) -> None:
    """Download files that are missing or incomplete, resuming partial ones with a Range request."""
    have = {p: (root / p).stat().st_size if (root / p).exists() else 0 for p, _ in files}

    def done():
        return sum(min(have[p], size) for p, size in files)

    progress.update(task, total=sum(size for _, size in files), completed=done())
    for path, size in files:
        dest = root / path
        if not dest.resolve().is_relative_to(root.resolve()):  # paths come from the server: stay inside root
            raise DebriefError(f"refusing to write {path}: it points outside {root}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(10):
            if have[path] == size:
                break
            if have[path] > size:
                dest.unlink()
                have[path] = 0
            headers = {"Range": f"bytes={have[path]}-"} if have[path] else {}
            request = urllib.request.Request(f"{HUGGINGFACE_URL}/{repo}/resolve/main/{path}", headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=STALL_SECONDS) as r:
                    if have[path] and r.status != 206:  # the server ignored the Range: start over
                        have[path] = 0
                    with open(dest, "ab" if have[path] else "wb") as f:
                        while chunk := r.read(1 << 20):
                            f.write(chunk)
                            have[path] += len(chunk)
                            progress.update(task, completed=done())
            except OSError:
                time.sleep(min(2 * attempt, 10))
        if have[path] != size:
            raise DebriefError(f"could not download {repo}/{path}. Run `debrief setup` again to resume.")


def run_setup(whisper: str) -> None:
    console = Console()
    console.print(f"[bold]debrief setup[/]  [dim]models go to {tilde(MODELS_DIR)} (set DEBRIEF_MODELS to change)[/]\n")
    if not shutil.which("whisperkit-cli"):
        console.print("[yellow]whisperkit-cli is missing.[/] Install it with: [bold]brew install whisperkit-cli[/]\n")
    parts = [  # (label, repo, folder, file filter, destination)
        (f"Whisper {whisper}", "argmaxinc/whisperkit-coreml", WHISPER_MODELS[whisper][0], None, WHISPERKIT_DIR),
        ("Tokenizer", "openai/whisper-large-v3", "", r"^(config|tokenizer|tokenizer_config)\.json$", TOKENIZER_DIR),
        ("Speaker separation", "argmaxinc/speakerkit-coreml", "", r"pyannote-v[34]/|\.json$", SPEAKER_MODEL_DIR),
    ]
    columns = (TextColumn("{task.description:<20}"), BarColumn(bar_width=30), DownloadColumn(),
               TransferSpeedColumn(), TimeRemainingColumn())
    with Progress(*columns, console=console) as progress:
        for label, repo, folder, pattern, root in parts:
            task = progress.add_task(label, total=None)
            try:
                files = [(p, s) for p, s in _listing(repo, folder) if not pattern or re.search(pattern, p)]
            except OSError as e:
                raise DebriefError(f"could not reach Hugging Face ({e}). Check your connection and run setup again.")
            _download(repo, files, root, progress, task)

    model = SUMMARY_MODELS[0]
    if not shutil.which("ollama"):
        console.print("\n[yellow]Summaries need Ollama:[/] brew install ollama, then run [bold]debrief setup[/] again.")
    elif (installed := installed_models()) is None:
        console.print("\n[yellow]Ollama is installed but not running.[/] Start it (open the app, or "
                      "[bold]brew services start ollama[/]) and run [bold]debrief setup[/] again.")
    elif model not in installed:
        console.print(f"\nPulling the summary model [bold]{model}[/]…")
        subprocess.run(["ollama", "pull", model], check=False)
    console.print("\n[green]✓ Ready.[/] Run [bold]debrief[/].")
