#!/usr/bin/env python3
"""Generate a vintage-newspaper "on this day" page for GitHub activity.

Two modes. Repository mode collects everything that happened on one
month-day across every year of one or more repositories' history:
commits, issues opened and closed, pull requests merged, releases
published, tags cut, and notable comments. User mode (--author)
collects one user's whole footprint on the month-day instead: their
commits, issues and PRs opened, PRs merged, issues closed, and their
own comments, across every repository they touched.

Renders the result as a self-contained HTML page styled like a vintage
newspaper, with a gossip column up front: reverts (reverts of reverts
get flagged) and the day's most-commented threads.

Usage:
    python3 generate.py owner/repo [owner/repo ...] [--date MM-DD]
        [--out FILE] [--no-comments] [--share]
    python3 generate.py --author USER [--date MM-DD] [--out FILE] [--share]

With --share, the finished page is published to here.now as an anonymous
site and the shareable link (24-hour expiry) is printed. Anonymous
publishing needs no account and no login. The link is unlisted but anyone
with the URL can open it, so only share editions you are comfortable
making visible.

The month-day defaults to today (local time). The script authenticates with
THE_DAILY_COMMIT_TOKEN or GITHUB_TOKEN when set, and otherwise reuses the
GitHub CLI credential, so anyone logged in with `gh auth login` needs no
extra setup. Without any credential, the script uses the lower
unauthenticated rate limit and can only read public repositories. All API
calls are read-only GET requests, and nothing leaves the machine the
script runs on.
"""

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

API = "https://api.github.com"
__version__ = "0.1.0"
MAX_ISSUE_PAGES = 10
MAX_PULL_PAGES = 5
MAX_RELEASE_PAGES = 3
MAX_COMMENT_PAGES = 2
MAX_YEARS = 25
MAX_TAG_DATE_LOOKUPS = 25


_cached_token = None
_token_resolved = False


def resolve_token():
    """Return a GitHub token, or "" when none is available.

    Uses THE_DAILY_COMMIT_TOKEN or GITHUB_TOKEN when set, otherwise falls
    back to the GitHub CLI's stored credential, so anyone already logged
    in with `gh auth login` needs no extra setup. The result is cached for
    the run.
    """
    global _cached_token, _token_resolved
    if _token_resolved:
        return _cached_token
    token = ""
    for var in ("THE_DAILY_COMMIT_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(var, "").strip()
        if token:
            break
    else:
        gh = shutil.which("gh")
        if gh is not None:
            try:
                proc = subprocess.run(
                    [gh, "auth", "token"],
                    capture_output=True, text=True, timeout=15)
                candidate = proc.stdout.strip()
                if proc.returncode == 0 and candidate:
                    token = candidate
            except (OSError, subprocess.TimeoutExpired):
                token = ""
    _cached_token = token
    _token_resolved = True
    return _cached_token


def api_get(path, params=None):
    """Perform one authenticated-or-anonymous GET against the GitHub API."""
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "the-daily-commit-generator",
        },
    )
    token = resolve_token()
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def search_get(path, params):
    """GET against the search API, paced for its stricter rate limits."""
    time.sleep(2 if resolve_token() else 7)
    try:
        return api_get(path, params)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            print("error: GitHub search rate limit hit. Wait a minute, or "
                  "set THE_DAILY_COMMIT_TOKEN (or log in with gh) for "
                  "higher limits.", file=sys.stderr)
        raise


def search_paged(path, q):
    """First page of a search (up to 100 hits): one author's day fits."""
    data = search_get(path, {"q": q, "per_page": 100})
    return data.get("items", [])


def api_paged(path, params, max_pages, ignore_404=False):
    """Collect up to max_pages of a paginated endpoint.

    With ignore_404, a 404 becomes an empty collection. Collection
    endpoints 404 when the feature is disabled on the repository (for
    example, pull requests on a mailing-list workflow), which means
    there is simply nothing to report.
    """
    items = []
    params = dict(params or {})
    for page in range(1, max_pages + 1):
        params["page"] = page
        try:
            batch = api_get(path, params)
        except urllib.error.HTTPError as exc:
            if ignore_404 and exc.code == 404:
                return items
            raise
        if not batch:
            break
        items.extend(batch)
        if len(batch) < params.get("per_page", 30):
            break
    return items


def month_day(iso_stamp):
    """Extract MM-DD from an ISO 8601 timestamp. Returns None when absent."""
    if not iso_stamp or len(iso_stamp) < 10:
        return None
    return iso_stamp[5:10]


def year_of(iso_stamp):
    return int(iso_stamp[0:4])


def year_ranges(year, mds):
    """(since, until) ISO ranges covering the month-days within one year.

    A week can straddle New Year; then it becomes two ranges.
    """
    lo, hi = min(mds), max(mds)
    if lo <= hi:
        return [("%d-%sT00:00:00Z" % (year, lo),
                 "%d-%sT23:59:59Z" % (year, hi))]
    return [("%d-01-01T00:00:00Z" % year,
             "%d-%sT23:59:59Z" % (year, hi)),
            ("%d-%sT00:00:00Z" % (year, lo),
             "%d-12-31T23:59:59Z" % year)]


def search_ranges(year, mds):
    """GitHub search date ranges covering the month-days within one year."""
    lo, hi = min(mds), max(mds)
    if lo <= hi:
        return ["%d-%s..%d-%s" % (year, lo, year, hi)]
    return ["%d-01-01..%d-%s" % (year, hi),
            "%d-%s..%d-12-31" % (year, lo)]


def esc(text):
    return html.escape(text or "", quote=True)


def first_line(text):
    line = (text or "").strip().split("\n")[0].strip()
    return line if len(line) <= 140 else line[:137] + "..."


def fetch_commits(repo, mds, years):
    """Commits whose committer date falls on one of the month-days."""
    mds = set(mds)
    found = []
    for year in years:
        for since, until in year_ranges(year, mds):
            params = {"since": since, "until": until, "per_page": 100}
            for commit in api_paged("/repos/%s/commits" % repo, params, 1,
                                    ignore_404=True):
                info = commit.get("commit", {})
                author = (info.get("author") or {}).get("name") or "unknown"
                day = month_day((info.get("committer") or {}).get("date"))
                if day not in mds:
                    continue
                found.append({
                    "year": year,
                    "day": day,
                    "headline": first_line(info.get("message")),
                    "byline": author,
                    "body": commit.get("sha", "")[:7],
                    "url": commit.get("html_url", ""),
                })
    return found


def fetch_issues(repo, mds):
    """Non-PR issues opened or closed on the month-days, client-filtered."""
    mds = set(mds)
    params = {"state": "all", "sort": "created", "direction": "asc",
              "per_page": 100}
    opened, closed = [], []
    for issue in api_paged("/repos/%s/issues" % repo, params,
                             MAX_ISSUE_PAGES, ignore_404=True):
        if "pull_request" in issue:
            continue
        number = issue.get("number")
        url = issue.get("html_url", "")
        byline = (issue.get("user") or {}).get("login") or "unknown"
        created_day = month_day(issue.get("created_at"))
        if created_day in mds:
            opened.append({
                "year": year_of(issue["created_at"]),
                "day": created_day,
                "headline": "#%s %s" % (number, first_line(issue.get("title"))),
                "byline": byline,
                "body": first_line(issue.get("body")),
                "url": url,
                "comments": issue.get("comments", 0),
            })
        closed_day = month_day(issue.get("closed_at"))
        if closed_day in mds:
            closed.append({
                "year": year_of(issue["closed_at"]),
                "day": closed_day,
                "headline": "#%s %s" % (number, first_line(issue.get("title"))),
                "byline": byline,
                "body": first_line(issue.get("body")),
                "url": url,
                "comments": issue.get("comments", 0),
            })
    return opened, closed


def fetch_merged_prs(repo, mds):
    """Pull requests merged on the month-days."""
    mds = set(mds)
    params = {"state": "closed", "sort": "updated", "direction": "desc",
              "per_page": 100}
    merged = []
    for pr in api_paged("/repos/%s/pulls" % repo, params, MAX_PULL_PAGES,
                          ignore_404=True):
        day = month_day(pr.get("merged_at"))
        if day not in mds:
            continue
        merged.append({
            "year": year_of(pr["merged_at"]),
            "day": day,
            "headline": "#%s %s (%s \u2192 %s)" % (
                pr.get("number"), first_line(pr.get("title")),
                ((pr.get("head") or {}).get("ref")) or "?",
                ((pr.get("base") or {}).get("ref")) or "?"),
            "byline": (pr.get("user") or {}).get("login") or "unknown",
            "body": first_line(pr.get("body")),
            "url": pr.get("html_url", ""),
        })
    return merged


def fetch_releases(repo, mds):
    """Releases published on the month-days."""
    mds = set(mds)
    released = []
    for rel in api_paged("/repos/%s/releases" % repo, {"per_page": 100},
                         MAX_RELEASE_PAGES, ignore_404=True):
        stamp = rel.get("published_at") or rel.get("created_at")
        day = month_day(stamp)
        if day not in mds:
            continue
        released.append({
            "year": year_of(stamp),
            "day": day,
            "headline": first_line(rel.get("name") or rel.get("tag_name")),
            "byline": (rel.get("author") or {}).get("login") or "unknown",
            "body": first_line(rel.get("body")),
            "url": rel.get("html_url", ""),
            "tag": rel.get("tag_name") or "",
        })
    return released


def fetch_tags(repo, mds, skip_names):
    """Tags cut on the month-days, best-effort for the most recent tags.

    The tags API carries no dates, so each tag's commit is resolved with
    one extra call, bounded by MAX_TAG_DATE_LOOKUPS. Tags that already
    have a GitHub release are skipped: the release announcement covers
    them.
    """
    mds = set(mds)
    found = []
    tags = api_paged("/repos/%s/tags" % repo, {"per_page": 100}, 1,
                     ignore_404=True)
    for tag in tags[:MAX_TAG_DATE_LOOKUPS]:
        name = tag.get("name") or ""
        sha = ((tag.get("commit") or {}).get("sha")) or ""
        if not name or not sha or name in skip_names:
            continue
        try:
            commit = api_get("/repos/%s/commits/%s" % (repo, sha))
        except urllib.error.HTTPError:
            continue
        info = commit.get("commit", {})
        stamp = ((info.get("committer") or {}).get("date")
                 or (info.get("author") or {}).get("date"))
        day = month_day(stamp)
        if day not in mds:
            continue
        author = ((info.get("author") or {}).get("name")
                  or (info.get("committer") or {}).get("name")
                  or "unknown")
        found.append({
            "year": year_of(stamp),
            "day": day,
            "headline": name,
            "byline": author,
            "body": first_line(info.get("message")),
            "url": "https://github.com/%s/tree/%s" % (repo, name),
        })
    return found


def fetch_comments(repo, mds):
    """Up to five issue comments written on the month-days."""
    mds = set(mds)
    params = {"sort": "created", "direction": "desc", "per_page": 100}
    picked = []
    for comment in api_paged("/repos/%s/issues/comments" % repo, params,
                             MAX_COMMENT_PAGES, ignore_404=True):
        day = month_day(comment.get("created_at"))
        if day not in mds:
            continue
        picked.append({
            "year": year_of(comment["created_at"]),
            "day": day,
            "headline": "A voice from the threads",
            "byline": (comment.get("user") or {}).get("login") or "unknown",
            "body": first_line(comment.get("body")),
            "url": comment.get("html_url", ""),
        })
        if len(picked) >= 5:
            break
    return picked


def author_commits(user, mds, years):
    """Commits authored by user on the month-days, via commit search."""
    mds = set(mds)
    found = []
    for year in years:
        for span in search_ranges(year, mds):
            query = "author:%s committer-date:%s" % (user, span)
            for hit in search_paged("/search/commits", query):
                info = hit.get("commit", {})
                stamp = ((info.get("committer") or {}).get("date")
                         or (info.get("author") or {}).get("date"))
                day = month_day(stamp)
                if day not in mds:
                    continue
                repo = ((hit.get("repository") or {}).get("full_name")) or "?"
                found.append({
                    "year": year_of(stamp),
                    "day": day,
                    "headline": first_line(info.get("message")),
                    "byline": "%s · %s" % (user, repo),
                    "body": hit.get("sha", "")[:7],
                    "url": hit.get("html_url", ""),
                    "login": user,
                })
    return found


def author_issue_item(hit, stamp, user, kind, head="?", base="?"):
    """One issue/PR search hit as a newspaper item."""
    repo_url = hit.get("repository_url", "").rstrip("/")
    repo = "/".join(repo_url.split("/")[-2:]) or "?"
    if kind == "PR":
        headline = "PR #%s %s (%s \u2192 %s)" % (
            hit.get("number"), first_line(hit.get("title")), head, base)
    else:
        headline = "%s #%s %s" % (kind, hit.get("number"),
                                  first_line(hit.get("title")))
    return {
        "year": year_of(stamp),
        "headline": headline,
        "byline": "%s · %s" % (user, repo),
        "body": first_line(hit.get("body")),
        "url": hit.get("html_url", ""),
        "comments": hit.get("comments", 0),
    }


def pr_branches(owner, repo, number):
    """(head, base) branch names for a PR, ("?", "?") on failure."""
    try:
        pr = api_get("/repos/%s/%s/pulls/%s" % (owner, repo, number))
    except urllib.error.HTTPError:
        return "?", "?"
    head = ((pr.get("head") or {}).get("ref")) or "?"
    base = ((pr.get("base") or {}).get("ref")) or "?"
    return head, base


def repo_parts(hit):
    """(owner, repo) from an issue search hit's repository_url."""
    parts = hit.get("repository_url", "").rstrip("/").split("/")[-2:]
    return parts if len(parts) == 2 else (None, None)


def author_issues(user, mds, years):
    """Issues/PRs opened, merged, and issues closed by user on the days.

    Merged pull requests need one pulls-API read each: issue search
    results carry no merge date. Releases and tags have no global
    search, so they stay repository-mode only.
    """
    mds = set(mds)
    opened, prs_opened, merged, closed = [], [], [], []
    for year in years:
        for span in search_ranges(year, mds):
            for hit in search_paged(
                    "/search/issues", "author:%s created:%s" % (user, span)):
                created_day = month_day(hit.get("created_at"))
                if created_day not in mds:
                    continue
                if "pull_request" in hit:
                    owner, repo = repo_parts(hit)
                    if owner:
                        head, base = pr_branches(owner, repo,
                                                hit.get("number"))
                    else:
                        head, base = "?", "?"
                    item = author_issue_item(hit, hit.get("created_at"),
                                             user, "PR", head, base)
                else:
                    item = author_issue_item(hit, hit.get("created_at"),
                                             user, "Issue")
                item["day"] = created_day
                (prs_opened if "pull_request" in hit
                 else opened).append(item)
            for hit in search_paged(
                    "/search/issues",
                    "author:%s updated:%s type:pr" % (user, span)):
                if month_day(hit.get("closed_at")) not in mds:
                    continue
                owner, repo = repo_parts(hit)
                if not owner:
                    continue
                try:
                    pr = api_get("/repos/%s/%s/pulls/%s"
                                 % (owner, repo, hit.get("number")))
                except urllib.error.HTTPError:
                    continue
                merged_day = month_day(pr.get("merged_at"))
                if merged_day in mds:
                    head = ((pr.get("head") or {}).get("ref")) or "?"
                    base = ((pr.get("base") or {}).get("ref")) or "?"
                    item = author_issue_item(hit, pr.get("merged_at"),
                                             user, "PR", head, base)
                    item["day"] = merged_day
                    merged.append(item)
            for hit in search_paged(
                    "/search/issues",
                    "author:%s updated:%s type:issue" % (user, span)):
                closed_day = month_day(hit.get("closed_at"))
                if closed_day in mds:
                    item = author_issue_item(hit, hit.get("closed_at"),
                                             user, "Issue")
                    item["day"] = closed_day
                    closed.append(item)
    return opened, prs_opened, merged, closed


def author_comments(user, mds, years):
    """Comments the user posted on the month-days, across all repos."""
    mds = set(mds)
    found = []
    seen = set()
    for year in years:
        for span in search_ranges(year, mds):
            for hit in search_paged(
                    "/search/issues", "commenter:%s updated:%s" % (user, span)):
                owner, repo = repo_parts(hit)
                if not owner:
                    continue
                key = (owner, repo, hit.get("number"))
                if key in seen:
                    continue
                seen.add(key)
                try:
                    comments = api_paged(
                        "/repos/%s/%s/issues/%s/comments"
                        % (owner, repo, hit.get("number")),
                        {"per_page": 100}, MAX_COMMENT_PAGES)
                except urllib.error.HTTPError:
                    continue
                kind = "PR" if "pull_request" in hit else "Issue"
                for comment in comments:
                    day = month_day(comment.get("created_at"))
                    if ((comment.get("user") or {}).get("login") != user
                            or day not in mds):
                        continue
                    found.append({
                        "year": year,
                        "day": day,
                        "headline": "On %s #%s %s" % (
                            kind, hit.get("number"),
                            first_line(hit.get("title"))),
                        "byline": "%s · %s" % (user, "%s/%s" % (owner, repo)),
                        "body": first_line(comment.get("body")),
                        "url": comment.get("html_url", ""),
                    })
    return found


def article(item):
    """Render one event as a newspaper article block."""
    body = "<p>%s</p>" % esc(item["body"]) if item["body"] else ""
    if item["url"]:
        headline = '<h3><a href="%s">%s</a></h3>' % (
            esc(item["url"]), esc(item["headline"]) or "(untitled)")
    else:
        headline = "<h3>%s</h3>" % (esc(item["headline"]) or "(untitled)")
    return (
        "<article>\n"
        "  %s\n"
        '  <div class="byline">By %s &middot; %s</div>\n'
        "  %s\n"
        "</article>"
    ) % (headline, esc(item["byline"]), esc(str(item["year"])), body)


def section(title, items, preview=5):
    """Render a section showing the first few articles.

    The rest hide behind a <details> expander: pure HTML, no JavaScript,
    so it works in every sandbox the page might be served from.
    """
    if not items:
        return ""
    items = sorted(items, key=lambda i: (i["year"], i["headline"]))
    shown, rest = items[:preview], items[preview:]
    parts = ["<section>", "<h2>%s</h2>" % esc(title)]
    parts.extend(article(i) for i in shown)
    if rest:
        parts.append('<details class="more"><summary>Show all %d</summary>'
                     % len(items))
        parts.extend(article(i) for i in rest)
        parts.append("</details>")
    parts.append("</section>")
    return "\n".join(parts)


def find_reverts(commits):
    """Revert commits, the closest thing git has to public drama.

    A revert of a revert is flagged: somebody re-landed what somebody
    else undid, and the argument is now in the permanent record.
    """
    drama = []
    for item in commits:
        headline = item["headline"]
        if not headline.startswith("Revert "):
            continue
        entry = dict(item)
        if headline.startswith('Revert "Revert '):
            entry["headline"] = "Revert of a revert: " + headline
        drama.append(entry)
    return drama


LAUGH_TOKENS = ("haha", "lol", "lmao", "rofl", "hehe", "ha ha")
OOPS_TOKENS = ("oops", "woops", "oopsie", "uh oh", "my bad", "my fault")
SPICY_TOKENS = ("wtf", "damn", "dammit", "urgent", "asap", "sorry",
                "please", "broken", "yolo")


def has_emoji(text):
    """True when text contains an expressive emoji.

    Leading emoji is usually conventional (gitmoji puts it first), so
    only emoji past the first word counts. That is where the
    personality lives.
    """
    words = text.split(None, 1)
    expressive = words[1] if len(words) == 2 else ""
    return any(
        0x1F300 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF
        or 0xFE00 <= code <= 0xFE0F
        for code in map(ord, expressive))


def quote_score(text):
    """Heuristic funniness score for one commit message or comment line."""
    if not text:
        return 0
    low = text.lower()
    if low.startswith("merge ") or low.startswith("revert "):
        return 0
    score = 0
    if any(t in low for t in LAUGH_TOKENS):
        score += 3
    if any(t in low for t in OOPS_TOKENS):
        score += 3
    if any(t in low for t in SPICY_TOKENS):
        score += 2
    if has_emoji(text):
        score += 2
    score += min(text.count("!"), 3)
    score += min(text.count("?"), 2)
    if '"' in text or "\u201c" in text:
        score += 1
    if any(w.isalpha() and w.isupper() and len(w) >= 4
           for w in text.split()):
        score += 2
    if len(text) < 15:
        score -= 1
    return score


def find_quote(commits, comments, minimum=5):
    """The day's most quotable line, with its author.

    Returns None when nothing clears the bar: a forced funny quote is
    worse than no quote at all.
    """
    best = None
    best_score = 0
    for item in commits:
        scored = quote_score(item["headline"])
        if scored > best_score:
            best = {"text": item["headline"], "author": item["byline"],
                    "url": item["url"]}
            best_score = scored
    for item in comments:
        scored = quote_score(item["body"])
        if scored > best_score:
            best = {"text": item["body"], "author": item["byline"],
                    "url": item["url"]}
            best_score = scored
    return best if best_score >= minimum else None


def pullquote(quote):
    """A pull-quote box with attributed author, or "" when quoteless."""
    if not quote:
        return ""
    return (
        '<aside class="pullquote">\n'
        "  <p>%s</p>\n"
        '  <div class="who">&mdash; %s</div>\n'
        "</aside>"
    ) % (esc(quote["text"]), esc(quote["author"]))


MERGE_PR_RE = re.compile(r"^Merge pull request #(\d+) from (\S+)")
MERGE_BRANCH_RE = re.compile(r"^Merge branch '([^']+)'( into (\S+))?")


def find_merges(commits):
    """Merge commits as drama: whose branch landed, and where.

    A merge from another author's fork is flagged cross-author; a
    direct merge into main or master gets called out. Needs the
    commit item's "login" field for the cross-author check.
    """
    found = []
    for item in commits:
        msg = item["headline"]
        note = ""
        match = MERGE_PR_RE.match(msg)
        if match:
            source = match.group(2)
            owner = None
            for sep in (":", "/"):
                if sep in source:
                    owner = source.split(sep)[0]
                    break
            merger = item.get("login", "")
            if owner and merger and owner.lower() != merger.lower():
                note = ("Cross-author merge: %s merged @%s's branch."
                        % (merger, owner))
            else:
                note = "Merged branch %s." % source
        else:
            match = MERGE_BRANCH_RE.match(msg)
            if not match:
                continue
            target = match.group(3) or ""
            if target in ("main", "master"):
                note = "Straight into %s." % target
            else:
                note = "Merged branch '%s'." % match.group(1)
        found.append({
            "year": item["year"],
            "headline": msg,
            "byline": item["byline"],
            "body": note,
            "url": item["url"],
        })
    return found


def hottest_threads(opened, closed, limit=5, minimum=5):
    """The day's most-commented issues, deduped and hottest first."""
    seen = {}
    for item in opened + closed:
        seen[item["url"]] = item
    ranked = sorted(
        (i for i in seen.values() if i.get("comments", 0) >= minimum),
        key=lambda i: i["comments"], reverse=True)
    return ranked[:limit]


def build_lede(repo, md_long, counts, years, week=False):
    when = "In the week of %s" % md_long if week else "On %s" % md_long
    total = sum(counts.values())
    span = "%d-%d" % (years[0], years[-1]) if len(years) > 1 else str(years[0])
    if total == 0:
        return ("%s, the archives of %s are quiet. Across the years %s, "
                "nothing was committed, opened, closed, merged, or released "
                "in this period." % (when, repo, span))
    bits = []
    if counts.get("commits"):
        bits.append("%d commit%s" % (counts["commits"],
                                    "" if counts["commits"] == 1 else "s"))
    if counts.get("opened"):
        bits.append("%d issue%s opened" % (counts["opened"],
                                          "" if counts["opened"] == 1 else "s"))
    if counts.get("prs_opened"):
        bits.append("%d pull request%s opened" % (
            counts["prs_opened"], "" if counts["prs_opened"] == 1 else "s"))
    if counts.get("closed"):
        bits.append("%d closed" % counts["closed"])
    if counts.get("merged"):
        bits.append("%d pull request%s merged" % (
            counts["merged"], "" if counts["merged"] == 1 else "s"))
    if counts.get("merges"):
        bits.append("%d merge%s" % (counts["merges"],
                                    "" if counts["merges"] == 1 else "s"))
    if counts.get("releases"):
        bits.append("%d release%s" % (counts["releases"],
                                      "" if counts["releases"] == 1 else "s"))
    if counts.get("tags"):
        bits.append("%d tag%s cut" % (counts["tags"],
                                      "" if counts["tags"] == 1 else "s"))
    if counts.get("comments"):
        bits.append("%d notable comment%s" % (
            counts["comments"], "" if counts["comments"] == 1 else "s"))
    if counts.get("drama"):
        bits.append("%d scandal%s" % (counts["drama"],
                                      "" if counts["drama"] == 1 else "s"))
    return ("%s, across the years %s, %s saw %s."
            % (when, span, repo, "; ".join(bits)))


HERENOW_API = "https://here.now/api/v1"
HERENOW_CLIENT = "the-daily-commit"


def xkcd_link():
    """A random xkcd comic link for the footer, "" when unreachable.

    xkcd is free and serves JSON: the latest comic's number bounds the
    random pick. Two quick requests; failure never breaks the edition.
    """
    try:
        with urllib.request.urlopen("https://xkcd.com/info.0.json",
                                    timeout=10) as resp:
            latest = json.loads(resp.read().decode("utf-8"))["num"]
        num = random.randrange(1, latest + 1)
        if num == 404:
            num = 403
        with urllib.request.urlopen("https://xkcd.com/%d/info.0.json" % num,
                                    timeout=10) as resp:
            title = json.loads(resp.read().decode("utf-8")).get("title", "")
        text = "xkcd #%d: %s" % (num, title) if title else "xkcd #%d" % num
        return ' &middot; <a href="https://xkcd.com/%d/">%s</a>' % (
            num, esc(text))
    except Exception:  # noqa: BLE001 - footer treat, never fatal
        return ""


def share_html(path):
    """Publish path to here.now as an anonymous site.

    Returns the live URL (expires in 24 hours), or "" on failure.
    Anonymous publishing needs no account and no login.
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as exc:
        print("error: cannot read %s: %s" % (path, exc), file=sys.stderr)
        return ""
    manifest = {"files": [{
        "path": "index.html",
        "size": len(data),
        "contentType": "text/html; charset=utf-8",
        "hash": hashlib.sha256(data).hexdigest(),
    }]}
    headers = {"Content-Type": "application/json",
               "x-herenow-client": HERENOW_CLIENT,
               "User-Agent": "the-daily-commit-generator"}
    try:
        create_req = urllib.request.Request(
            HERENOW_API + "/publish",
            data=json.dumps(manifest).encode(), headers=headers)
        with urllib.request.urlopen(create_req, timeout=30) as resp:
            created = json.loads(resp.read().decode("utf-8"))
        if created.get("error"):
            raise ValueError(created["error"])
        upload = created["upload"]
        for item in upload["uploads"]:
            content_type = item["headers"].get(
                "Content-Type", "text/html; charset=utf-8")
            put_req = urllib.request.Request(
                item["url"], data=data, method="PUT",
                headers={"Content-Type": content_type})
            with urllib.request.urlopen(put_req, timeout=60) as put_resp:
                if not 200 <= put_resp.status < 300:
                    raise ValueError("upload HTTP %d" % put_resp.status)
        finalize_req = urllib.request.Request(
            upload["finalizeUrl"],
            data=json.dumps({"versionId": upload["versionId"]}).encode(),
            headers=headers)
        with urllib.request.urlopen(finalize_req, timeout=30) as resp:
            finalized = json.loads(resp.read().decode("utf-8"))
        if finalized.get("error"):
            raise ValueError(finalized["error"])
        return created.get("siteUrl") or ""
    except Exception as exc:  # noqa: BLE001 - report, never crash the run
        print("error: share failed: %s" % exc, file=sys.stderr)
        return ""


def tag_repo(items, repo):
    """Stamp gathered items with their repository."""
    for item in items:
        item["repo"] = repo
    return items


def day_by_day(pairs, week_days, span):
    """Per-weekday activity summaries across the years."""
    items = []
    for md, label in week_days:
        bits = []
        for name, lst in pairs:
            n = sum(1 for i in lst if i.get("day") == md)
            if n:
                bits.append("%d %s" % (n, name if n == 1 else name + "s"))
        if not bits:
            continue
        items.append({
            "year": span,
            "headline": label,
            "byline": "The week in review",
            "body": "; ".join(bits),
            "url": "",
        })
    return section("Day by Day", items)


def gather_repos(repos, mds, no_comments, week_days=()):
    """Edition data across one or more repositories."""
    current_year = dt.datetime.now().year
    data = {"commits": [], "opened": [], "closed": [], "merged": [],
            "releases": [], "tags": [], "comments": []}
    created = []
    private = False
    for repo in repos:
        info = api_get("/repos/%s" % repo)
        created.append(year_of(info.get("created_at", "2020-01-01T00:00:00Z")))
        private = private or bool(info.get("private"))
        years = list(range(created[-1], current_year + 1))[-MAX_YEARS:]
        data["commits"].extend(tag_repo(fetch_commits(repo, mds, years), repo))
        opened, closed = fetch_issues(repo, mds)
        data["opened"].extend(tag_repo(opened, repo))
        data["closed"].extend(tag_repo(closed, repo))
        data["merged"].extend(tag_repo(fetch_merged_prs(repo, mds), repo))
        repo_releases = tag_repo(fetch_releases(repo, mds), repo)
        data["releases"].extend(repo_releases)
        data["tags"].extend(tag_repo(
            fetch_tags(repo, mds,
                       {r["tag"] for r in repo_releases if r["tag"]}), repo))
        if not no_comments:
            data["comments"].extend(tag_repo(fetch_comments(repo, mds), repo))
    if len(repos) > 1:
        for item in (data["commits"] + data["opened"] + data["closed"]
                     + data["merged"] + data["releases"] + data["tags"]
                     + data["comments"]):
            item["byline"] = "%s · %s" % (item["byline"], item["repo"])
    years = list(range(min(created), current_year + 1))[-MAX_YEARS:]
    span = "%d-%d" % (years[0], years[-1]) if len(years) > 1 else str(years[0])
    counts = {key: len(items) for key, items in data.items()}
    drama = (find_reverts(data["commits"])
             + hottest_threads(data["opened"], data["closed"]))
    counts["drama"] = len(drama)
    quote = find_quote(data["commits"], data["comments"])
    sections = [
        pullquote(quote),
        section("Scandals & Corrections", drama),
        section("★ Releases", data["releases"]),
        section("From the Commit Ledger", data["commits"]),
        section("Tags Cut", data["tags"]),
        section("Issues Opened", data["opened"]),
        section("Issues Closed", data["closed"]),
        section("Pull Requests Merged", data["merged"]),
        section("Voices From the Threads", data["comments"]),
    ]
    if week_days:
        pairs = [("commit", data["commits"]), ("issue opened", data["opened"]),
                 ("issue closed", data["closed"]),
                 ("pull request merged", data["merged"]),
                 ("release", data["releases"]), ("tag", data["tags"]),
                 ("comment", data["comments"])]
        sections.insert(1, day_by_day(pairs, week_days, span))
    body = "\n".join(sections)
    single = len(repos) == 1
    return {
        "title": repos[0] if single else ", ".join(repos),
        "label": repos[0] if single else "%d repositories" % len(repos),
        "years": years,
        "counts": counts,
        "body": body,
        "record": ("Compiled from the private record" if private
                   else "Compiled from the public record"),
        "subject": {"repos": repos},
    }


def gather_author(user, mds, week_days=()):
    """Edition data for one user's full activity on the month-days.

    Commits, issues and PRs opened, PRs merged, issues closed, and the
    user's own comments, across every repository they touched. Built on
    the commit and issue search APIs, so it is paced for search rate
    limits.
    """
    info = api_get("/users/%s" % user)
    created_year = year_of(info.get("created_at", "2020-01-01T00:00:00Z"))
    current_year = dt.datetime.now().year
    years = list(range(created_year, current_year + 1))[-MAX_YEARS:]
    span = "%d-%d" % (years[0], years[-1]) if len(years) > 1 else str(years[0])
    commits = author_commits(user, mds, years)
    opened, prs_opened, merged, closed = author_issues(user, mds, years)
    comments = author_comments(user, mds, years)
    counts = {"commits": len(commits), "opened": len(opened),
              "prs_opened": len(prs_opened), "merged": len(merged),
              "closed": len(closed), "comments": len(comments)}
    drama = (find_reverts(commits)
             + hottest_threads(opened + prs_opened, closed))
    counts["drama"] = len(drama)
    merges = find_merges(commits)
    counts["merges"] = len(merges)
    quote = find_quote(commits, comments)
    sections = [
        pullquote(quote),
        section("Scandals & Corrections", drama),
        section("Merges", merges),
        section("From the Commit Ledger", commits),
        section("Issues Opened", opened),
        section("Pull Requests Opened", prs_opened),
        section("Pull Requests Merged", merged),
        section("Issues Closed", closed),
        section("Voices From the Threads", comments),
    ]
    if week_days:
        pairs = [("commit", commits), ("issue opened", opened),
                 ("pull request opened", prs_opened),
                 ("pull request merged", merged),
                 ("issue closed", closed), ("merge", merges),
                 ("comment", comments)]
        sections.insert(1, day_by_day(pairs, week_days, span))
    body = "\n".join(sections)
    return {
        "title": user,
        "label": user,
        "years": years,
        "counts": counts,
        "body": body,
        "record": ("Compiled from the private record" if resolve_token()
                   else "Compiled from the public record"),
        "subject": {"author": user},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate an 'on this day' newspaper page: one or more "
                    "repositories, or one author's activity across GitHub.")
    parser.add_argument("repos", nargs="*",
                        help="Repositories as owner/repo (one or more)")
    parser.add_argument("--author", default="",
                        help="Author mode: the month-day in USER's activity, "
                             "across every repository they touched")
    parser.add_argument("--date", default="",
                        help="Month-day as MM-DD (default: today, local time)")
    parser.add_argument("--out", default="",
                        help="Output HTML path (default: print a summary)")
    parser.add_argument("--no-comments", action="store_true",
                        help="Skip the comments section")
    parser.add_argument("--share", action="store_true",
                        help="Publish the edition to here.now anonymously "
                             "and print the shareable link (24-hour expiry, "
                             "no login required)")
    parser.add_argument("--version", action="version",
                        version="the-daily-commit " + __version__)
    parser.add_argument("--week", action="store_true",
                        help="Weekly edition: Monday to Friday of the week "
                             "containing --date (or this week), instead of "
                             "one day")
    parser.add_argument("--week-url", default="",
                        help="Author mode only: link the daily edition to "
                             "its weekly edition at this URL")
    args = parser.parse_args(argv)

    if args.author and args.repos:
        parser.error("--author cannot be combined with repositories")
    if not args.author and not args.repos:
        parser.error("give one or more owner/repo repositories, "
                     "or --author USER")
    if args.week_url and not args.author:
        parser.error("--week-url needs --author")

    now = dt.datetime.now()
    if args.date:
        target = dt.datetime.strptime("%d-%s" % (now.year, args.date),
                                      "%Y-%m-%d")
    else:
        target = now
    if args.week:
        monday = target - dt.timedelta(days=target.weekday())
        days = [monday + dt.timedelta(days=i) for i in range(5)]
        mds = [d.strftime("%m-%d") for d in days]
        week_days = [(d.strftime("%m-%d"),
                      d.strftime("%A, %B %d")) for d in days]
        first, last = days[0], days[-1]
        if first.month == last.month:
            md_long = "%s %d-%d, %d" % (first.strftime("%B"), first.day,
                                        last.day, first.year)
        else:
            md_long = "%s %d to %s %d, %d" % (
                first.strftime("%B"), first.day,
                last.strftime("%B"), last.day, last.year)
    else:
        md = target.strftime("%m-%d")
        mds = [md]
        week_days = []
        md_long = target.strftime("%B %d")

    if args.author:
        edition = gather_author(args.author, mds, week_days)
    else:
        edition = gather_repos(args.repos, mds, args.no_comments, week_days)

    body = edition["body"]
    if not body.strip():
        body = '<p class="quiet">A quiet day in the archives.</p>'
    if args.week_url:
        body = ('<p class="weeklink"><a href="%s">This week in %s →</a></p>\n'
                % (esc(args.week_url), esc(args.author))) + body

    script_dir = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(script_dir, "template.html"), encoding="utf-8") as f:
        template = f.read()
    page = (template
            .replace("{{TITLE}}", esc(edition["title"]))
            .replace("{{KICKER}}", "A weekly chronicle of repository history"
                     if args.week else "A daily chronicle of repository history")
            .replace("{{DATELINE}}", esc(md_long))
            .replace("{{RECORD}}", edition["record"])
            .replace("{{LEDE}}", esc(build_lede(edition["label"], md_long,
                                               edition["counts"],
                                               edition["years"],
                                               week=args.week)))
            .replace("{{BODY}}", body)
            .replace("{{XKCD}}", xkcd_link()))

    out_path = args.out
    if args.share and not out_path:
        fd, out_path = tempfile.mkstemp(suffix=".html",
                                        prefix="daily-commit-")
        os.close(fd)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(page)
    share_url = share_html(out_path) if args.share else ""
    summary = {"date": mds[0] if len(mds) == 1 else "%s..%s" % (mds[0],
                                                               mds[-1]),
               "counts": edition["counts"],
               "out": out_path or "(stdout summary only)",
               "share": share_url or None}
    summary.update(edition["subject"])
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
