#!/usr/bin/env python3
"""Generate terminal-style GitHub stats SVG cards.

Fetches profile data via the GitHub GraphQL API (stars, followers,
languages, PRs, issues, commit contributions) and renders deterministic,
dependency-free SVG cards into assets/.

Usage:
    GITHUB_TOKEN=<token> python3 scripts/generate_stats.py [username]

Only the Python standard library is used.

Accuracy notes (with the standard GITHUB_TOKEN):
  - repositories / stars / followers / PRs / issues: exact, public data.
  - commits: GitHub's contribution count (default-branch commits, summed
    per calendar year) — matches the profile graph, not `git log` totals.
    Private contributions are invisible to the token and are not guessed.
  - languages: Linguist byte share across owned non-fork public repos.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

GRAPHQL_URL = "https://api.github.com/graphql"
DEFAULT_USERNAME = "Mr-Don-Leo"
ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5

# ---------------------------------------------------------------------------
# GitHub API access
# ---------------------------------------------------------------------------

PROFILE_QUERY = """
query($login: String!) {
  user(login: $login) {
    login
    createdAt
    followers { totalCount }
    publicRepos: repositories(privacy: PUBLIC, ownerAffiliations: OWNER) { totalCount }
    pullRequests { totalCount }
    issues { totalCount }
  }
}
"""

REPOS_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    repositories(
      first: 100
      after: $cursor
      privacy: PUBLIC
      ownerAffiliations: OWNER
      orderBy: {field: PUSHED_AT, direction: DESC}
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        isFork
        stargazerCount
        pushedAt
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name } }
        }
      }
    }
  }
}
"""

CONTRIBUTIONS_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      contributionCalendar {
        weeks {
          contributionDays { date contributionCount }
        }
      }
    }
  }
}
"""


class GitHubAPIError(RuntimeError):
    """Raised when the GitHub API returns an unrecoverable error."""


def graphql(token: str, query: str, variables: dict) -> dict:
    """Run a GraphQL query with basic retry handling for transient errors."""
    payload = json.dumps({"query": query, "variables": variables}).encode()
    request = urllib.request.Request(
        GRAPHQL_URL,
        data=payload,
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "github-stats-card-generator",
        },
    )

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.load(response)
            if "errors" in body:
                messages = "; ".join(e.get("message", "?") for e in body["errors"])
                raise GitHubAPIError(f"GraphQL errors: {messages}")
            return body["data"]
        except urllib.error.HTTPError as err:
            if err.code in (403, 429):
                reset = err.headers.get("X-RateLimit-Reset", "")
                raise GitHubAPIError(
                    f"Rate limited or forbidden (HTTP {err.code}). "
                    f"Rate limit resets at epoch {reset or 'unknown'}."
                ) from err
            if err.code >= 500 and attempt < MAX_RETRIES:
                last_error = err
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            raise GitHubAPIError(f"HTTP {err.code}: {err.reason}") from err
        except urllib.error.URLError as err:
            if attempt < MAX_RETRIES:
                last_error = err
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            raise GitHubAPIError(f"Network error: {err.reason}") from err
    raise GitHubAPIError(f"Giving up after {MAX_RETRIES} attempts: {last_error}")


def fetch_repositories(token: str, login: str) -> list[dict]:
    """Fetch all public owned repositories, following pagination cursors."""
    repos: list[dict] = []
    cursor = None
    while True:
        data = graphql(token, REPOS_QUERY, {"login": login, "cursor": cursor})
        connection = data["user"]["repositories"]
        repos.extend(connection["nodes"])
        if not connection["pageInfo"]["hasNextPage"]:
            return repos
        cursor = connection["pageInfo"]["endCursor"]


def fetch_contributions(token: str, login: str, created_at: str) -> tuple[int, dict[str, int]]:
    """Fetch commit totals and per-day contribution counts since creation.

    contributionsCollection only accepts a range of at most one year, so the
    account lifetime is split into calendar-year windows. Returns the summed
    commit contributions plus a date -> contribution-count map. The calendar
    pads each window to full weeks, so adjacent windows overlap by a few days;
    keying by date deduplicates them.
    """
    created_year = int(created_at[:4])
    now = datetime.now(timezone.utc)
    commits = 0
    day_counts: dict[str, int] = {}
    for year in range(created_year, now.year + 1):
        start = f"{year}-01-01T00:00:00Z"
        end = f"{year}-12-31T23:59:59Z"
        data = graphql(
            token, CONTRIBUTIONS_QUERY, {"login": login, "from": start, "to": end}
        )
        collection = data["user"]["contributionsCollection"]
        commits += collection["totalCommitContributions"]
        for week in collection["contributionCalendar"]["weeks"]:
            for day in week["contributionDays"]:
                day_counts[day["date"]] = day["contributionCount"]
    return commits, day_counts


def compute_streaks(day_counts: dict[str, int], today: str) -> dict:
    """Derive total contributions and current/longest streaks from day counts.

    A streak is consecutive calendar days with at least one contribution. The
    current streak is counted back from today; a zero for today itself does
    not break it (the day isn't over yet).
    """
    days = sorted((d, c) for d, c in day_counts.items() if d <= today)
    total = sum(c for _, c in days)

    longest = {"days": 0, "start": None, "end": None}
    run_start = None
    run_len = 0
    for date, count in days:
        if count > 0:
            if run_len == 0:
                run_start = date
            run_len += 1
            if run_len > longest["days"]:
                longest = {"days": run_len, "start": run_start, "end": date}
        else:
            run_len = 0

    index = len(days) - 1
    if index >= 0 and days[index][1] == 0:
        index -= 1
    end_index = index
    while index >= 0 and days[index][1] > 0:
        index -= 1
    current_len = end_index - index
    current = {
        "days": current_len,
        "start": days[index + 1][0] if current_len else None,
        "end": days[end_index][0] if current_len else None,
    }

    return {"total": total, "current": current, "longest": longest}


def collect_stats(token: str, username: str) -> dict:
    """Gather every statistic shown on the cards into a plain dict."""
    profile = graphql(token, PROFILE_QUERY, {"login": username})["user"]
    repos = fetch_repositories(token, username)
    commits, day_counts = fetch_contributions(token, username, profile["createdAt"])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    streaks = compute_streaks(day_counts, today)

    source_repos = [r for r in repos if not r["isFork"]]

    languages: dict[str, int] = {}
    for repo in source_repos:
        for edge in repo["languages"]["edges"]:
            name = edge["node"]["name"]
            languages[name] = languages.get(name, 0) + edge["size"]

    recent = sorted(
        (r for r in repos if r["pushedAt"]),
        key=lambda r: (r["pushedAt"], r["name"]),
        reverse=True,
    )[:5]

    def primary_language(repo: dict) -> str:
        edges = repo["languages"]["edges"]
        return edges[0]["node"]["name"] if edges else "—"

    return {
        "login": profile["login"],
        "since": profile["createdAt"][:4],
        "contributions": streaks["total"],
        "current_streak": streaks["current"],
        "longest_streak": streaks["longest"],
        "repositories": profile["publicRepos"]["totalCount"],
        "followers": profile["followers"]["totalCount"],
        "stars": sum(r["stargazerCount"] for r in repos),
        "commits": commits,
        "pull_requests": profile["pullRequests"]["totalCount"],
        "issues": profile["issues"]["totalCount"],
        # Sorted by bytes desc, then name, for deterministic output.
        "languages": sorted(languages.items(), key=lambda kv: (-kv[1], kv[0]))[:5],
        "recent": [
            {
                "name": r["name"],
                "pushed": r["pushedAt"][:10],
                "language": primary_language(r),
                "stars": r["stargazerCount"],
            }
            for r in recent
        ],
    }


# ---------------------------------------------------------------------------
# SVG rendering
# ---------------------------------------------------------------------------

# Layout constants (pixels). CHAR_WIDTH matches a 14px monospace advance
# closely enough for the fixed-width tree/bar alignment used below.
FONT_SIZE = 14
LINE_HEIGHT = 24
CHAR_WIDTH = 8.4
PAD_X = 28
CARD_WIDTH = 840

COLORS = {
    "bg": "#0b0e14",
    "border": "#00e5a0",
    "frame": "#1c2333",
    "title": "#00e5a0",
    "section": "#ff2e88",
    "tree": "#3d4a63",
    "label": "#8b9bb4",
    "value": "#e6edf3",
    "bar_fill": "#00b4ff",
    "bar_empty": "#1c2333",
    "prompt": "#00e5a0",
    "muted": "#4a5772",
}

BAR_CHARS = 24
# Character column where the right-hand section of the wide card starts.
RIGHT_COL = 46


def text(x: float, y: int, content: str, fill: str, bold: bool = False) -> str:
    weight = ' font-weight="bold"' if bold else ""
    return (
        f'<text x="{x:g}" y="{y}" fill="{fill}"{weight} xml:space="preserve">'
        f"{escape(content)}</text>"
    )


def col(chars: float) -> float:
    """Convert a character column to an x pixel offset inside the padding."""
    return PAD_X + chars * CHAR_WIDTH


def card(title: str, aria: str, height: int, body: list[str]) -> str:
    """Wrap rendered body fragments in the shared terminal-window chrome."""
    body_svg = "\n".join("  " + line for line in body)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{CARD_WIDTH}" height="{height}" viewBox="0 0 {CARD_WIDTH} {height}" role="img" aria-label="{escape(aria)}">
  <style>
    text {{
      font-family: 'JetBrains Mono', 'Fira Code', 'SF Mono', Menlo, Consolas, 'DejaVu Sans Mono', monospace;
      font-size: {FONT_SIZE}px;
    }}
    .cursor {{ animation: blink 1.2s step-end infinite; }}
    @keyframes blink {{ 50% {{ opacity: 0; }} }}
  </style>
  <rect x="1.5" y="1.5" width="{CARD_WIDTH - 3}" height="{height - 3}" rx="12" fill="{COLORS['bg']}" stroke="{COLORS['border']}" stroke-opacity="0.55" stroke-width="1.5"/>
  <circle cx="{PAD_X}" cy="30" r="5" fill="#ff5f56"/>
  <circle cx="{PAD_X + 18}" cy="30" r="5" fill="#ffbd2e"/>
  <circle cx="{PAD_X + 36}" cy="30" r="5" fill="#27c93f"/>
  <text x="{PAD_X + 54}" y="35" fill="{COLORS['title']}" font-weight="bold">{escape(title)}</text>
  <line x1="{PAD_X - 12}" y1="48" x2="{CARD_WIDTH - PAD_X + 12}" y2="48" stroke="{COLORS['frame']}" stroke-width="1"/>
{body_svg}
</svg>
"""


def render_stats_card(stats: dict, generated_at: datetime) -> str:
    """Wide main card: activity tree on the left, language bars on the right."""
    body: list[str] = []
    top = 76  # first baseline below the header bar

    # -- left column: activity tree ----------------------------------------
    body.append(text(col(0), top, "github.activity", COLORS["section"], bold=True))
    rows = [
        ("repositories", stats["repositories"]),
        ("stars", stats["stars"]),
        ("commits", stats["commits"]),
        ("pull_requests", stats["pull_requests"]),
        ("issues", stats["issues"]),
        ("followers", stats["followers"]),
    ]
    for index, (label, value) in enumerate(rows):
        y = top + LINE_HEIGHT * (index + 1)
        branch = "└─" if index == len(rows) - 1 else "├─"
        body.append(text(col(0), y, branch, COLORS["tree"]))
        body.append(text(col(3), y, label, COLORS["label"]))
        body.append(text(col(17), y, f"{value:>7,}", COLORS["value"], bold=True))

    # -- right column: language bars ---------------------------------------
    body.append(text(col(RIGHT_COL), top, "languages", COLORS["section"], bold=True))
    total_bytes = sum(size for _, size in stats["languages"]) or 1
    for index, (name, size) in enumerate(stats["languages"]):
        y = top + LINE_HEIGHT * (index + 1)
        share = size / total_bytes
        filled = max(1, round(share * BAR_CHARS))
        body.append(text(col(RIGHT_COL), y, f"{name[:12]:<13}", COLORS["label"]))
        body.append(text(col(RIGHT_COL + 13), y, "█" * filled, COLORS["bar_fill"]))
        body.append(
            text(col(RIGHT_COL + 13 + filled), y, "░" * (BAR_CHARS - filled), COLORS["bar_empty"])
        )
        body.append(
            text(col(RIGHT_COL + 13 + BAR_CHARS + 2), y, f"{share * 100:4.1f}%", COLORS["value"])
        )
    if not stats["languages"]:
        body.append(text(col(RIGHT_COL), top + LINE_HEIGHT, "no language data", COLORS["muted"]))

    # -- bottom line: prompt + blinking cursor left, timestamp right -------
    prompt_y = top + LINE_HEIGHT * (len(rows) + 1) + 10
    prompt = f"{stats['login'].lower()}@github:~$"
    body.append(text(col(0), prompt_y, prompt, COLORS["prompt"], bold=True))
    body.append(
        f'<rect class="cursor" x="{col(len(prompt) + 1):g}" y="{prompt_y - 12}" '
        f'width="8" height="15" fill="{COLORS["prompt"]}"/>'
    )
    stamp = generated_at.strftime("%Y-%m-%d %H:%M UTC")
    body.append(
        f'<text x="{CARD_WIDTH - PAD_X}" y="{prompt_y}" fill="{COLORS["muted"]}" '
        f'text-anchor="end" font-size="11">last sync: {escape(stamp)}</text>'
    )

    title = f"{stats['login'].upper()} // SYSTEM MONITOR"
    aria = f"GitHub statistics for {stats['login']}"
    return card(title, aria, prompt_y + 22, body)


def render_activity_card(stats: dict) -> str:
    """Recent-pushes card: repo name, primary language, stars, push date."""
    body: list[str] = []
    top = 76
    rows = stats["recent"]

    for index, repo in enumerate(rows):
        y = top + LINE_HEIGHT * index
        body.append(text(col(0), y, "→", COLORS["tree"]))
        body.append(text(col(2), y, repo["name"][:30], COLORS["value"], bold=True))
        body.append(text(col(34), y, repo["language"][:14], COLORS["label"]))
        body.append(text(col(50), y, f"★ {repo['stars']:>4,}", COLORS["bar_fill"]))
        body.append(
            f'<text x="{CARD_WIDTH - PAD_X}" y="{y}" fill="{COLORS["muted"]}" '
            f'text-anchor="end" xml:space="preserve">{escape(repo["pushed"])}</text>'
        )
    if not rows:
        body.append(text(col(0), top, "no recent activity", COLORS["muted"]))

    height = top + LINE_HEIGHT * max(len(rows) - 1, 0) + 26
    title = "GIT.LOG // RECENT PUSHES"
    aria = f"Recently active repositories of {stats['login']}"
    return card(title, aria, height, body)


def render_streak_card(stats: dict) -> str:
    """Compact card: total contributions plus current and longest streak."""

    def span(streak: dict) -> str:
        if not streak["days"]:
            return "—"
        return f"{streak['start']} → {streak['end']}"

    def days(streak: dict) -> str:
        return f"{streak['days']:,} day" + ("" if streak["days"] == 1 else "s")

    columns = [
        (f"{stats['contributions']:,}", "total contributions", f"since {stats['since']}"),
        (days(stats["current_streak"]), "current streak", span(stats["current_streak"])),
        (days(stats["longest_streak"]), "longest streak", span(stats["longest_streak"])),
    ]

    body: list[str] = []
    height = 168
    for index, (value, label, detail) in enumerate(columns):
        x = CARD_WIDTH * (2 * index + 1) / 6
        body.append(
            f'<text x="{x:g}" y="98" fill="{COLORS["value"]}" font-weight="bold" '
            f'font-size="26" text-anchor="middle">{escape(value)}</text>'
        )
        body.append(
            f'<text x="{x:g}" y="124" fill="{COLORS["label"]}" '
            f'text-anchor="middle">{escape(label)}</text>'
        )
        body.append(
            f'<text x="{x:g}" y="146" fill="{COLORS["muted"]}" font-size="11" '
            f'text-anchor="middle">{escape(detail)}</text>'
        )
    for divider in (CARD_WIDTH / 3, CARD_WIDTH * 2 / 3):
        body.append(
            f'<line x1="{divider:g}" y1="66" x2="{divider:g}" y2="{height - 20}" '
            f'stroke="{COLORS["frame"]}" stroke-width="1"/>'
        )

    title = "CONTRIB.LOG // STREAK"
    aria = f"Contribution totals and streaks of {stats['login']}"
    return card(title, aria, height, body)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

TIMESTAMP_PATTERN = re.compile(r"last sync: [0-9:\- ]+ UTC")


def write_if_changed(svg: str, path: Path) -> bool:
    """Write the SVG only if it differs from the existing file beyond the
    footer timestamp. Keeps scheduled runs from producing no-op commits."""
    if path.exists():
        old = path.read_text(encoding="utf-8")
        if TIMESTAMP_PATTERN.sub("", old) == TIMESTAMP_PATTERN.sub("", svg):
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg, encoding="utf-8")
    return True


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("error: GITHUB_TOKEN environment variable is not set", file=sys.stderr)
        return 1
    username = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_USERNAME

    try:
        stats = collect_stats(token, username)
    except GitHubAPIError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    cards = {
        ASSETS_DIR / "github-stats.svg": render_stats_card(stats, now),
        ASSETS_DIR / "github-activity.svg": render_activity_card(stats),
        ASSETS_DIR / "github-streak.svg": render_streak_card(stats),
    }
    for path, svg in cards.items():
        if write_if_changed(svg, path):
            print(f"updated {path.name}")
        else:
            print(f"{path.name} unchanged, left untouched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
