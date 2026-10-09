#!/usr/bin/env python3
"""Generate a vintage-newspaper "on this day" page for a GitHub repository.

Collects everything that happened on one month-day across every year of a
repository's history: commits, issues opened and closed, pull requests
merged, releases published, and notable comments. Renders the result as a
self-contained HTML page styled like a vintage newspaper.

Usage:
    python3 generate.py owner/repo [--date MM-DD] [--out FILE] [--no-comments]

The month-day defaults to today (UTC). The script reads its credential from
the THE_DAILY_COMMIT_TOKEN environment variable, falling back to GITHUB_TOKEN.
Set one of them to raise the API rate limit and to cover private
repositories; without a token, the script uses the lower unauthenticated
rate limit and can only read public repositories. All API calls are
read-only GET requests, and nothing leaves the machine the script runs on.
"""

import argparse
import datetime as dt
import html
import json
import os
import sys
import urllib.parse
import urllib.request

API = "https://api.github.com"
MAX_ISSUE_PAGES = 10
MAX_PULL_PAGES = 5
MAX_RELEASE_PAGES = 3
MAX_COMMENT_PAGES = 2
MAX_YEARS = 25


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
    token = os.environ.get("THE_DAILY_COMMIT_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def api_paged(path, params, max_pages):
    """Collect up to max_pages of a paginated endpoint."""
    items = []
    params = dict(params or {})
    for page in range(1, max_pages + 1):
        params["page"] = page
        batch = api_get(path, params)
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


def esc(text):
    return html.escape(text or "", quote=True)


def first_line(text):
    line = (text or "").strip().split("\n")[0].strip()
    return line if len(line) <= 140 else line[:137] + "..."


def fetch_commits(repo, md, years):
    """Commits whose committer date falls on month-day, one call per year."""
    found = []
    for year in years:
        params = {
            "since": "%d-%sT00:00:00Z" % (year, md),
            "until": "%d-%sT23:59:59Z" % (year, md),
            "per_page": 100,
        }
        for commit in api_paged("/repos/%s/commits" % repo, params, 1):
            info = commit.get("commit", {})
            author = (info.get("author") or {}).get("name") or "unknown"
            found.append({
                "year": year,
                "headline": first_line(info.get("message")),
                "byline": author,
                "body": commit.get("sha", "")[:7],
                "url": commit.get("html_url", ""),
            })
    return found


def fetch_issues(repo, md):
    """Non-PR issues opened or closed on month-day, filtered client-side."""
    params = {"state": "all", "sort": "created", "direction": "asc",
              "per_page": 100}
    opened, closed = [], []
    for issue in api_paged("/repos/%s/issues" % repo, params, MAX_ISSUE_PAGES):
        if "pull_request" in issue:
            continue
        number = issue.get("number")
        url = issue.get("html_url", "")
        byline = (issue.get("user") or {}).get("login") or "unknown"
        if month_day(issue.get("created_at")) == md:
            opened.append({
                "year": year_of(issue["created_at"]),
                "headline": "#%s %s" % (number, first_line(issue.get("title"))),
                "byline": byline,
                "body": first_line(issue.get("body")),
                "url": url,
            })
        if month_day(issue.get("closed_at")) == md:
            closed.append({
                "year": year_of(issue["closed_at"]),
                "headline": "#%s %s" % (number, first_line(issue.get("title"))),
                "byline": byline,
                "body": first_line(issue.get("body")),
                "url": url,
            })
    return opened, closed


def fetch_merged_prs(repo, md):
    """Pull requests merged on month-day."""
    params = {"state": "closed", "sort": "updated", "direction": "desc",
              "per_page": 100}
    merged = []
    for pr in api_paged("/repos/%s/pulls" % repo, params, MAX_PULL_PAGES):
        if month_day(pr.get("merged_at")) != md:
            continue
        merged.append({
            "year": year_of(pr["merged_at"]),
            "headline": "#%s %s" % (pr.get("number"),
                                    first_line(pr.get("title"))),
            "byline": (pr.get("user") or {}).get("login") or "unknown",
            "body": first_line(pr.get("body")),
            "url": pr.get("html_url", ""),
        })
    return merged


def fetch_releases(repo, md):
    """Releases published on month-day."""
    released = []
    for rel in api_paged("/repos/%s/releases" % repo, {"per_page": 100},
                         MAX_RELEASE_PAGES):
        stamp = rel.get("published_at") or rel.get("created_at")
        if month_day(stamp) != md:
            continue
        released.append({
            "year": year_of(stamp),
            "headline": first_line(rel.get("name") or rel.get("tag_name")),
            "byline": (rel.get("author") or {}).get("login") or "unknown",
            "body": first_line(rel.get("body")),
            "url": rel.get("html_url", ""),
        })
    return released


def fetch_comments(repo, md):
    """Up to five issue comments written on month-day."""
    params = {"sort": "created", "direction": "desc", "per_page": 100}
    picked = []
    for comment in api_paged("/repos/%s/issues/comments" % repo, params,
                             MAX_COMMENT_PAGES):
        if month_day(comment.get("created_at")) != md:
            continue
        picked.append({
            "year": year_of(comment["created_at"]),
            "headline": "A voice from the threads",
            "byline": (comment.get("user") or {}).get("login") or "unknown",
            "body": first_line(comment.get("body")),
            "url": comment.get("html_url", ""),
        })
        if len(picked) >= 5:
            break
    return picked


def article(item):
    """Render one event as a newspaper article block."""
    body = "<p>%s</p>" % esc(item["body"]) if item["body"] else ""
    return (
        "<article>\n"
        '  <h3><a href="%s">%s</a></h3>\n'
        '  <div class="byline">By %s &middot; %s</div>\n'
        "  %s\n"
        "</article>"
    ) % (esc(item["url"]), esc(item["headline"]) or "(untitled)",
         esc(item["byline"]), esc(str(item["year"])), body)


def section(title, items):
    if not items:
        return ""
    items = sorted(items, key=lambda i: (i["year"], i["headline"]))
    return "<section>\n<h2>%s</h2>\n%s\n</section>" % (
        esc(title), "\n".join(article(i) for i in items))


def build_lede(repo, md_long, counts, years):
    parts = []
    total = sum(counts.values())
    span = "%d-%d" % (years[0], years[-1]) if len(years) > 1 else str(years[0])
    if total == 0:
        return ("On %s, the archives of %s are quiet. Across the years %s, "
                "nothing was committed, opened, closed, merged, or released "
                "on this date." % (md_long, repo, span))
    bits = []
    if counts["commits"]:
        bits.append("%d commit%s" % (counts["commits"],
                                    "" if counts["commits"] == 1 else "s"))
    if counts["opened"]:
        bits.append("%d issue%s opened" % (counts["opened"],
                                          "" if counts["opened"] == 1 else "s"))
    if counts["closed"]:
        bits.append("%d closed" % counts["closed"])
    if counts["merged"]:
        bits.append("%d pull request%s merged" % (
            counts["merged"], "" if counts["merged"] == 1 else "s"))
    if counts["releases"]:
        bits.append("%d release%s" % (counts["releases"],
                                      "" if counts["releases"] == 1 else "s"))
    if counts["comments"]:
        bits.append("%d notable comment%s" % (
            counts["comments"], "" if counts["comments"] == 1 else "s"))
    return ("On %s, across the years %s, %s saw %s."
            % (md_long, span, repo, "; ".join(bits)))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate an 'on this day' newspaper page for a repo.")
    parser.add_argument("repo", help="Repository as owner/repo")
    parser.add_argument("--date", default="",
                        help="Month-day as MM-DD (default: today, UTC)")
    parser.add_argument("--out", default="",
                        help="Output HTML path (default: print a summary)")
    parser.add_argument("--no-comments", action="store_true",
                        help="Skip the comments section")
    args = parser.parse_args(argv)

    if args.date:
        md = args.date
    else:
        md = dt.datetime.now(dt.timezone.utc).strftime("%m-%d")
    md_long = dt.datetime.strptime(md, "%m-%d").strftime("%B %d")

    info = api_get("/repos/%s" % args.repo)
    created_year = year_of(info.get("created_at", "2020-01-01T00:00:00Z"))
    current_year = dt.datetime.now(dt.timezone.utc).year
    years = list(range(created_year, current_year + 1))[-MAX_YEARS:]

    commits = fetch_commits(args.repo, md, years)
    opened, closed = fetch_issues(args.repo, md)
    merged = fetch_merged_prs(args.repo, md)
    releases = fetch_releases(args.repo, md)
    comments = [] if args.no_comments else fetch_comments(args.repo, md)

    counts = {"commits": len(commits), "opened": len(opened),
              "closed": len(closed), "merged": len(merged),
              "releases": len(releases), "comments": len(comments)}

    body = "\n".join([
        section("From the Commit Ledger", commits),
        section("Issues Opened", opened),
        section("Issues Closed", closed),
        section("Pull Requests Merged", merged),
        section("Releases", releases),
        section("Voices From the Threads", comments),
    ])
    if not body.strip():
        body = '<p class="quiet">A quiet day in the archives.</p>'

    script_dir = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(script_dir, "template.html"), encoding="utf-8") as f:
        template = f.read()
    page = (template
            .replace("{{TITLE}}", esc(args.repo))
            .replace("{{DATELINE}}", esc(md_long))
            .replace("{{LEDE}}", esc(build_lede(args.repo, md_long, counts,
                                               years)))
            .replace("{{BODY}}", body))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(page)
    print(json.dumps({"repo": args.repo, "date": md, "counts": counts,
                      "out": args.out or "(stdout summary only)"}))


if __name__ == "__main__":
    main()
