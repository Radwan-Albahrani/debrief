"""Getting a summary into Slack."""
import re
import subprocess

from debrief.features.library import body_of


def to_slack(markdown: str) -> str:
    """Markdown -> Slack mrkdwn: headings and **bold** become *bold*, bullets become •. Title and footer are dropped."""
    lines = []
    for line in body_of(markdown).splitlines():
        line = line.rstrip()
        if m := re.match(r"#+\s*(.+)", line):
            line = f"*{m[1].strip()}*"
        else:
            line = re.sub(r"^(\s*)[-*]\s+", r"\1• ", line)
            line = re.sub(r"\*\*(.+?)\*\*", r"*\1*", line)
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def copy_to_clipboard(text: str) -> None:
    subprocess.run(["pbcopy"], input=text, text=True, check=True)
