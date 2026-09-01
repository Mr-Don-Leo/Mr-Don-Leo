#!/usr/bin/env python3
"""Generate a terminal-style GitHub stats SVG card.

Fetches profile data via the GitHub GraphQL API (stars, followers,
languages, PRs, issues, commit contributions) and renders a deterministic,
dependency-free SVG into assets/github-stats.svg.

Usage:
    GITHUB_TOKEN=<token> python3 scripts/generate_stats.py [username]

Only the Python standard library is used. See README.md for details on
which statistics are exact and which are approximations.
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
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "assets" / "github-stats.svg"

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

COMMITS_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
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


def fetch_total_commits(token: str, login: str, created_at: str) -> int:
    """Sum commit contributions year by year since account creation.

    contributionsCollection only accepts a range of at most one year, so the
    account lifetime is split into calendar-year windows. Counts follow
    GitHub's contribution rules (default-branch commits in repos the token
    can see), so this matches the profile contribution graph rather than a
    raw git count.
    """
    created_year = int(created_at[:4])
    now = datetime.now(timezone.utc)
    total = 0
    for year in range(created_year, now.year + 1):
        start = f"{year}-01-01T00:00:00Z"
        end = f"{year}-12-31T23:59:59Z"
        data = graphql(token, COMMITS_QUERY, {"login": login, "from": start, "to": end})
        total += data["user"]["contributionsCollection"]["totalCommitContributions"]
    return total


def collect_stats(token: str, username: str) -> dict:
    """Gather every statistic shown on the card into a plain dict."""
    profile = graphql(token, PROFILE_QUERY, {"login": username})["user"]
    repos = fetch_repositories(token, username)
    commits = fetch_total_commits(token, username, profile["createdAt"])

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
    )[:3]

    return {
        "login": profile["login"],
        "repositories": profile["publicRepos"]["totalCount"],
        "followers": profile["followers"]["totalCount"],
        "stars": sum(r["stargazerCount"] for r in repos),
        "commits": commits,
        "pull_requests": profile["pullRequests"]["totalCount"],
        "issues": profile["issues"]["totalCount"],
        # Sorted by bytes desc, then name, for deterministic output.
        "languages": sorted(languages.items(), key=lambda kv: (-kv[1], kv[0]))[:5],
        "recent": [(r["name"], r["pushedAt"][:10]) for r in recent],
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
WIDTH = 560

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

BAR_CHARS = 18


def text(x: float, y: int, content: str, fill: str, bold: bool = False) -> str:
    weight = ' font-weight="bold"' if bold else ""
    return (
        f'<text x="{x:g}" y="{y}" fill="{fill}"{weight} xml:space="preserve">'
        f"{escape(content)}</text>"
    )


def col(chars: float) -> float:
    """Convert a character column to an x pixel offset inside the padding."""
    return PAD_X + chars * CHAR_WIDTH


def render_svg(stats: dict, generated_at: datetime) -> str:
    lines: list[str] = []
    y = 76  # first baseline below the header bar

    def emit(*fragments: str) -> None:
        lines.extend(fragments)

    def newline(count: int = 1) -> None:
        nonlocal y
        y += LINE_HEIGHT * count

    # -- activity tree ------------------------------------------------------
    emit(text(col(0), y, "github.activity", COLORS["section"], bold=True))
    newline()
    rows = [
        ("repositories", stats["repositories"]),
        ("stars", stats["stars"]),
        ("commits", stats["commits"]),
        ("pull_requests", stats["pull_requests"]),
        ("issues", stats["issues"]),
        ("followers", stats["followers"]),
    ]
    for index, (label, value) in enumerate(rows):
        branch = "└─" if index == len(rows) - 1 else "├─"
        emit(
            text(col(0), y, branch, COLORS["tree"]),
            text(col(3), y, label, COLORS["label"]),
            text(col(17), y, f"{value:>7,}", COLORS["value"], bold=True),
        )
        newline()
    newline()

    # -- languages ----------------------------------------------------------
    emit(text(col(0), y, "languages", COLORS["section"], bold=True))
    newline()
    total_bytes = sum(size for _, size in stats["languages"]) or 1
    for name, size in stats["languages"]:
        share = size / total_bytes
        filled = max(1, round(share * BAR_CHARS))
        bar = "█" * filled + "░" * (BAR_CHARS - filled)
        emit(
            text(col(0), y, f"{name[:12]:<13}", COLORS["label"]),
            text(col(13), y, bar[:filled], COLORS["bar_fill"]),
            text(col(13 + filled), y, bar[filled:], COLORS["bar_empty"]),
            text(col(13 + BAR_CHARS + 2), y, f"{share * 100:4.1f}%", COLORS["value"]),
        )
        newline()
    if not stats["languages"]:
        emit(text(col(0), y, "no language data", COLORS["muted"]))
        newline()
    newline()

    # -- recent activity ----------------------------------------------------
    emit(text(col(0), y, "recent.pushes", COLORS["section"], bold=True))
    newline()
    for name, pushed in stats["recent"]:
        emit(
            text(col(0), y, "→", COLORS["tree"]),
            text(col(2), y, name[:26], COLORS["value"]),
            text(col(30), y, pushed, COLORS["muted"]),
        )
        newline()
    newline()

    # -- prompt with blinking cursor ---------------------------------------
    prompt = f"{stats['login'].lower()}@github:~$"
    emit(text(col(0), y, prompt, COLORS["prompt"], bold=True))
    cursor_x = col(len(prompt) + 1)
    emit(
        f'<rect class="cursor" x="{cursor_x:g}" y="{y - 12}" '
        f'width="8" height="15" fill="{COLORS["prompt"]}"/>'
    )
    newline()

    # -- footer -------------------------------------------------------------
    stamp = generated_at.strftime("%Y-%m-%d %H:%M UTC")
    emit(
        f'<text x="{WIDTH - PAD_X}" y="{y}" fill="{COLORS["muted"]}" '
        f'text-anchor="end" font-size="11">last sync: {escape(stamp)}</text>'
    )
    height = y + 26

    title = f"{stats['login'].upper()} // SYSTEM MONITOR"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" viewBox="0 0 {WIDTH} {height}" role="img" aria-label="GitHub statistics for {escape(stats['login'])}">
  <style>
    text {{
      font-family: 'JetBrains Mono', 'Fira Code', 'SF Mono', Menlo, Consolas, 'DejaVu Sans Mono', monospace;
      font-size: {FONT_SIZE}px;
    }}
    .cursor {{ animation: blink 1.2s step-end infinite; }}
    @keyframes blink {{ 50% {{ opacity: 0; }} }}
  </style>
  <rect x="1.5" y="1.5" width="{WIDTH - 3}" height="{height - 3}" rx="12" fill="{COLORS['bg']}" stroke="{COLORS['border']}" stroke-opacity="0.55" stroke-width="1.5"/>
  <circle cx="{PAD_X}" cy="30" r="5" fill="#ff5f56"/>
  <circle cx="{PAD_X + 18}" cy="30" r="5" fill="#ffbd2e"/>
  <circle cx="{PAD_X + 36}" cy="30" r="5" fill="#27c93f"/>
  <text x="{PAD_X + 54}" y="35" fill="{COLORS['title']}" font-weight="bold">{escape(title)}</text>
  <line x1="{PAD_X - 12}" y1="48" x2="{WIDTH - PAD_X + 12}" y2="48" stroke="{COLORS['frame']}" stroke-width="1"/>
  {chr(10).join('  ' + line for line in lines)}
</svg>
"""


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

    svg = render_svg(stats, datetime.now(timezone.utc))
    if write_if_changed(svg, OUTPUT_PATH):
        print(f"updated {OUTPUT_PATH}")
    else:
        print("stats unchanged, file left untouched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
