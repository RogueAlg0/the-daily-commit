#!/usr/bin/env python3
"""Generate a vintage-newspaper "on this day" page for a GitHub repository.

Collects everything that happened on one month-day across every year of a
repository's history: commits, issues opened and closed, pull requests
merged, releases published, tags cut, and notable comments. Renders the
result as a self-contained HTML page styled like a vintage newspaper,
with a gossip column up front: reverts (reverts of reverts get flagged)
and the day's most-commented threads.

Usage:
    python3 generate.py owner/repo [--date MM-DD] [--out FILE] [--no-comments]
    [--share]

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
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

API = "https://api.github.com"
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
        for commit in api_paged("/repos/%s/commits" % repo, params, 1,
                                 ignore_404=True):
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
    for issue in api_paged("/repos/%s/issues" % repo, params,
                             MAX_ISSUE_PAGES, ignore_404=True):
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
                "comments": issue.get("comments", 0),
            })
        if month_day(issue.get("closed_at")) == md:
            closed.append({
                "year": year_of(issue["closed_at"]),
                "headline": "#%s %s" % (number, first_line(issue.get("title"))),
                "byline": byline,
                "body": first_line(issue.get("body")),
                "url": url,
                "comments": issue.get("comments", 0),
            })
    return opened, closed


def fetch_merged_prs(repo, md):
    """Pull requests merged on month-day."""
    params = {"state": "closed", "sort": "updated", "direction": "desc",
              "per_page": 100}
    merged = []
    for pr in api_paged("/repos/%s/pulls" % repo, params, MAX_PULL_PAGES,
                          ignore_404=True):
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
                         MAX_RELEASE_PAGES, ignore_404=True):
        stamp = rel.get("published_at") or rel.get("created_at")
        if month_day(stamp) != md:
            continue
        released.append({
            "year": year_of(stamp),
            "headline": first_line(rel.get("name") or rel.get("tag_name")),
            "byline": (rel.get("author") or {}).get("login") or "unknown",
            "body": first_line(rel.get("body")),
            "url": rel.get("html_url", ""),
            "tag": rel.get("tag_name") or "",
        })
    return released


def fetch_tags(repo, md, skip_names):
    """Tags cut on month-day, best-effort for the most recent tags.

    The tags API carries no dates, so each tag's commit is resolved with
    one extra call, bounded by MAX_TAG_DATE_LOOKUPS. Tags that already
    have a GitHub release are skipped: the release announcement covers
    them.
    """
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
        if month_day(stamp) != md:
            continue
        author = ((info.get("author") or {}).get("name")
                  or (info.get("committer") or {}).get("name")
                  or "unknown")
        found.append({
            "year": year_of(stamp),
            "headline": name,
            "byline": author,
            "body": first_line(info.get("message")),
            "url": "https://github.com/%s/tree/%s" % (repo, name),
        })
    return found


def fetch_comments(repo, md):
    """Up to five issue comments written on month-day."""
    params = {"sort": "created", "direction": "desc", "per_page": 100}
    picked = []
    for comment in api_paged("/repos/%s/issues/comments" % repo, params,
                             MAX_COMMENT_PAGES, ignore_404=True):
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


def hottest_threads(opened, closed, limit=5, minimum=5):
    """The day's most-commented issues, deduped and hottest first."""
    seen = {}
    for item in opened + closed:
        seen[item["url"]] = item
    ranked = sorted(
        (i for i in seen.values() if i.get("comments", 0) >= minimum),
        key=lambda i: i["comments"], reverse=True)
    return ranked[:limit]


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
    if counts["tags"]:
        bits.append("%d tag%s cut" % (counts["tags"],
                                      "" if counts["tags"] == 1 else "s"))
    if counts["comments"]:
        bits.append("%d notable comment%s" % (
            counts["comments"], "" if counts["comments"] == 1 else "s"))
    if counts["drama"]:
        bits.append("%d scandal%s" % (counts["drama"],
                                      "" if counts["drama"] == 1 else "s"))
    return ("On %s, across the years %s, %s saw %s."
            % (md_long, span, repo, "; ".join(bits)))


HERENOW_API = "https://here.now/api/v1"
HERENOW_CLIENT = "the-daily-commit"


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


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate an 'on this day' newspaper page for a repo.")
    parser.add_argument("repo", help="Repository as owner/repo")
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
    args = parser.parse_args(argv)

    if args.date:
        md = args.date
    else:
        md = dt.datetime.now().strftime("%m-%d")
    md_long = dt.datetime.strptime(md, "%m-%d").strftime("%B %d")

    info = api_get("/repos/%s" % args.repo)
    created_year = year_of(info.get("created_at", "2020-01-01T00:00:00Z"))
    current_year = dt.datetime.now().year
    years = list(range(created_year, current_year + 1))[-MAX_YEARS:]

    commits = fetch_commits(args.repo, md, years)
    opened, closed = fetch_issues(args.repo, md)
    merged = fetch_merged_prs(args.repo, md)
    releases = fetch_releases(args.repo, md)
    release_tags = {r["tag"] for r in releases if r["tag"]}
    tags = fetch_tags(args.repo, md, release_tags)
    comments = [] if args.no_comments else fetch_comments(args.repo, md)

    counts = {"commits": len(commits), "opened": len(opened),
              "closed": len(closed), "merged": len(merged),
              "releases": len(releases), "tags": len(tags),
              "comments": len(comments)}
    drama = find_reverts(commits) + hottest_threads(opened, closed)
    counts["drama"] = len(drama)

    body = "\n".join([
        section("Scandals & Corrections", drama),
        section("\u2605 Releases", releases),
        section("From the Commit Ledger", commits),
        section("Tags Cut", tags),
        section("Issues Opened", opened),
        section("Issues Closed", closed),
        section("Pull Requests Merged", merged),
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
            .replace("{{RECORD}}", "Compiled from the private record"
                     if info.get("private") else
                     "Compiled from the public record")
            .replace("{{LEDE}}", esc(build_lede(args.repo, md_long, counts,
                                               years)))
            .replace("{{BODY}}", body))

    out_path = args.out
    if args.share and not out_path:
        fd, out_path = tempfile.mkstemp(suffix=".html",
                                        prefix="daily-commit-")
        os.close(fd)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(page)
    share_url = share_html(out_path) if args.share else ""
    print(json.dumps({"repo": args.repo, "date": md, "counts": counts,
                      "out": out_path or "(stdout summary only)",
                      "share": share_url or None}))


if __name__ == "__main__":
    main()
