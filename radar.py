#!/usr/bin/env python3
"""
radar — a live TUI for the GitHub PRs you recently opened.

Shows every open PR you authored within the last N hours (default 48) and keeps
the table refreshed every few seconds. For each PR it displays:

  repo · #number (clickable) · title · CI status · Copilot agent session ·
  open review threads · last commit

A little ASCII radar sweep spins in the header while it works; each tracked PR
shows up as a blip that lights up when the beam passes over it.

Data comes from a single GitHub GraphQL search call per refresh, made through the
already-authenticated `gh` CLI, so there is no token to manage here. The fetch runs
on a background thread so the sweep keeps spinning smoothly while data loads.

Usage:
  ./radar                 # last 48h, refresh every 5s
  ./radar --hours 12 --interval 10
  ./radar --no-drafts     # hide draft PRs

Quit with Ctrl-C.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

# Cap the content width so it sits as a centered block (with margins) on wide
# terminals, while still filling narrower ones.
MAX_CONTENT_WIDTH = 124

# A single search returns every field the table needs. statusCheckRollup may be
# null (no CI configured / no checks yet); reviewThreads is capped at 100, which
# is plenty for counting unresolved threads on a normal PR.
GRAPHQL_QUERY = """
query($q: String!) {
  search(query: $q, type: ISSUE, first: 100) {
    nodes {
      ... on PullRequest {
        number
        title
        url
        isDraft
        createdAt
        headRefName
        repository { name nameWithOwner }
        commits(last: 1) {
          nodes {
            commit {
              committedDate
              statusCheckRollup { state }
            }
          }
        }
        reviewThreads(first: 100) { nodes { isResolved } }
      }
    }
  }
}
"""

# Map a GraphQL StatusState to (symbol, label, rich-style).
CI_STATES = {
    "SUCCESS":  ("✓", "passing", "bold green"),
    "FAILURE":  ("✗", "failing", "bold red"),
    "ERROR":    ("✗", "error",   "bold red"),
    "PENDING":  ("●", "running", "bold yellow"),
    "EXPECTED": ("●", "queued",  "bold yellow"),
}
CI_NONE = ("–", "no checks", "dim")

# The Copilot coding agent runs as a GitHub Actions "dynamic" workflow run named
# exactly this; an in-progress run on a PR's head branch ≈ a live agent session.
# GitHub exposes no official session API yet (community request #185347), so we
# infer it from Actions runs.
COPILOT_RUN_NAME = "Running Copilot cloud agent"
COPILOT_ACTIVE_STATUSES = {"in_progress", "queued", "requested", "waiting", "pending"}
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# --- animation tuning ---------------------------------------------------------
FPS = 12                  # display refreshes per second
FRAME_DT = 1.0 / FPS
RADAR_R = 2               # radar radius, in text rows (grid is 2R+1 tall)
RADAR_PERIOD = 24         # frames per full sweep revolution (~2s at 12fps)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Live TUI for your recently opened GitHub PRs.")
    p.add_argument("--hours", type=int, default=48,
                   help="Only show PRs created within the last N hours (default: 48).")
    p.add_argument("--interval", type=float, default=5.0,
                   help="Refresh interval in seconds (default: 5).")
    p.add_argument("--no-drafts", action="store_true", help="Hide draft PRs.")
    return p.parse_args()


def iso_to_dt(value: str) -> datetime:
    """Parse a GitHub ISO-8601 'Z' timestamp into an aware datetime."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def humanize_age(dt: datetime, now: datetime) -> str:
    """Compact relative age, e.g. '12s', '4m', '3h', '2d' ago."""
    seconds = max(0, int((now - dt).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def fetch_prs(hours: int, include_drafts: bool) -> list[dict]:
    """Run the GraphQL search through `gh` and return the PR nodes (newest first)."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = f"is:pr is:open author:@me created:>={since} sort:created-desc"
    if not include_drafts:
        query += " draft:false"

    proc = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={GRAPHQL_QUERY}", "-f", f"q={query}"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "gh api graphql failed")

    payload = json.loads(proc.stdout)
    if "errors" in payload:
        raise RuntimeError("; ".join(e.get("message", str(e)) for e in payload["errors"]))
    return payload["data"]["search"]["nodes"]


def copilot_active_branches(repo: str) -> set[str]:
    """Head branches in `repo` (owner/name) with a live Copilot coding-agent run."""
    path = (f"/repos/{repo}/actions/runs"
            "?event=dynamic&per_page=50&exclude_pull_requests=true")
    try:
        proc = subprocess.run(["gh", "api", path],
                              capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            return set()
        runs = json.loads(proc.stdout).get("workflow_runs", [])
    except Exception:
        return set()  # Actions disabled, no access, or timeout → treat as no session
    return {r["head_branch"] for r in runs
            if r.get("name") == COPILOT_RUN_NAME
            and r.get("status") in COPILOT_ACTIVE_STATUSES
            and r.get("head_branch")}


def annotate_copilot_sessions(prs: list[dict]) -> None:
    """Tag each PR with `copilotRunning`; one Actions query per distinct repo."""
    by_repo: dict[str, set[str]] = {}
    for pr in prs:
        repo = pr["repository"]["nameWithOwner"]
        if repo not in by_repo:
            by_repo[repo] = copilot_active_branches(repo)
        pr["copilotRunning"] = pr.get("headRefName") in by_repo[repo]


class Fetcher:
    """Runs fetch_prs on a daemon thread so the UI never blocks on the network."""

    def __init__(self, hours: int, include_drafts: bool):
        self.hours = hours
        self.include_drafts = include_drafts
        self._lock = threading.Lock()
        self._pending: tuple[list[dict] | None, str | None] | None = None
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.busy:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            prs = fetch_prs(self.hours, self.include_drafts)
            annotate_copilot_sessions(prs)
            result: tuple[list[dict] | None, str | None] = (prs, None)
        except Exception as exc:  # surfaced in the UI, thread stays alive-safe
            result = (None, str(exc))
        with self._lock:
            self._pending = result

    def take(self) -> tuple[list[dict] | None, str | None] | None:
        """Return the latest finished result once, else None."""
        with self._lock:
            result, self._pending = self._pending, None
            return result


# --- radar animation ----------------------------------------------------------
def _sweep_char(angle: float) -> str:
    """Beam glyph for the octant the sweep currently points at (y grows downward)."""
    return "─╲│╱─╲│╱"[int(round(angle / (math.pi / 4))) % 8]


def radar_grid(frame: int, prs: list[dict]) -> list[list[tuple[str, str]]]:
    """Build the radar as a grid of (char, rich-style) cells."""
    R, rows, cols = RADAR_R, 2 * RADAR_R + 1, 4 * RADAR_R + 1
    cx, cy = 2 * R, R
    angle = (frame % RADAR_PERIOD) / RADAR_PERIOD * 2 * math.pi
    grid = [[(" ", "") for _ in range(cols)] for _ in range(rows)]

    # faint range ring (x compressed by 2 so the circle looks round)
    for y in range(rows):
        for x in range(cols):
            if abs(math.hypot((x - cx) / 2.0, y - cy) - R) < 0.5:
                grid[y][x] = ("·", "green dim")

    # one blip per PR, at a stable spot derived from its number; lights up on ping
    for i, pr in enumerate(prs):
        num = pr.get("number", i)
        ba = (num * 2.39996) % (2 * math.pi)        # golden-angle spread
        br = 1 + (num % R)                          # radius 1..R
        bx = int(round(cx + 2 * br * math.cos(ba)))
        by = int(round(cy + br * math.sin(ba)))
        if 0 <= by < rows and 0 <= bx < cols:
            delta = abs(((angle - ba + math.pi) % (2 * math.pi)) - math.pi)
            grid[by][bx] = ("•", "bold yellow" if delta < 0.5 else "green")

    # sweep beam: a faint trailing line then the bright leading line on top
    for lag, style in ((-0.5, "green"), (0.0, "bold bright_green")):
        a = angle + lag * (2 * math.pi / RADAR_PERIOD)
        ch = _sweep_char(a)
        steps = R * 4
        for s in range(1, steps + 1):
            rr = R * s / steps
            x = int(round(cx + 2 * rr * math.cos(a)))
            y = int(round(cy + rr * math.sin(a)))
            if 0 <= y < rows and 0 <= x < cols and (x, y) != (cx, cy) and grid[y][x][0] != "•":
                grid[y][x] = (ch, style)

    grid[cy][cx] = ("◉", "bold green")
    return grid


def build_radar(frame: int, prs: list[dict]) -> Text:
    radar = Text()
    grid = radar_grid(frame, prs)
    for i, row in enumerate(grid):
        for ch, style in row:
            radar.append(ch, style=style)
        if i < len(grid) - 1:
            radar.append("\n")
    return radar


def build_header(prs: list[dict] | None, error: str | None,
                 hours: int, interval: float, frame: int, now: datetime) -> Table:
    info = Text("\n")  # leading blank line nudges text toward radar's vertical center
    info.append("radar", style="bold bright_green")
    info.append("  ·  my open PRs", style="bold")
    info.append(f"  ·  last {hours}h\n", style="dim")

    if error is not None:
        info.append("⚠ ", style="bold red")
        info.append(error if len(error) < 60 else error[:57] + "…", style="red")
        info.append("\n")
        if prs:
            info.append("showing last good data · ", style="dim")
    elif prs is None:
        info.append("scanning…\n", style="green")
    else:
        n = len(prs)
        info.append(f"tracking {n} PR{'s' if n != 1 else ''}", style="green")
        info.append(f"  ·  refreshed {now.astimezone().strftime('%H:%M:%S')}\n", style="dim")
    info.append(f"every {interval:g}s · Ctrl-C to quit", style="dim")

    header = Table.grid(padding=(0, 3))
    header.add_column()
    header.add_column()
    header.add_row(build_radar(frame, prs or []), info)
    return header


def ci_cell(pr: dict) -> Text:
    commits = pr["commits"]["nodes"]
    rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
    state = rollup["state"] if rollup else None
    symbol, label, style = CI_STATES.get(state, CI_NONE)
    return Text(f"{symbol} {label}", style=style)


def copilot_cell(pr: dict, frame: int) -> Text:
    """Whether a Copilot coding-agent session is live on this PR (animated spinner)."""
    if pr.get("copilotRunning"):
        spin = SPINNER_FRAMES[(frame // 2) % len(SPINNER_FRAMES)]
        return Text(f"{spin} working", style="bold magenta")
    return Text("–", style="dim")


def threads_cell(pr: dict) -> Text:
    unresolved = sum(1 for t in pr["reviewThreads"]["nodes"] if not t["isResolved"])
    if unresolved == 0:
        return Text("0", style="dim")
    return Text(str(unresolved), style="bold magenta")


def commit_cell(pr: dict, now: datetime) -> Text:
    commits = pr["commits"]["nodes"]
    if not commits:
        return Text("—", style="dim")
    age = humanize_age(iso_to_dt(commits[0]["commit"]["committedDate"]), now)
    return Text(age)


def pr_number_cell(pr: dict) -> Text:
    """The PR number as a clickable OSC-8 hyperlink to the PR on GitHub."""
    return Text(f"#{pr['number']}", style=f"bold cyan underline link {pr['url']}")


def build_table(prs: list[dict], now: datetime, width: int, frame: int) -> Table:
    table = Table(
        width=width,
        header_style="bold",
        row_styles=["", "on grey7"],
        border_style="grey50",
        pad_edge=False,
    )
    table.add_column("repo", no_wrap=True, style="green")
    table.add_column("PR", no_wrap=True, justify="right")
    table.add_column("title", ratio=1, no_wrap=True, overflow="ellipsis")
    table.add_column("CI", no_wrap=True)
    table.add_column("copilot", no_wrap=True)
    table.add_column("threads", justify="right", no_wrap=True)
    table.add_column("last commit", no_wrap=True, justify="right", style="dim")

    for pr in prs:
        title = pr["title"]
        if pr.get("isDraft"):
            title = "[dim italic]draft[/dim italic] " + title
        table.add_row(
            pr["repository"]["name"],
            pr_number_cell(pr),
            Text.from_markup(title),
            ci_cell(pr),
            copilot_cell(pr, frame),
            threads_cell(pr),
            commit_cell(pr, now),
        )
    return table


def render(console: Console, prs: list[dict] | None, error: str | None,
           hours: int, interval: float, frame: int) -> Align:
    now = datetime.now(timezone.utc)
    width = min(console.width, MAX_CONTENT_WIDTH)
    header = Align.center(build_header(prs, error, hours, interval, frame, now), width=width)

    if prs:
        body: object = Group(header, Text(""), build_table(prs, now, width, frame))
    elif error is None and prs is None:
        body = header                       # initial load; header says "scanning…"
    elif error is None:                     # loaded, but nothing in range
        note = Align.center(Text(f"No open PRs you authored in the last {hours}h.",
                                 style="dim italic"), width=width)
        body = Group(header, Text(""), note)
    else:
        body = header                       # first fetch failed; header carries the error

    # Center the whole block horizontally and vertically within the screen.
    return Align.center(body, width=console.width, height=console.height, vertical="middle")


def main() -> int:
    args = parse_args()
    console = Console()
    fetcher = Fetcher(args.hours, include_drafts=not args.no_drafts)

    prs: list[dict] | None = None
    error: str | None = None
    fetcher.start()
    last_fetch = time.monotonic()
    frame = 0

    try:
        with Live(console=console, screen=True, auto_refresh=False,
                  redirect_stdout=False, redirect_stderr=False) as live:
            while True:
                done = fetcher.take()
                if done is not None:
                    new_prs, err = done
                    if err is not None:
                        error = err          # keep last-good prs on screen
                    else:
                        prs, error = new_prs, None

                if not fetcher.busy and time.monotonic() - last_fetch >= args.interval:
                    fetcher.start()
                    last_fetch = time.monotonic()

                live.update(render(console, prs, error, args.hours, args.interval, frame),
                            refresh=True)
                frame += 1
                time.sleep(FRAME_DT)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
