"""The live view of a running job: one row per step, plus resource use and the latest log lines."""
import subprocess
import time

from rich.console import Group, RenderableType
from rich.progress_bar import ProgressBar
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from debrief.core.formatting import clock, filesize
from debrief.pipeline import Job

ICONS = {"wait": Text("·", style="dim"), "done": Text("✓", style="green"), "fail": Text("✗", style="red"),
         "skip": Text("–", style="dim")}
RUNNING = Spinner("dots", style="bold cyan")


def process_stats(pid: int) -> tuple[float, int] | None:
    """(CPU %, resident bytes) of a process, or None once it has exited."""
    out = subprocess.run(["ps", "-o", "%cpu=,rss=", "-p", str(pid)], capture_output=True, text=True).stdout.split()
    return (float(out[0]), int(out[1]) * 1024) if len(out) == 2 else None


def render_job(job: Job, stats: tuple[float, int] | None = None) -> RenderableType:
    now = time.time()
    grid = Table.grid(padding=(0, 2))
    for _ in range(5):
        grid.add_column()
    for name, step in job.steps.items():
        elapsed = clock((step.ended or now) - step.started) if step.started else ""
        if step.state == "skip":
            bar, note = Text(""), Text("off", style="dim")
        else:
            bar = ProgressBar(total=1, completed=step.progress or (1 if step.state == "done" else 0), width=26,
                              pulse=step.progress is None and step.state == "run",
                              complete_style="cyan", finished_style="green", style="grey23")
            note = Text(step.note or ("waiting" if step.state == "wait" else ""),
                        style="" if step.state == "run" else "dim")
        icon = RUNNING if step.state == "run" else ICONS[step.state]
        name_style = {"run": "bold", "wait": "dim", "skip": "dim"}.get(step.state, "")
        grid.add_row(icon, Text(name, style=name_style), bar, Text(elapsed, style="dim"), note)

    total = clock((job.ended or now) - job.started)
    if job.running:
        head = Text.assemble(("● ", "cyan"), ("Running ", "bold"), (f"{total} elapsed", "dim"))
    elif job.error:
        head = Text.assemble(("✗ ", "red"), (job.error.splitlines()[0][:120], "bold red"))
    else:
        speed = f" · {clock(job.audio_seconds)} of audio · {job.audio_seconds / max(job.ended - job.started, 1):.1f}× " \
                f"realtime" if job.audio_seconds else ""
        head = Text.assemble(("✓ ", "green"), (f"Done in {total}", "bold"), (speed, "dim"))

    parts = [head, "", grid]
    if stats and job.proc:
        cpu, rss = stats
        parts += ["", Text.assemble(("whisperkit  ", "dim"), f"CPU {cpu:.0f}%  ·  RAM {filesize(rss)}  ·  ",
                                    ("pid ", "dim"), str(job.proc.pid))]
    if job.log and job.running:
        parts += ["", Text("\n".join(job.log), style="grey50", overflow="ellipsis", no_wrap=True)]
    return Group(*parts)
