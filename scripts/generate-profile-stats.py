#!/usr/bin/env python3
"""
Generate local SVG cards for a GitHub profile README.

What it does:
- Scans repositories under GITHUB_USERNAME only.
- Uses a personal token only when GH_STATS_TOKEN is supplied.
- Falls back to public profile repositories when no personal token is available.
- Counts either every commit in scanned profile repos or commits matching configured author identities.
- Aggregates language bytes across scanned repositories.
- Writes aggregate-only SVG files to ./profile.

It does not write repository names, private source code, or file contents into
the generated SVGs, and it avoids printing repository names into Actions logs.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter
from html import escape
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

API_BASE = "https://api.github.com"
USERNAME = os.environ.get("GITHUB_USERNAME", "13garth").strip()
PERSONAL_TOKEN = os.environ.get("GH_STATS_TOKEN") or ""
API_TOKEN = PERSONAL_TOKEN or os.environ.get("GITHUB_TOKEN") or ""
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "profile"))


def csv_values(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


EXCLUDE_REPOS = {repo.lower() for repo in csv_values("EXCLUDE_REPOS", "13garth/13garth,13garth/13garth.github.io")}
DEFAULT_HIDE_LANGUAGES = "HTML,CSS,SCSS,Hack,MDX,C,C++,Astro,Makefile,Shell,Dockerfile"
HIDE_LANGUAGES = set(csv_values("HIDE_LANGUAGES", DEFAULT_HIDE_LANGUAGES))
COMMIT_AUTHOR_LOGINS = {value.lower() for value in csv_values("COMMIT_AUTHOR_LOGINS", USERNAME)}
COMMIT_AUTHOR_NAMES = {value.lower() for value in csv_values("COMMIT_AUTHOR_NAMES", f"{USERNAME},Garth Baker")}
COMMIT_AUTHOR_EMAILS = {value.lower() for value in csv_values("COMMIT_AUTHOR_EMAILS")}
COUNT_ALL_COMMITS = os.environ.get("COUNT_ALL_COMMITS", "false").lower() in {"1", "true", "yes", "on"}
USE_GITHUB_AUTHOR_FILTER = os.environ.get("USE_GITHUB_AUTHOR_FILTER", "false").lower() in {"1", "true", "yes", "on"}

LANGUAGE_ALIASES: dict[str, str] = {}
for item in csv_values("LANGUAGE_ALIASES", "Blade:PHP,Twig:PHP"):
    if ":" in item:
        source, target = item.split(":", 1)
        if source.strip() and target.strip():
            LANGUAGE_ALIASES[source.strip()] = target.strip()

INCLUDE_FORKS = os.environ.get("INCLUDE_FORKS", "false").lower() in {"1", "true", "yes", "on"}
EXCLUDE_ARCHIVED = os.environ.get("EXCLUDE_ARCHIVED", "false").lower() in {"1", "true", "yes", "on"}
SCAN_ALL_BRANCHES = os.environ.get("SCAN_ALL_BRANCHES", "true").lower() in {"1", "true", "yes", "on"}

MAX_REPOS = int(os.environ.get("MAX_REPOS", "1000"))
MAX_BRANCHES_PER_REPO = int(os.environ.get("MAX_BRANCHES_PER_REPO", "25"))
MAX_COMMITS_PER_REPO = int(os.environ.get("MAX_COMMITS_PER_REPO", "2000"))
MAX_HISTORY_YEARS = int(os.environ.get("MAX_HISTORY_YEARS", "8"))
REQUEST_PAUSE_SECONDS = float(os.environ.get("REQUEST_PAUSE_SECONDS", "0.05"))

CARD_WIDTH = 760
TEXT_COLOR = "#f5f7fb"
MUTED_COLOR = "#9ca8ba"
PANEL_COLOR = "#111827"
BORDER_COLOR = "#263244"
BLUE = "#38bdf8"
GREEN = "#34d399"
RED = "#ff5a5f"
AMBER = "#f59e0b"
PURPLE = "#a78bfa"

# These only control display colors for visible languages; they do not add languages.
LANGUAGE_COLORS = {
    "PHP": "#4F5D95",
    "JavaScript": "#f1e05a",
    "TypeScript": "#3178c6",
    "Blade": "#f7523f",
    "HTML": "#e34c26",
    "CSS": "#663399",
    "Vue": "#41b883",
    "Dart": "#00B4AB",
    "Shell": "#89e051",
    "Python": "#3572A5",
    "Java": "#b07219",
    "C#": "#178600",
    "C++": "#f34b7d",
    "C": "#555555",
    "PowerShell": "#012456",
    "Dockerfile": "#384d54",
    "SCSS": "#c6538c",
    "Ruby": "#701516",
    "Go": "#00ADD8",
    "Kotlin": "#A97BFF",
    "Swift": "#F05138",
    "Twig": "#c1d026",
}


def api_json(url: str) -> tuple[Any, dict[str, str]]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "13garth-profile-stats-generator",
    }

    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"

    request = Request(url, headers=headers)

    try:
        with urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            response_headers = {key: value for key, value in response.headers.items()}
            return json.loads(body) if body else None, response_headers

    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")

        # 409 usually means an empty repository.
        # 404 can happen when a token cannot access a repo returned elsewhere.
        # Skip both safely without exposing repository names in logs.
        if error.code in {404, 409}:
            return None, {"Status": str(error.code), "Body": body}

        print(f"GitHub API error {error.code} while reading GitHub API.", file=sys.stderr)
        raise


def paged_get(path: str, params: dict[str, str], max_items: int | None = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    page = 1

    while True:
        query = dict(params)
        query["per_page"] = "100"
        query["page"] = str(page)

        url = f"{API_BASE}{path}?{urlencode(query)}"
        data, headers = api_json(url)

        if not isinstance(data, list):
            break

        results.extend(data)

        if max_items is not None and len(results) >= max_items:
            return results[:max_items]

        link_header = headers.get("Link", "")
        if 'rel="next"' not in link_header:
            break

        page += 1

    return results


def fetch_accessible_repositories() -> list[dict[str, Any]]:
    if PERSONAL_TOKEN:
        return paged_get(
            "/user/repos",
            {
                "visibility": "all",
                "affiliation": "owner",
                "sort": "updated",
                "direction": "desc",
            },
        )

    # Public fallback keeps local runs useful even when no PAT is available.
    return paged_get(
        f"/users/{quote(USERNAME)}/repos",
        {
            "type": "owner",
            "sort": "updated",
            "direction": "desc",
        },
    )


def fetch_repositories() -> list[dict[str, Any]]:
    repos = fetch_accessible_repositories()
    filtered: list[dict[str, Any]] = []
    seen: set[str] = set()

    for repo in repos:
        full_name = str(repo.get("full_name", "")).lower()

        if not full_name or full_name in seen:
            continue

        if full_name in EXCLUDE_REPOS:
            continue

        if repo.get("fork") and not INCLUDE_FORKS:
            continue

        if repo.get("archived") and EXCLUDE_ARCHIVED:
            continue

        filtered.append(repo)
        seen.add(full_name)

        if len(filtered) >= MAX_REPOS:
            break

    return filtered


def repo_path(full_name: str, suffix: str) -> str:
    owner, repo = full_name.split("/", 1)
    return f"/repos/{quote(owner)}/{quote(repo)}/{suffix}"


def fetch_branches(repo: dict[str, Any]) -> list[str]:
    default_branch = str(repo.get("default_branch") or "main")

    if not SCAN_ALL_BRANCHES:
        return [default_branch]

    full_name = str(repo["full_name"])
    branches = paged_get(repo_path(full_name, "branches"), {}, max_items=MAX_BRANCHES_PER_REPO)
    branch_names = [str(branch.get("name")) for branch in branches if branch.get("name")]

    return branch_names or [default_branch]


YEAR_RE = re.compile(r"^(\d{4})-")


def commit_year(commit: dict[str, Any]) -> str | None:
    commit_data = commit.get("commit", {})
    author_data = commit_data.get("author", {}) if isinstance(commit_data, dict) else {}
    committer_data = commit_data.get("committer", {}) if isinstance(commit_data, dict) else {}

    date_text = str(author_data.get("date") or committer_data.get("date") or "")
    match = YEAR_RE.match(date_text)

    return match.group(1) if match else None


def value_matches(value: Any, allowed: set[str]) -> bool:
    return bool(value) and str(value).strip().lower() in allowed


def commit_matches_author_identity(commit: dict[str, Any]) -> bool:
    api_author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
    api_committer = commit.get("committer") if isinstance(commit.get("committer"), dict) else {}
    commit_data = commit.get("commit", {}) if isinstance(commit.get("commit"), dict) else {}
    git_author = commit_data.get("author", {}) if isinstance(commit_data.get("author"), dict) else {}
    git_committer = commit_data.get("committer", {}) if isinstance(commit_data.get("committer"), dict) else {}

    return any(
        [
            value_matches(api_author.get("login"), COMMIT_AUTHOR_LOGINS),
            value_matches(api_committer.get("login"), COMMIT_AUTHOR_LOGINS),
            value_matches(git_author.get("name"), COMMIT_AUTHOR_NAMES),
            value_matches(git_committer.get("name"), COMMIT_AUTHOR_NAMES),
            value_matches(git_author.get("email"), COMMIT_AUTHOR_EMAILS),
            value_matches(git_committer.get("email"), COMMIT_AUTHOR_EMAILS),
        ]
    )


def fetch_authored_commit_summary(repo: dict[str, Any]) -> tuple[int, Counter[str]]:
    full_name = str(repo["full_name"])
    branches = fetch_branches(repo)
    seen_shas: set[str] = set()
    years: Counter[str] = Counter()

    for branch_name in branches:
        remaining = MAX_COMMITS_PER_REPO - len(seen_shas)
        if remaining <= 0:
            break

        query = {"sha": branch_name}

        if USE_GITHUB_AUTHOR_FILTER:
            query["author"] = USERNAME

        commits = paged_get(repo_path(full_name, "commits"), query, max_items=remaining)

        for commit in commits:
            sha = str(commit.get("sha") or "")
            if not sha or sha in seen_shas:
                continue

            if not COUNT_ALL_COMMITS and not USE_GITHUB_AUTHOR_FILTER and not commit_matches_author_identity(commit):
                continue

            seen_shas.add(sha)

            year = commit_year(commit)
            if year:
                years[year] += 1

    return len(seen_shas), years


def fetch_languages(repo: dict[str, Any]) -> Counter[str]:
    languages = Counter()
    languages_url = repo.get("languages_url")

    if not languages_url:
        return languages

    data, _headers = api_json(str(languages_url))

    if not isinstance(data, dict):
        return languages

    for language, byte_count in data.items():
        mapped_language = LANGUAGE_ALIASES.get(str(language), str(language))

        if mapped_language in HIDE_LANGUAGES:
            continue

        languages[mapped_language] += int(byte_count or 0)

    return languages


def format_number(value: int) -> str:
    return f"{value:,}"


def language_color(language: str) -> str:
    return LANGUAGE_COLORS.get(language, "#8b949e")


def svg_style() -> str:
    return f"""
  .bg {{ fill: #0b1020; }}
  .card {{ fill: #0f172a; stroke: {BORDER_COLOR}; stroke-width: 1; }}
  .panel {{ fill: {PANEL_COLOR}; stroke: #243047; stroke-width: 1; }}
  .title {{ fill: {TEXT_COLOR}; font: 800 24px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; }}
  .subtitle {{ fill: {MUTED_COLOR}; font: 500 13px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; }}
  .label {{ fill: #cbd5e1; font: 700 14px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; }}
  .small {{ fill: {MUTED_COLOR}; font: 500 12px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; }}
  .value {{ fill: {TEXT_COLOR}; font: 800 27px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; }}
  .axis {{ fill: #cbd5e1; font: 700 13px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; }}
"""


def card_defs() -> str:
    return """
<defs>
  <linearGradient id="accent" x1="0" x2="760" y1="0" y2="0" gradientUnits="userSpaceOnUse">
    <stop offset="0" stop-color="#ff5a5f" />
    <stop offset="0.34" stop-color="#f59e0b" />
    <stop offset="0.67" stop-color="#34d399" />
    <stop offset="1" stop-color="#38bdf8" />
  </linearGradient>
  <pattern id="grid" width="28" height="28" patternUnits="userSpaceOnUse">
    <path d="M 28 0 L 0 0 0 28" fill="none" stroke="#1e293b" stroke-width="0.7" opacity="0.55" />
  </pattern>
</defs>
"""


def card_shell(title: str, subtitle: str, height: int, body: str) -> str:
    return f'''<svg width="{CARD_WIDTH}" height="{height}" viewBox="0 0 {CARD_WIDTH} {height}" fill="none" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="title desc">
<title id="title">{escape(title)}</title>
<desc id="desc">{escape(subtitle)}</desc>
{card_defs()}
<style>{svg_style()}</style>
<rect class="bg" width="{CARD_WIDTH}" height="{height}" rx="16" />
<rect width="{CARD_WIDTH}" height="{height}" fill="url(#grid)" rx="16" />
<rect class="card" x="0.5" y="0.5" width="{CARD_WIDTH - 1}" height="{height - 1}" rx="16" />
<rect x="0" y="0" width="{CARD_WIDTH}" height="5" fill="url(#accent)" />
<text x="28" y="42" class="title">{escape(title)}</text>
<text x="28" y="64" class="subtitle">{escape(subtitle)}</text>
{body}
</svg>
'''


def write_hero_svg(output_file: Path) -> None:
    width = 760
    height = 280

    svg = f'''<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" fill="none" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="title desc">
<title id="title">Garth Baker - Senior Laravel Full-Stack Engineer</title>
<desc id="desc">Profile banner for Garth Baker, a senior Laravel full-stack engineer and senior WordPress developer based in the Netherlands.</desc>
<defs>
  <linearGradient id="heroAccent" x1="0" x2="{width}" y1="0" y2="{height}" gradientUnits="userSpaceOnUse">
    <stop offset="0" stop-color="#ff5a5f" />
    <stop offset="0.32" stop-color="#f59e0b" />
    <stop offset="0.66" stop-color="#34d399" />
    <stop offset="1" stop-color="#38bdf8" />
  </linearGradient>
  <pattern id="heroGrid" width="34" height="34" patternUnits="userSpaceOnUse">
    <path d="M 34 0 L 0 0 0 34" fill="none" stroke="#263244" stroke-width="0.8" opacity="0.55" />
  </pattern>
</defs>
<style>
  .bg {{ fill: #08111f; }}
  .grid {{ fill: url(#heroGrid); }}
  .title {{ fill: #f8fafc; font: 900 46px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; letter-spacing: 0; }}
  .role {{ fill: #dbeafe; font: 800 20px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; }}
  .copy {{ fill: #aebbd0; font: 600 13px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; }}
  .pillText {{ fill: #f8fafc; font: 800 12px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; }}
  .pill {{ fill: #111827; stroke: #2b3952; stroke-width: 1; }}
  .metric {{ fill: #f8fafc; font: 900 25px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; }}
  .metricLabel {{ fill: #aebbd0; font: 700 12px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; }}
</style>
<rect class="bg" width="{width}" height="{height}" rx="24" />
<rect class="grid" width="{width}" height="{height}" rx="24" />
<rect x="0" y="0" width="{width}" height="8" fill="url(#heroAccent)" />
<rect x="28" y="34" width="70" height="70" rx="20" fill="#111827" stroke="#2b3952" />
<text x="63" y="70" fill="#ff5a5f" font-family="-apple-system, BlinkMacSystemFont, Segoe UI, sans-serif" font-size="34" font-weight="900" text-anchor="middle" dominant-baseline="middle">GB</text>
<text x="116" y="60" class="title">Garth Baker</text>
<text x="118" y="92" class="role">Senior Laravel Full-Stack Engineer</text>
<text x="118" y="121" class="copy">Senior WordPress developer, product builder, and practical business software engineer.</text>
<text x="118" y="142" class="copy">Based in the Netherlands. Strongest lane: Laravel, PHP, MySQL, JavaScript.</text>
<rect x="28" y="166" width="157" height="35" rx="17.5" class="pill" />
<text x="106.5" y="188" class="pillText" text-anchor="middle">Laravel full stack</text>
<rect x="197" y="166" width="165" height="35" rx="17.5" class="pill" />
<text x="279.5" y="188" class="pillText" text-anchor="middle">Senior WordPress</text>
<rect x="374" y="166" width="157" height="35" rx="17.5" class="pill" />
<text x="452.5" y="188" class="pillText" text-anchor="middle">PHP / MySQL / JS</text>
<rect x="543" y="166" width="189" height="35" rx="17.5" class="pill" />
<text x="637.5" y="188" class="pillText" text-anchor="middle">Product &amp; business tools</text>
<rect x="28" y="226" width="194" height="1" fill="#334155" />
<text x="28" y="258" class="metric">SaaS</text>
<text x="94" y="258" class="metricLabel">billing, dashboards, APIs</text>
<rect x="283" y="226" width="194" height="1" fill="#334155" />
<text x="283" y="258" class="metric">CMS</text>
<text x="350" y="258" class="metricLabel">WordPress builds and recovery</text>
<rect x="538" y="226" width="194" height="1" fill="#334155" />
<text x="538" y="258" class="metric">Ops</text>
<text x="594" y="258" class="metricLabel">deployments and CI</text>
</svg>
'''

    output_file.write_text(svg, encoding="utf-8")


def write_stats_svg(stats: dict[str, int | str], output_file: Path) -> None:
    height = 232
    panels = [
        (28, 92, 166, 78, "Commits", format_number(int(stats["total_commits"])), RED),
        (210, 92, 166, 78, "Profile repos", format_number(int(stats["repo_count"])), BLUE),
        (392, 92, 166, 78, "Private repos", format_number(int(stats["private_repo_count"])), AMBER),
        (574, 92, 158, 78, "Active years", format_number(int(stats["active_year_count"])), GREEN),
    ]

    panel_svg = []
    for x, y, width, panel_height, label, value, color in panels:
        panel_svg.append(
            f'<rect class="panel" x="{x}" y="{y}" width="{width}" height="{panel_height}" rx="12" />'
            f'<circle cx="{x + 20}" cy="{y + 23}" r="5" fill="{color}" />'
            f'<text x="{x + 34}" y="{y + 28}" class="label">{escape(label)}</text>'
            f'<text x="{x + 20}" y="{y + 61}" class="value">{escape(value)}</text>'
        )

    footer = (
        f'13garth profile repositories only | '
        f'forks {"included" if INCLUDE_FORKS else "hidden"} | '
        f'branch scan {"on" if SCAN_ALL_BRANCHES else "off"} | '
        f'{escape(str(stats["scan_mode"]))}'
    )

    body = "".join(panel_svg)
    body += f'<text x="28" y="204" class="small">{footer}</text>'

    subtitle = "Aggregate activity across profile repositories only. Organizations are not scanned."
    output_file.write_text(card_shell("GitHub Activity", subtitle, height, body), encoding="utf-8")


def write_languages_svg(languages: Counter[str], output_file: Path) -> None:
    width = CARD_WIDTH
    top_languages = languages.most_common(8)
    total_bytes = sum(languages.values())

    if total_bytes <= 0 or not top_languages:
        body = '<text x="28" y="112" class="label">No language data found.</text>'
        output_file.write_text(
            card_shell("Language Mix", "Aggregate language usage across scanned repositories.", 150, body),
            encoding="utf-8",
        )
        return

    height = 150 + len(top_languages) * 28
    bar_x = 28
    bar_y = 92
    bar_width = width - 56
    bar_height = 16
    current_x = bar_x
    bar_segments = []
    legend_rows = []

    for index, (language, byte_count) in enumerate(top_languages):
        percentage = byte_count / total_bytes
        remaining_width = bar_x + bar_width - current_x
        segment_width = round(bar_width * percentage)

        if index == len(top_languages) - 1:
            segment_width = remaining_width
        else:
            segment_width = max(1, min(segment_width, remaining_width))

        color = language_color(language)
        if segment_width > 0:
            bar_segments.append(
                f'<rect x="{current_x}" y="{bar_y}" width="{segment_width}" height="{bar_height}" fill="{color}" />'
            )
            current_x += segment_width

        legend_y = 144 + index * 28
        percent_text = f"{percentage * 100:.1f}%"
        legend_rows.append(
            f'<circle cx="38" cy="{legend_y - 5}" r="5" fill="{color}" />'
            f'<text x="54" y="{legend_y}" class="label">{escape(language)}</text>'
            f'<text x="690" y="{legend_y}" class="small" text-anchor="end">{escape(percent_text)}</text>'
        )

    alias_note = ", ".join(f"{source}->{target}" for source, target in LANGUAGE_ALIASES.items()) or "none"
    subtitle = f"Aggregate repository language usage. Aliases: {alias_note}. Incidental languages hidden."

    body = f'''
<clipPath id="barClip"><rect x="{bar_x}" y="{bar_y}" width="{bar_width}" height="{bar_height}" rx="8" /></clipPath>
<rect x="{bar_x}" y="{bar_y}" width="{bar_width}" height="{bar_height}" fill="#1f2937" rx="8" />
<g clip-path="url(#barClip)">{"".join(bar_segments)}</g>
{"".join(legend_rows)}
'''

    output_file.write_text(card_shell("Language Mix", subtitle, height, body), encoding="utf-8")


def write_commit_history_svg(years: Counter[str], output_file: Path) -> None:
    top_years = sorted(years.items(), key=lambda item: item[0], reverse=True)[:MAX_HISTORY_YEARS]

    if not top_years:
        body = '<text x="28" y="112" class="label">No authored commits found in the scanned repositories.</text>'
        output_file.write_text(
            card_shell("Commit History", "Authored commits grouped by year across scanned repositories.", 150, body),
            encoding="utf-8",
        )
        return

    height = 112 + len(top_years) * 31 + 24
    max_count = max(count for _year, count in top_years)
    bar_x = 126
    bar_width = 500
    colors = [RED, AMBER, GREEN, BLUE, PURPLE]
    rows = []

    for index, (year, count) in enumerate(top_years):
        y = 104 + index * 31
        fill_width = max(6, round(bar_width * (count / max_count)))
        color = colors[index % len(colors)]

        rows.append(
            f'<text x="34" y="{y}" class="axis">{escape(year)}</text>'
            f'<rect x="{bar_x}" y="{y - 10}" width="{bar_width}" height="12" fill="#1f2937" rx="6" />'
            f'<rect x="{bar_x}" y="{y - 10}" width="{fill_width}" height="12" fill="{color}" rx="6" />'
            f'<text x="710" y="{y}" class="axis" text-anchor="end">{format_number(count)}</text>'
        )

    history_scope = "profile repository commits" if COUNT_ALL_COMMITS else "authored commits"
    subtitle = f"Unique {history_scope} grouped by year; branch-aware when SCAN_ALL_BRANCHES is enabled."
    output_file.write_text(card_shell("Commit History", subtitle, height, "".join(rows)), encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    scan_mode = "personal token scan" if PERSONAL_TOKEN else "public profile scan"
    print(f"Fetching repositories for {scan_mode}...")
    repos = fetch_repositories()
    print(f"Repositories scanned: {len(repos)}")

    total_commits = 0
    yearly_commits: Counter[str] = Counter()
    language_totals: Counter[str] = Counter()

    for index, repo in enumerate(repos, start=1):
        # Do not print repo names here. Public Actions logs can be visible, and
        # private repo names may be sensitive.
        print(f"Scanning repository {index}/{len(repos)}", flush=True)

        commit_count, repo_years = fetch_authored_commit_summary(repo)
        total_commits += commit_count
        yearly_commits.update(repo_years)
        language_totals.update(fetch_languages(repo))

        time.sleep(REQUEST_PAUSE_SECONDS)

    private_repo_count = sum(1 for repo in repos if repo.get("private"))

    stats: dict[str, int | str] = {
        "total_commits": total_commits,
        "repo_count": len(repos),
        "private_repo_count": private_repo_count,
        "language_count": len(language_totals),
        "active_year_count": len(yearly_commits),
        "scan_mode": scan_mode,
    }

    write_hero_svg(OUTPUT_DIR / "hero.svg")
    write_stats_svg(stats, OUTPUT_DIR / "stats.svg")
    write_commit_history_svg(yearly_commits, OUTPUT_DIR / "commit-history.svg")
    write_languages_svg(language_totals, OUTPUT_DIR / "top-langs.svg")

    print("Generated:")
    print(f"- {OUTPUT_DIR / 'hero.svg'}")
    print(f"- {OUTPUT_DIR / 'stats.svg'}")
    print(f"- {OUTPUT_DIR / 'commit-history.svg'}")
    print(f"- {OUTPUT_DIR / 'top-langs.svg'}")


if __name__ == "__main__":
    main()
