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
__version__ = "0.1.1"
MAX_ISSUE_PAGES = 10
MAX_PULL_PAGES = 5
MAX_RELEASE_PAGES = 3
MAX_COMMENT_PAGES = 2
MAX_COMMIT_PAGES = 3
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


def _straddles_new_year(mds):
    """True when the month-days span December and January."""
    months = {md[:2] for md in mds if len(md) >= 2}
    return "12" in months and "01" in months


def year_ranges(year, mds):
    """(since, until) ISO ranges covering the month-days within one year.

    A week can straddle New Year; then it becomes two ranges.
    """
    jan = sorted(md for md in mds if md.startswith("01-"))
    dec = sorted(md for md in mds if md.startswith("12-"))
    if jan and dec:
        return [("%d-%sT00:00:00Z" % (year, jan[0]),
                 "%d-%sT23:59:59.999Z" % (year, jan[-1])),
                ("%d-%sT00:00:00Z" % (year, dec[0]),
                 "%d-%sT23:59:59.999Z" % (year, dec[-1]))]
    lo, hi = min(mds), max(mds)
    return [("%d-%sT00:00:00Z" % (year, lo),
             "%d-%sT23:59:59.999Z" % (year, hi))]


def search_ranges(year, mds):
    """GitHub search date ranges covering the month-days within one year."""
    jan = sorted(md for md in mds if md.startswith("01-"))
    dec = sorted(md for md in mds if md.startswith("12-"))
    if jan and dec:
        return ["%d-%s..%d-%s" % (year, jan[0], year, jan[-1]),
                "%d-%s..%d-%s" % (year, dec[0], year, dec[-1])]
    lo, hi = min(mds), max(mds)
    return ["%d-%s..%d-%s" % (year, lo, year, hi)]


def esc(text):
    return html.escape(text or "", quote=True)


def _title_link(title):
    """Repo title as a hyperlink, when it looks like owner/repo."""
    if "/" in title and " " not in title:
        url = "https://github.com/" + title
        return '<a href="%s">%s</a>' % (esc(url), esc(title))
    return esc(title)


def profile_url(login):
    """GitHub profile URL for a login, or "" when not linkable."""
    if login and login != "unknown" and re.match(r"^[A-Za-z0-9-]+$", login):
        return "https://github.com/" + login
    return ""


class Platform:
    """A git hosting platform: URL patterns for web links.

    Subclasses define the web URL shapes for commits, pull requests,
    issues, releases, and user profiles. The API client for data
    fetching is separate (see GitHubAPI below).
    """
    name = "unknown"
    web_base = ""

    def repo_url(self, owner, repo):
        return "%s/%s/%s" % (self.web_base, owner, repo)

    def commit_url(self, owner, repo, sha):
        raise NotImplementedError

    def pr_url(self, owner, repo, number):
        raise NotImplementedError

    def issue_url(self, owner, repo, number):
        raise NotImplementedError

    def profile_url(self, login):
        if login and login != "unknown":
            return "%s/%s" % (self.web_base, login)
        return ""


class GitHubPlatform(Platform):
    name = "github"
    web_base = "https://github.com"

    def commit_url(self, owner, repo, sha):
        return "%s/%s/%s/commit/%s" % (self.web_base, owner, repo, sha)

    def pr_url(self, owner, repo, number):
        return "%s/%s/%s/pull/%s" % (self.web_base, owner, repo, number)

    def issue_url(self, owner, repo, number):
        return "%s/%s/%s/issues/%s" % (self.web_base, owner, repo, number)


class GitLabPlatform(Platform):
    name = "gitlab"
    web_base = "https://gitlab.com"

    def commit_url(self, owner, repo, sha):
        return "%s/%s/%s/-/commit/%s" % (self.web_base, owner, repo, sha)

    def pr_url(self, owner, repo, number):
        return "%s/%s/%s/-/merge_requests/%s" % (
            self.web_base, owner, repo, number)

    def issue_url(self, owner, repo, number):
        return "%s/%s/%s/-/issues/%s" % (self.web_base, owner, repo, number)


class BitbucketPlatform(Platform):
    name = "bitbucket"
    web_base = "https://bitbucket.org"

    def commit_url(self, owner, repo, sha):
        return "%s/%s/%s/commits/%s" % (self.web_base, owner, repo, sha)

    def pr_url(self, owner, repo, number):
        return "%s/%s/%s/pull-requests/%s" % (
            self.web_base, owner, repo, number)

    def issue_url(self, owner, repo, number):
        return "%s/%s/%s/issues/%s" % (self.web_base, owner, repo, number)


class GiteaPlatform(Platform):
    """Gitea and Forgejo: GitHub-compatible URL shapes."""
    name = "gitea"
    web_base = ""

    def __init__(self, base_url=""):
        self.web_base = base_url.rstrip("/")

    def commit_url(self, owner, repo, sha):
        return "%s/%s/%s/commit/%s" % (self.web_base, owner, repo, sha)

    def pr_url(self, owner, repo, number):
        return "%s/%s/%s/pulls/%s" % (self.web_base, owner, repo, number)

    def issue_url(self, owner, repo, number):
        return "%s/%s/%s/issues/%s" % (self.web_base, owner, repo, number)


class GenericGitPlatform(Platform):
    """Fallback for any git host: uses GitHub-style URLs.

    Most git forges (Gitea, Forgejo, SourceHut, etc.) copy GitHub's
    URL shapes. When the platform is unknown, this is the best guess.
    Override with --platform if the host uses different patterns.
    """
    name = "generic"

    def __init__(self, base_url=""):
        self.web_base = base_url.rstrip("/")

    def commit_url(self, owner, repo, sha):
        return "%s/%s/%s/commit/%s" % (self.web_base, owner, repo, sha)

    def pr_url(self, owner, repo, number):
        return "%s/%s/%s/pull/%s" % (self.web_base, owner, repo, number)

    def issue_url(self, owner, repo, number):
        return "%s/%s/%s/issues/%s" % (self.web_base, owner, repo, number)


PLATFORMS = {
    "github.com": GitHubPlatform(),
    "gitlab.com": GitLabPlatform(),
    "bitbucket.org": BitbucketPlatform(),
}

# Hosts that run Gitea/Forgejo (GitHub-compatible). Add more as found.
GITEA_HOSTS = {"gitea.com", "codeberg.org"}


def detect_platform(repo_ref, platform_hint=""):
    """Detect the platform from a repo reference.

    Accepts "owner/repo" (assumes GitHub), a full HTTPS URL, or an
    SSH-style "git@host:owner/repo.git". Returns (platform, owner, repo).

    The platform_hint ("github", "gitlab", "bitbucket", "gitea",
    "generic") overrides auto-detection for self-hosted or unusual
    forges. Unknown hosts fall back to GenericGitPlatform with
    GitHub-style URLs.
    """
    if platform_hint:
        hint = platform_hint.lower()
        if hint == "github":
            plat = GitHubPlatform()
        elif hint == "gitlab":
            plat = GitLabPlatform()
        elif hint == "bitbucket":
            plat = BitbucketPlatform()
        elif hint in ("gitea", "forgejo"):
            plat = GiteaPlatform()
        else:
            plat = GenericGitPlatform()
        # Re-parse just to get owner/repo/host.
        _, owner, repo, host = _parse_repo_ref(repo_ref)
        if hasattr(plat, "web_base") and not plat.web_base and host:
            plat.web_base = "https://" + host
        return plat, owner, repo

    platform, owner, repo, host = _parse_repo_ref(repo_ref)
    return platform, owner, repo


def _parse_repo_ref(repo_ref):
    """Parse a repo ref into (platform, owner, repo, host)."""
    # SSH style: git@github.com:owner/repo.git
    m = re.match(r"git@([^:]+):([^/]+)/(.+?)(?:\.git)?$", repo_ref)
    if m:
        host, owner, repo = m.groups()
        return _platform_for_host(host), owner, repo, host
    # HTTPS URL: https://github.com/owner/repo
    m = re.match(r"https?://([^/]+)/([^/]+)/(.+?)(?:\.git)?$", repo_ref)
    if m:
        host, owner, repo = m.groups()
        return _platform_for_host(host), owner, repo, host
    # Bare owner/repo: assume GitHub (backwards compatible).
    if "/" in repo_ref:
        owner, repo = repo_ref.split("/", 1)
        return GitHubPlatform(), owner, repo, "github.com"
    raise ValueError("Cannot parse repo reference: %s" % repo_ref)


def _platform_for_host(host):
    """Return the Platform for a hostname, with smart fallbacks."""
    host = host.lower()
    if host in PLATFORMS:
        return PLATFORMS[host]
    if host in GITEA_HOSTS:
        return GiteaPlatform("https://" + host)
    # Self-hosted GitLab often has "gitlab" in the hostname.
    if "gitlab" in host:
        plat = GitLabPlatform()
        plat.web_base = "https://" + host
        return plat
    # Unknown host: generic GitHub-style URLs. Works for most forges.
    return GenericGitPlatform("https://" + host)


class APIClient:
    """Abstract data source for one platform.

    Each platform implements these to fetch the raw activity data.
    The newspaper logic (filtering by month-day, scoring, rendering)
    stays platform-agnostic and works on the normalized item dicts.
    """

    def __init__(self, platform, token=""):
        self.platform = platform
        self.token = token

    def repo_info(self, owner, repo):
        """Basic repo metadata: created_at, private, etc."""
        raise NotImplementedError

    def commits(self, owner, repo, since, until):
        """Commits in the date range. Yields normalized dicts."""
        raise NotImplementedError

    def issues(self, owner, repo, state="all"):
        """Issues (not PRs) with created/closed dates."""
        raise NotImplementedError

    def pull_requests(self, owner, repo, state="closed"):
        """Pull/merge requests with merged dates."""
        raise NotImplementedError

    def releases(self, owner, repo):
        """Releases with published dates."""
        raise NotImplementedError

    def comments(self, owner, repo, since, until):
        """Issue/PR comments in the date range."""
        raise NotImplementedError


class GitHubAPIClient(APIClient):
    """GitHub REST API client. Wraps the existing api_get/api_paged."""

    def repo_info(self, owner, repo):
        return api_get("/repos/%s/%s" % (owner, repo))

    # commits(), issues(), etc. delegate to the existing fetch_*
    # functions, which already speak GitHub REST. GitLab and Bitbucket
    # subclasses will override with their own endpoints and pagination.


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
            for commit in api_paged("/repos/%s/commits" % repo, params,
                                    MAX_COMMIT_PAGES, ignore_404=True):
                info = commit.get("commit", {})
                author = (info.get("author") or {}).get("name") or "unknown"
                day = month_day((info.get("committer") or {}).get("date"))
                if day not in mds:
                    continue
                login = (commit.get("author") or {}).get("login") or ""
                found.append({
                    "year": year,
                    "day": day,
                    "headline": first_line(info.get("message")),
                    "byline": author,
                    "body": commit.get("sha", "")[:7],
                    "url": commit.get("html_url", ""),
                    "login": login,
                    "author_url": profile_url(login),
                })
    return found


def fetch_issues(repo, mds):
    """Non-PR issues opened or closed on the month-days, client-filtered.

    Fetches from both ends (oldest and newest) so large repositories do
    not lose recent issues to the page cap.
    """
    mds = set(mds)
    seen = set()
    opened, closed = [], []
    for direction in ("asc", "desc"):
        params = {"state": "all", "sort": "created",
                  "direction": direction, "per_page": 100}
        for issue in api_paged("/repos/%s/issues" % repo, params,
                               MAX_ISSUE_PAGES, ignore_404=True):
            if "pull_request" in issue:
                continue
            number = issue.get("number")
            if number in seen:
                continue
            seen.add(number)
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
                    "author_url": profile_url(byline),
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
                    "author_url": profile_url(byline),
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


_repo_private_cache = {}


def repo_is_private(owner, repo):
    """True when the repository is private, cached for the run."""
    key = "%s/%s" % (owner, repo)
    if key not in _repo_private_cache:
        try:
            info = api_get("/repos/%s" % key)
            _repo_private_cache[key] = bool(info.get("private"))
        except urllib.error.HTTPError:
            _repo_private_cache[key] = False
    return _repo_private_cache[key]


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
                private = bool((hit.get("repository") or {}).get("private"))
                found.append({
                    "year": year_of(stamp),
                    "day": day,
                    "headline": first_line(info.get("message")),
                    "byline": "%s · %s" % (user, repo),
                    "body": hit.get("sha", "")[:7],
                    "url": hit.get("html_url", ""),
                    "login": user,
                    "private": private,
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


def article(item, style=""):
    """Render one event as a newspaper article block.

    Style can be "lead-story" (big, spans columns), "brief" (compact
    one-liner), or "" (standard).
    """
    body = "<p>%s</p>" % esc(item["body"]) if item["body"] else ""
    if item["url"]:
        headline = '<h3><a href="%s">%s</a></h3>' % (
            esc(item["url"]), esc(item["headline"]) or "(untitled)")
    else:
        headline = "<h3>%s</h3>" % (esc(item["headline"]) or "(untitled)")
    # Link the author name when we have their GitHub profile URL.
    # The byline may be "author" or "author · repo"; link the author part.
    # Falls back to treating the byline author as a GitHub login.
    byline = item["byline"]
    author_url = item.get("author_url")
    if not author_url:
        author_url = profile_url(byline.split(" · ", 1)[0])
    if author_url:
        parts = byline.split(" · ", 1)
        parts[0] = '<a href="%s">%s</a>' % (
            esc(author_url), esc(parts[0]))
        byline_html = " · ".join([parts[0]] +
                                 [esc(p) for p in parts[1:]])
    else:
        byline_html = esc(byline)
    cls = ' class="%s"' % style if style else ""
    return (
        "<article%s>\n"
        "  %s\n"
        '  <div class="byline">By %s &middot; %s</div>\n'
        "  %s\n"
        "</article>"
    ) % (cls, headline, byline_html, esc(str(item["year"])), body)


def section(title, items, preview=5, lead=False, brief=False):
    """Render a section showing the first few articles.

    The rest hide behind a <details> expander: pure HTML, no JavaScript,
    so it works in every sandbox the page might be served from.

    Lead=True makes the first item a big lead story spanning columns.
    Brief=True renders all items as compact one-liners.
    """
    if not items:
        return ""
    items = sorted(items, key=lambda i: (i["year"], i["headline"]))
    shown, rest = items[:preview], items[preview:]
    parts = ["<section>", "<h2>%s</h2>" % esc(title)]
    for idx, item in enumerate(shown):
        if brief:
            parts.append(article(item, style="brief"))
        elif lead and idx == 0:
            parts.append(article(item, style="lead-story"))
        else:
            parts.append(article(item))
    if rest:
        parts.append('<details class="more"><summary>Show all %d</summary>'
                     % len(items))
        style = "brief" if brief else ""
        parts.extend(article(i, style=style) for i in rest)
        parts.append("</details>")
    parts.append("</section>")
    return "\n".join(parts)


def hottest_threads(opened, closed, limit=5, minimum=5):
    """The day's most-commented issues, deduped and hottest first."""
    seen = {}
    for item in opened + closed:
        seen[item["url"]] = item
    ranked = sorted(
        (i for i in seen.values() if i.get("comments", 0) >= minimum),
        key=lambda i: i["comments"], reverse=True)
    return ranked[:limit]


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


# Words people write when they're confessing to a mistake.
CONFESSION_TOKENS = (
    "oops", "woops", "oopsie", "my bad", "my fault", "sorry",
    "mistake", "broke", "broken", "fix my", "revert my",
)
# Words that signal a temporary hack.
HACK_TOKENS = (
    "hack", "temp", "temporary", "wip", "do not merge", "dont merge",
    "please work", "yolo", "magic",
)
# "Final" is never final.
FINAL_TOKENS = ("final", "really final", "actually final", "last fix")
# Author doesn't know why it works.
IDK_TOKENS = (
    "don't know", "dont know", "not sure", "no idea", "magic",
    "no clue", "works somehow", "don't ask",
)
# Desperation.
DESPERATE_TOKENS = (
    "please", "help", "wtf", "urgent", "asap", "emergency",
    "critical", "broken prod", "prod is down",
)
# Exhaustion.
TIRED_TOKENS = (
    "tired", "late night", "3am", "midnight", "exhausted",
    "sleep", "weekend",
)
# Blaming the tool.
BLAME_TOOL_TOKENS = (
    "stupid", "damn linter", "webpack", "npm", "gradle",
    "xcode", "android studio",
)
# Overconfidence.
OVERCONFIDENT_TOKENS = (
    "should work", "probably", "hopefully", "fingers crossed",
    "trust me",
)
# Underconfidence.
UNDERCONFIDENT_TOKENS = (
    "maybe", "might fix", "try", "attempt", "see if",
)
# Typo fixes (the humblest commits).
TYPO_TOKENS = ("typo", "spelling", "whitespace", "semicolon", "lint")
# Merge pain.
MERGE_HELL_TOKENS = (
    "merge conflict", "conflict", "resolve", "damn merge",
)
# Works on my machine.
LOCAL_TOKENS = (
    "works locally", "my machine", "can't reproduce", "cant reproduce",
)
# Food-powered.
FOOD_TOKENS = ("coffee", "pizza", "beer", "caffeine")
# Deletions (satisfying).
DELETE_TOKENS = (
    "remove dead", "delete", "cleanup", "dead code", "kill",
)
# First commits.
FIRST_TOKENS = ("initial commit", "first commit", "hello world")
# Performance.
PERF_TOKENS = ("perf", "optimize", "faster", "slow", "speed up")
# Security.
SECURITY_TOKENS = ("security", "vuln", "cve", "auth fix")


# Registry of quip types: (name, tokens).
QUIP_REGISTRY = [
    ("confession", CONFESSION_TOKENS),
    ("hack", HACK_TOKENS),
    ("final", FINAL_TOKENS),
    ("idk", IDK_TOKENS),
    ("desperate", DESPERATE_TOKENS),
    ("tired", TIRED_TOKENS),
    ("blame_tool", BLAME_TOOL_TOKENS),
    ("overconfident", OVERCONFIDENT_TOKENS),
    ("underconfident", UNDERCONFIDENT_TOKENS),
    ("typo", TYPO_TOKENS),
    ("merge_hell", MERGE_HELL_TOKENS),
    ("local", LOCAL_TOKENS),
    ("food", FOOD_TOKENS),
    ("delete", DELETE_TOKENS),
    ("first", FIRST_TOKENS),
    ("perf", PERF_TOKENS),
    ("security", SECURITY_TOKENS),
]


def find_quips(commits):
    """All quip types in one pass. Returns {quip_name: [items]}.

    Content-based only: matches words people actually wrote.
    No timestamps, no counts, no math.
    """
    results = {name: [] for name, _ in QUIP_REGISTRY}
    results["shouty"] = []
    results["question"] = []
    for item in commits:
        headline = item["headline"]
        low = headline.lower()
        for name, tokens in QUIP_REGISTRY:
            if any(t in low for t in tokens):
                results[name].append(item)
        # ALL CAPS and trailing "?" need their own checks.
        words = [w for w in headline.split() if w.isalpha()]
        if len(words) >= 3 and all(w.isupper() for w in words):
            results["shouty"].append(item)
        if headline.strip().endswith("?"):
            results["question"].append(item)
    return {k: v for k, v in results.items() if v}


def overheard_box(quips):
    """Sidebar of funny things people wrote. Not inline articles.

    A newspaper doesn't list every quip as a full story; it puts
    the best ones in a box. That's what this is.
    """
    if not quips:
        return ""
    parts = ['<div class="overheard">', "<h3>Overheard</h3>"]
    for item in quips[:6]:
        who = item.get("byline", "")
        if " · " in who:
            who = who.split(" · ", 1)[0]
        parts.append(
            '<div class="quip">"%s" <span class="who">&mdash; %s</span></div>'
            % (esc(item["headline"]), esc(who)))
    parts.append("</div>")
    return "\n".join(parts)


def new_voices_box(voices, seed=""):
    """Post-it notes for first-time contributors.

    Not a formal box. Little yellow stickies, slightly rotated,
    like someone stuck them on the newspaper. Each gets a random
    tilt and a short, varied message (seeded for consistency).
    """
    if not voices:
        return ""
    import random
    rng = random.Random(seed)
    # Short, punchy, like real Post-its. Not sentences.
    messages = [
        "hi %s!",
        "hey %s!",
        "%s is new!",
        "welcome %s",
        "say hi to %s",
        "fresh: %s",
        "%s joined!",
        "new kid: %s",
    ]
    parts = ['<div class="postit-stack">']
    for v in voices[:5]:
        login = v["byline"]
        url = v.get("author_url") or ("https://github.com/" + login)
        tilt = rng.uniform(-3, 3)
        msg = rng.choice(messages) % ('<a href="%s">%s</a>' % (esc(url), esc(login)))
        parts.append(
            '<div class="postit" style="transform: rotate(%.1fdeg)">%s</div>'
            % (tilt, msg))
    parts.append("</div>")
    return "\n".join(parts)


def charm_box(title, items, style=""):
    """A charm section with its own personality.

    Each type gets a distinct visual style, not just a boring box.
    Style is a CSS class suffix: weather, marriage, letter, missed, classified.
    """
    if not items:
        return ""
    cls = "charm-box"
    if style:
        cls += " charm-%s" % style
    parts = ['<div class="%s">' % cls, "<h4>%s</h4>" % esc(title)]
    for item in items:
        parts.append('<div class="item">')
        if item.get("headline"):
            parts.append('<span class="headline">%s</span>' %
                         esc(item["headline"]))
        if item.get("body"):
            parts.append('<br>%s' % esc(item["body"]))
        parts.append('</div>')
    parts.append("</div>")
    return "\n".join(parts)


def quip_text(item):
    """The stable identity string of a quip item for memory hashing."""
    return item.get("headline", "")


def curate_drama(candidates_by_type, limit=5, seed="", memory=None,
                 within=6, current_key=""):
    """A newspaper editor, not a firehose.

    Takes dict of {type_name: [items]}, picks up to `limit` total,
    spreading picks across types for variety. Seeded for consistency:
    the same edition shows the same selection. Random, but respectful.

    When `memory` (an EditionMemory) is given, quips seen in recent
    editions sort after fresh ones, so repeats fade without ever
    forcing an empty pick: if every candidate was seen, the least
    stale ones still run. `current_key` excludes the edition being
    rendered, so re-renders stay byte-identical.
    """
    import random
    rng = random.Random(seed)
    # Shuffle types, then round-robin pick one from each.
    types = list(candidates_by_type.keys())
    rng.shuffle(types)
    picked = []
    pools = {t: list(items) for t, items in candidates_by_type.items()}
    for t in pools:
        rng.shuffle(pools[t])
        if memory is not None:
            # Stable partition: seen quips sink to the front so the
            # pop() below takes fresh ones first. Falls back to seen
            # ones when the pool of fresh quips runs dry.
            pools[t].sort(key=lambda item: memory.seen_quip(
                quip_text(item), within=within, exclude_key=current_key),
                reverse=True)
    while len(picked) < limit:
        progressed = False
        for t in types:
            if pools[t] and len(picked) < limit:
                picked.append(pools[t].pop())
                progressed = True
        if not progressed:
            break
    return picked


def funny_branch_name(branch):
    """Extract the human part of a branch name for quoting.

    "fix/PROJ-123-please-work" -> "please-work". Skips boring names.
    """
    if not branch:
        return ""
    # Take the last segment, strip ticket keys.
    part = branch.split("/")[-1]
    part = TICKET_RE.sub("", part).strip("-_")
    # Skip if it's just a ticket key or too short to be funny.
    if len(part) < 4:
        return ""
    # Skip purely descriptive names.
    boring = ("main", "master", "develop", "feature", "fix", "hotfix",
              "release", "test")
    if part.lower() in boring:
        return ""
    return part


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
TICKET_RE = re.compile(r"\b[A-Z]{2,10}-\d{1,5}\b")


def find_tickets(text):
    """Ticket keys (ABC-123) in text, deduplicated and sorted."""
    return sorted(set(TICKET_RE.findall(text or "")))


def ticket_link(key, base_url):
    """Link to the ticket in the tracker, or the bare key."""
    if base_url:
        return '<a href="%s/%s">%s</a>' % (
            esc(base_url.rstrip("/")), esc(key), esc(key))
    return esc(key)


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


def find_new_voices(commits):
    """First-time contributors appearing in the paper.

    An author whose earliest commit in the fetched history is from
    the most recent year is a new voice: they weren't in the paper
    in prior years. Celebrate them.
    """
    if not commits:
        return []
    by_author = {}
    for item in commits:
        login = item.get("login") or item.get("byline", "")
        if not login or login == "unknown":
            continue
        by_author.setdefault(login, []).append(item)
    max_year = max(i["year"] for i in commits)
    voices = []
    for login, items in by_author.items():
        earliest = min(i["year"] for i in items)
        if earliest == max_year and len(items) <= 3:
            # First appearance, and not too many (not a regular).
            first = min(items, key=lambda i: i["year"])
            voices.append({
                "year": first["year"],
                "headline": "Welcome, %s!" % login,
                "byline": login,
                "body": "First appearance in the paper with: %s" %
                        first["headline"][:60],
                "url": first["url"],
                "author_url": profile_url(login),
            })
    return voices


class EditionMemory:
    """Lean cross-edition memory. Never bloats.

    Stores only hashes and counters, never full text. Bounded to
    MAX_EDITIONS entries; each entry holds at most MAX_QUIPS quip
    hashes, MAX_ADS ad hashes, and MAX_CONTRIBUTORS contributor
    hashes. record() is idempotent per edition key: re-rendering
    the same edition updates its entry in place instead of
    appending a duplicate.
    """
    MAX_EDITIONS = 10
    # Caps chosen so the file can never bloat: 5 quip hashes (curate
    # picks at most 5), 3 ad hashes (3 ads per edition), 10 contributor
    # hashes, everything else hashed to 8 chars. Worst case measured
    # at 2,494 bytes.
    MAX_QUIPS = 5
    MAX_ADS = 3
    MAX_CONTRIBUTORS = 10
    FILENAME = "editions.json"

    def __init__(self, path=None):
        import os
        if path is None:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            path = os.path.join(script_dir, self.FILENAME)
        self.path = path
        self.data = {"editions": []}
        self._load()

    def _load(self):
        import json, os
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, OSError, ValueError):
                self.data = {"editions": []}
        if not isinstance(self.data.get("editions"), list):
            self.data = {"editions": []}

    def _save(self):
        import json, os
        # Prune to last N before saving.
        self.data["editions"] = self.data["editions"][-self.MAX_EDITIONS:]
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, separators=(",", ":"))
        os.replace(tmp, self.path)

    def _hash(self, text):
        import hashlib
        return hashlib.md5(text.encode("utf-8")).hexdigest()[:8]

    def _recent(self, within, exclude_key=""):
        editions = self.data["editions"][-within:]
        if exclude_key:
            kh = self._hash(exclude_key)
            editions = [e for e in editions if e.get("k") != kh]
        return editions

    def record(self, key, quips=(), ads=(), contributors=(),
               lead_headline=""):
        """Record one edition. All inputs are strings; we store hashes.

        Re-rendering the same edition key updates its entry in place,
        so regeneration stays byte-identical.
        """
        edition = {
            "k": self._hash(key),
            "q": [self._hash(q) for q in quips[:self.MAX_QUIPS]],
            "a": [self._hash(a) for a in ads[:self.MAX_ADS]],
            # Hashed logins, sorted for stable output across processes.
            "c": sorted({self._hash(c) for c in contributors if c})[
                :self.MAX_CONTRIBUTORS],
            "l": self._hash(lead_headline) if lead_headline else "",
        }
        kh = edition["k"]
        self.data["editions"] = [
            e for e in self.data["editions"] if e.get("k") != kh]
        self.data["editions"].append(edition)
        self._save()

    def seen_quip(self, quip_text, within=6, exclude_key=""):
        """Has this quip appeared in the last N editions?"""
        h = self._hash(quip_text)
        for e in self._recent(within, exclude_key):
            if h in e.get("q", []):
                return True
        return False

    def seen_ad(self, ad_text, within=6, exclude_key=""):
        h = self._hash(ad_text)
        for e in self._recent(within, exclude_key):
            if h in e.get("a", []):
                return True
        return False

    def contributor_streak(self, login):
        """How many recent editions featured this contributor?"""
        lh = self._hash(login)
        count = 0
        for e in reversed(self.data["editions"]):
            if lh in e.get("c", []):
                count += 1
            else:
                break
        return count

    def size_bytes(self):
        import os
        if os.path.exists(self.path):
            return os.path.getsize(self.path)
        return 0


def repo_weather(commits, merges, issues):
    """Commit activity as a weather report.

    Pure charm: the repo's activity becomes a forecast.
    """
    n_commits = len(commits)
    n_merges = len(merges)
    n_issues = len(issues)
    if n_commits == 0:
        return "Clear skies. No activity today."
    parts = []
    if n_commits >= 20:
        parts.append("Heavy commit showers (%d)" % n_commits)
    elif n_commits >= 10:
        parts.append("Steady commits (%d)" % n_commits)
    elif n_commits >= 5:
        parts.append("Light commit drizzle (%d)" % n_commits)
    else:
        parts.append("A few commits (%d)" % n_commits)
    if n_merges > 0:
        parts.append("with a chance of merges (%d)" % n_merges)
    if n_issues > 0:
        parts.append("and scattered issues (%d)" % n_issues)
    return ", ".join(parts) + "."


def marriage_announcements(merges, seed=""):
    """Merged branches as wedding announcements.

    Each gets a different ceremony variant, picked with the edition
    seed. Hotfix branches get the rushed chapel line.
    """
    if not merges:
        return []
    import random, re
    rng = random.Random(seed + "-marriages")
    ceremonies = [
        "In a beautiful ceremony, branch '%s' was joined in holy matrimony to main.",
        "Vows were exchanged at the registry office as '%s' wed main.",
        "In a 2am elopement, '%s' ran away with main. No one was surprised.",
        "Before the CI congregation, '%s' renewed its vows with main.",
        "In a rushed chapel ceremony between rebases, '%s' married main.",
    ]
    announcements = []
    for m in merges[:5]:
        msg = m["headline"]
        match = re.search(r"from \S+/(\S+)", msg)
        if match:
            branch = match.group(1)
        else:
            match = re.search(r"Merge branch '([^']+)'", msg)
            if not match:
                continue
            branch = match.group(1)
        # Hotfix branches get the rushed chapel
        if "hotfix" in branch.lower():
            body = ceremonies[4] % branch
        else:
            body = rng.choice(ceremonies) % branch
        announcements.append({
            "headline": "%s weds main" % branch,
            "byline": m["byline"],
            "body": body,
            "url": m["url"],
        })
    return announcements


def letters_to_editor(commits):
    """Commit messages that sound like complaints, as letters."""
    if not commits:
        return []
    import re
    # Messages with frustration markers
    patterns = [
        (r"\bfix\b.*\bbug\b", "Dear Editor,"),
        (r"\bhate\b", "Dear Editor,"),
        (r"\bannoying\b", "Dear Editor,"),
        (r"\bbroken\b", "Dear Editor,"),
        (r"\bwhy\b.*\?", "Dear Editor,"),
    ]
    letters = []
    for c in commits:
        msg = c["headline"].lower()
        for pattern, _ in patterns:
            if re.search(pattern, msg):
                letters.append({
                    "headline": "Letter: %s" % c["headline"][:50],
                    "byline": c["byline"],
                    "body": '"%s" - %s' % (c["headline"], c["byline"]),
                    "url": c["url"],
                })
                break
        if len(letters) >= 3:
            break
    return letters


def missed_connections(issues_opened, issues_closed):
    """Opened-but-unclosed issues as missed connections."""
    # Issues that were opened but never closed (still open)
    # For simplicity: opened issues that don't appear in closed
    closed_numbers = {i.get("number") for i in issues_closed if i.get("number")}
    missed = []
    for issue in issues_opened[:5]:
        if issue.get("number") not in closed_numbers:
            missed.append({
                "headline": "Missed Connection: #%s" % issue.get("number"),
                "byline": issue["byline"],
                "body": "You: %s. Me: still waiting. Let's try again?" %
                        issue["headline"][:60],
                "url": issue["url"],
            })
    return missed


def classified_ads(seed="", md=""):
    """Vintage-style fake classified ads. Weekly campaigns.

    Seeded by calendar week, not edition date: the same 3 ads run
    all week, then rotate as a block. The merge-conflict ad is an
    anchor advertiser with high rebook probability. The week comes
    from the edition month-day on a fixed reference year, so
    historical editions show their own week's campaign; without md
    it falls back to the current week. The edition seed salts the
    draw so each paper gets its own campaign.
    """
    import random, datetime
    if md:
        month, day = int(md[:2]), int(md[3:5])
        year, week, _ = datetime.date(2024, month, day).isocalendar()
    else:
        today = datetime.date.today()
        year, week, _ = today.isocalendar()
    week_seed = "%d-W%02d" % (year, week)
    rng = random.Random("%s|%s" % (week_seed, seed))
    ads = [
        "WANTED: Meaningful commit messages. No 'fix stuff'. Reward offered.",
        "FOR SALE: One slightly used merge conflict. As-is. No returns.",
        "LOST: My patience during rebase. If found, please return.",
        "HELP WANTED: Someone to review my PR. Please. Anyone.",
        "FOR TRADE: My technical debt for your clean architecture.",
        "NOTICE: The 'it works on my machine' defense is no longer valid.",
        "WANTED: A bug that reproduces consistently. Generous reward.",
        "FOR SALE: Slightly used keyboard. Keys W, A, S, D worn out.",
    ]
    # Anchor advertiser: 80% chance the merge-conflict ad runs.
    anchor = ads[1]
    pool = [a for a in ads if a != anchor]
    selected = rng.sample(pool, 2)
    if rng.random() < 0.8:
        selected.append(anchor)
    else:
        # Draw from what is left so no ad runs twice.
        selected.append(rng.choice([a for a in pool
                                   if a not in selected]))
    rng.shuffle(selected)
    items = []
    for ad in selected[:3]:
        keyword = ad.split(":", 1)[0].strip().title()
        items.append({"headline": keyword, "byline": "",
                      "body": ad, "url": ""})
    return items


def build_docket(items, ticket_url_base=""):
    """Group items by ticket key into a courtroom-style docket.

    Each ticket becomes a case with its exhibits (commits, PRs, issues).
    Witty labels mark the busiest case, cold cases, and brief appearances.
    """
    cases = {}
    for item in items:
        text = " ".join([
            item.get("headline", ""),
            item.get("body", ""),
            item.get("branch", ""),
        ])
        for key in find_tickets(text):
            cases.setdefault(key, []).append(item)
    if not cases:
        return ""
    # Busiest case first, then by key.
    ranked = sorted(cases.items(),
                    key=lambda kv: (-len(kv[1]), kv[0]))
    busiest = ranked[0][0] if ranked and len(ranked[0][1]) > 2 else None
    parts = ["<section>", "<h2>The Docket</h2>"]
    for key, exhibits in ranked:
        kinds = {}
        years = set()
        for e in exhibits:
            kinds[e.get("kind", "item")] = \
                kinds.get(e.get("kind", "item"), 0) + 1
            if e.get("year"):
                years.add(e["year"])
        bits = []
        for kind in ("commit", "PR", "issue"):
            n = kinds.get(kind, 0)
            if n:
                bits.append("%d %s%s" % (n, kind, "" if n == 1 else "s"))
        # Witty notes.
        notes = []
        if key == busiest:
            notes.append("Most argued today.")
        if years and max(years) - min(years) >= 2:
            notes.append("Reopened after %d years. The file was gathering dust."
                         % (max(years) - min(years)))
        elif len(exhibits) == 1:
            notes.append("A brief appearance before the court.")
        # Verdict from PR state.
        if any(e.get("merged") for e in exhibits):
            notes.append("Case closed.")
        elif any(e.get("kind") == "PR" for e in exhibits):
            notes.append("Adjourned.")
        headline = "Case %s &mdash; %d exhibit%s (%s)." % (
            ticket_link(key, ticket_url_base),
            len(exhibits), "" if len(exhibits) == 1 else "s",
            ", ".join(bits))
        if notes:
            headline += " " + " ".join(notes)
        parts.append("<article>\n  <h3>%s</h3>\n</article>" % headline)
    parts.append("</section>")
    return "\n".join(parts)
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
        bits.append("%d issue%s closed" % (counts["closed"],
                                          "" if counts["closed"] == 1
                                          else "s"))
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
    """Per-weekday activity summaries across the years.

    Each pair is (singular, plural, items).
    """
    items = []
    for md, label in week_days:
        bits = []
        for singular, plural, lst in pairs:
            n = sum(1 for i in lst if i.get("day") == md)
            if n:
                bits.append("%d %s" % (n, singular if n == 1 else plural))
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


def anniversary_batches(items, current_year, span=""):
    """Group items by anniversary: 1, 5, 10, 15, 20, 25 years ago.

    For week mode: "5 Years Ago This Week" batches.
    Only significant anniversaries get their own section.
    """
    if not items:
        return ""
    # Milestone anniversaries
    milestones = {1, 5, 10, 15, 20, 25}
    by_anniversary = {}
    for item in items:
        years_ago = current_year - item.get("year", current_year)
        if years_ago in milestones:
            by_anniversary.setdefault(years_ago, []).append(item)
    if not by_anniversary:
        return ""
    parts = []
    for years_ago in sorted(by_anniversary.keys()):
        batch = by_anniversary[years_ago]
        label = "%d Year%s Ago" % (years_ago, "s" if years_ago != 1 else "")
        if span == "week":
            label += " This Week"
        parts.append(section(label, batch[:10]))  # Cap at 10 per anniversary
    return "\n".join(parts)


def gather_repos(repos, mds, no_comments, week_days=(), ticket_url="",
                 memory=None, edition_key=""):
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
    # Newspaper-style curation: many quip types, but only a few make
    # the edition. Seeded by date for consistency.
    seed = "%s-%s" % (mds[0] if mds else "", span)
    candidates = find_quips(data["commits"])
    candidates["revert"] = find_reverts(data["commits"])
    quips = curate_drama(candidates, limit=5, seed=seed, memory=memory,
                         current_key=edition_key)
    for q in quips:
        q["quip_type"] = "quip"
    drama = (quips + hottest_threads(data["opened"], data["closed"]))
    counts["drama"] = len(drama)
    merges = find_merges(data["commits"])
    counts["merges"] = len(merges)
    quote = find_quote(data["commits"], data["comments"])
    # Tag items for the docket.
    for item in data["commits"]:
        item["kind"] = "commit"
    for item in data["opened"] + data["closed"]:
        item["kind"] = "issue"
    for item in data["merged"]:
        item["kind"] = "PR"
        item["merged"] = True
    docket_items = (data["commits"] + data["opened"] + data["closed"]
                    + data["merged"])
    # Separate serious drama (reverts, hot threads) from quips.
    # Quips go in the Overheard sidebar, not as full articles.
    serious = [d for d in drama if d.get("quip_type") != "quip"]
    quip_items = [d for d in drama if d.get("quip_type") == "quip"]
    voices = find_new_voices(data["commits"])
    seed = "%s-%s" % (mds[0] if mds else "", span)
    # Charm sections
    weather_text = repo_weather(data["commits"], merges, data["opened"])
    marriages = marriage_announcements(merges, seed=seed)
    letters = letters_to_editor(data["commits"])
    missed = missed_connections(data["opened"], data["closed"])
    ads = classified_ads(seed=seed, md=mds[0] if mds else "")
    sections = [
        pullquote(quote),
        new_voices_box(voices, seed=seed),
        overheard_box(quip_items),
        section("Scandals & Corrections", serious),
        build_docket(docket_items, ticket_url),
        section("Merges", merges),
        section("Releases", data["releases"], lead=True),
        section("From the Commit Ledger", data["commits"], lead=True),
        section("Tags Cut", data["tags"], brief=True),
        section("Issues Opened", data["opened"], brief=True),
        section("Issues Closed", data["closed"], brief=True),
        section("Pull Requests Merged", data["merged"], lead=True),
        section("Voices From the Threads", data["comments"]),
        charm_box("Weather", [{"headline": "", "body": weather_text}],
                  style="weather"),
        charm_box("Marriages", marriages, style="marriage"),
        charm_box("Letters to the Editor", letters, style="letter"),
        charm_box("Missed Connections", missed, style="missed"),
        charm_box("Classifieds", ads, style="classified"),
    ]
    if week_days:
        pairs = [("commit", "commits", data["commits"]),
                 ("issue opened", "issues opened", data["opened"]),
                 ("issue closed", "issues closed", data["closed"]),
                 ("pull request merged", "pull requests merged",
                  data["merged"]),
                 ("release", "releases", data["releases"]),
                 ("tag", "tags", data["tags"]),
                 ("comment", "comments", data["comments"])]
        sections.insert(1, day_by_day(pairs, week_days, span))
        # Anniversary batches: group by milestone years ago
        import datetime
        current_year = datetime.date.today().year
        all_items = (data["commits"] + data["opened"] + data["closed"] +
                     data["merged"] + data["releases"])
        anniv = anniversary_batches(all_items, current_year, span="week")
        if anniv:
            sections.insert(2, anniv)
    body = "\n".join(sections)
    single = len(repos) == 1
    if memory is not None and edition_key:
        memory.record(
            edition_key,
            quips=[quip_text(q) for q in quips],
            ads=[a.get("headline", "") for a in ads],
            contributors=[c.get("login") or c.get("byline", "")
                          for c in data["commits"]],
            lead_headline=(quote or {}).get("text", ""),
        )
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


def gather_author(user, mds, week_days=(), ticket_url="", memory=None,
                  edition_key=""):
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
    # The edition draws from the private record when any included activity
    # comes from a private repository. Commit search hits carry the flag;
    # issue and comment repos are checked directly, cached per repo.
    has_private = any(i.get("private") for i in commits)
    if not has_private:
        seen_repos = set()
        for item in opened + prs_opened + merged + closed + comments:
            byline = item.get("byline", "")
            if " · " in byline:
                repo = byline.split(" · ", 1)[1].strip()
                if "/" in repo and repo not in seen_repos:
                    seen_repos.add(repo)
                    owner, name = repo.split("/", 1)
                    if repo_is_private(owner, name):
                        has_private = True
                        break
    counts = {"commits": len(commits), "opened": len(opened),
              "prs_opened": len(prs_opened), "merged": len(merged),
              "closed": len(closed), "comments": len(comments)}
    seed = "%s-%s" % (mds[0] if mds else "", span)
    candidates = find_quips(commits)
    candidates["revert"] = find_reverts(commits)
    quips = curate_drama(candidates, limit=5, seed=seed, memory=memory,
                         current_key=edition_key)
    for q in quips:
        q["quip_type"] = "quip"
    drama = (quips + hottest_threads(opened + prs_opened, closed))
    counts["drama"] = len(drama)
    merges = find_merges(commits)
    counts["merges"] = len(merges)
    quote = find_quote(commits, comments)
    for item in commits:
        item["kind"] = "commit"
    for item in opened + closed:
        item["kind"] = "issue"
    for item in prs_opened:
        item["kind"] = "PR"
    for item in merged:
        item["kind"] = "PR"
        item["merged"] = True
    docket_items = commits + opened + prs_opened + merged + closed
    serious = [d for d in drama if d.get("quip_type") != "quip"]
    quip_items = [d for d in drama if d.get("quip_type") == "quip"]
    sections = [
        pullquote(quote),
        overheard_box(quip_items),
        section("Scandals & Corrections", serious),
        build_docket(docket_items, ticket_url),
        section("Merges", merges),
        section("From the Commit Ledger", commits, lead=True),
        section("Issues Opened", opened, brief=True),
        section("Pull Requests Opened", prs_opened, brief=True),
        section("Pull Requests Merged", merged, lead=True),
        section("Issues Closed", closed, brief=True),
        section("Voices From the Threads", comments),
    ]
    if week_days:
        pairs = [("commit", "commits", commits),
                 ("issue opened", "issues opened", opened),
                 ("pull request opened", "pull requests opened", prs_opened),
                 ("pull request merged", "pull requests merged", merged),
                 ("issue closed", "issues closed", closed),
                 ("merge", "merges", merges),
                 ("comment", "comments", comments)]
        sections.insert(1, day_by_day(pairs, week_days, span))
    body = "\n".join(sections)
    if memory is not None and edition_key:
        memory.record(
            edition_key,
            quips=[quip_text(q) for q in quips],
            contributors=[c.get("login") or c.get("byline", "")
                          for c in commits],
            lead_headline=(quote or {}).get("text", ""),
        )
    return {
        "title": user,
        "label": user,
        "years": years,
        "counts": counts,
        "body": body,
        "record": ("Compiled from the private record" if has_private
                   else "Compiled from the public record"),
        "subject": {"author": user},
    }


def card_bits(counts):
    """Short human stat phrases for the SVG card, skipping zeros."""
    bits = []
    n = counts.get("commits", 0)
    if n:
        bits.append("%d commit%s" % (n, "" if n == 1 else "s"))
    n = counts.get("merged", 0)
    if n:
        bits.append("%d PR%s merged" % (n, "" if n == 1 else "s"))
    n = counts.get("prs_opened", 0)
    if n:
        bits.append("%d PR%s opened" % (n, "" if n == 1 else "s"))
    n = counts.get("closed", 0)
    if n:
        bits.append("%d issue%s closed" % (n, "" if n == 1 else "s"))
    n = counts.get("opened", 0)
    if n:
        bits.append("%d issue%s opened" % (n, "" if n == 1 else "s"))
    return bits


CARD_SIZES = {"sm": (300, 85), "md": (600, 170), "lg": (900, 255)}


def render_paper_card(label, date_label, link_label, counts, size):
    """Newspaper-style SVG card. Pure static SVG, no scripts."""
    width, height = CARD_SIZES[size]
    stats = " · ".join(card_bits(counts)) or "a quiet day in the archives"
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d"'
        ' viewBox="0 0 600 170" role="img">'
        '<rect width="600" height="170" fill="#f5f1e6" stroke="#8a8272"'
        ' stroke-width="2"/>'
        '<text x="30" y="38" font-family="Georgia, serif" font-size="15"'
        ' letter-spacing="4" fill="#6b655a">THE DAILY COMMIT</text>'
        '<text x="30" y="82" font-family="Georgia, serif" font-size="34"'
        ' font-weight="bold" fill="#1c1a16">%s</text>'
        '<text x="30" y="114" font-family="Georgia, serif" font-size="16"'
        ' fill="#1c1a16">%s</text>'
        '<text x="30" y="140" font-family="Georgia, serif" font-size="14"'
        ' fill="#6b655a">%s</text>'
        '<text x="30" y="160" font-family="Georgia, serif" font-size="11"'
        ' fill="#8a8272">%s</text>'
        "</svg>"
        % (width, height, esc(label), esc(date_label), esc(stats),
           esc(link_label)))


def render_badge_card(counts):
    """Shields-style flat badge SVG with the headline stat."""
    bits = card_bits(counts)
    left, right = "daily commit", bits[0] if bits else "quiet day"
    lw = int(len(left) * 6.5 + 12)
    rw = int(len(right) * 6.5 + 12)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="20"'
        ' role="img">'
        '<rect width="%d" height="20" fill="#555"/>'
        '<rect x="%d" width="%d" height="20" fill="#4c1"/>'
        '<text x="6" y="14" font-family="Verdana, sans-serif" font-size="11"'
        ' fill="#fff">%s</text>'
        '<text x="%d" y="14" font-family="Verdana, sans-serif" font-size="11"'
        ' fill="#fff">%s</text>'
        "</svg>"
        % (lw + rw, lw, lw, rw, esc(left), lw + 6, esc(right)))


def render_card(label, date_label, link_label, counts, size, style):
    if style == "badge":
        return render_badge_card(counts)
    return render_paper_card(label, date_label, link_label, counts, size)


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
                        help="Output path (default: print a summary; "
                             "with --card, print the SVG)")
    parser.add_argument("--card", action="store_true",
                        help="Emit a standalone SVG card instead of the "
                             "HTML page, for embedding in README files")
    parser.add_argument("--card-size", default="md",
                        choices=("sm", "md", "lg"),
                        help="Card size (default: md)")
    parser.add_argument("--card-style", default="paper",
                        choices=("paper", "badge"),
                        help="Card style: newspaper card or shields-style "
                             "badge (default: paper)")
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
    parser.add_argument("--ticket-url", default="",
                        help="Base URL for ticket keys (e.g. "
                             "https://tracker.example.com/browse): "
                             "keys like ABC-123 become links")
    parser.add_argument("--platform", default="",
                        choices=["", "github", "gitlab", "bitbucket",
                                 "gitea", "forgejo", "generic"],
                        help="Override platform auto-detection for the repo "
                             "host (for self-hosted or unusual forges)")
    args = parser.parse_args(argv)

    if args.author and args.repos:
        parser.error("--author cannot be combined with repositories")
    if not args.author and not args.repos:
        parser.error("give one or more owner/repo repositories, "
                     "or --author USER")
    if args.week_url and not args.author:
        parser.error("--week-url needs --author")
    if args.card and args.share:
        parser.error("--card cannot be combined with --share")

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
        elif first.year == last.year:
            md_long = "%s %d to %s %d, %d" % (
                first.strftime("%B"), first.day,
                last.strftime("%B"), last.day, last.year)
        else:
            md_long = "%s %d, %d to %s %d, %d" % (
                first.strftime("%B"), first.day, first.year,
                last.strftime("%B"), last.day, last.year)
    else:
        md = target.strftime("%m-%d")
        mds = [md]
        week_days = []
        md_long = target.strftime("%B %d")

    # Cross-edition memory: deprioritizes quips that ran recently.
    # The state file lives next to generate.py locally; in CI the
    # daily workflow restores it from the published site before the
    # build and republishes the updated file with the site.
    memory = EditionMemory()
    key_suffix = ":week" if args.week else ""
    if args.author:
        edition_key = "author:%s:%s%s" % (args.author, mds[0], key_suffix)
    else:
        edition_key = "repo:%s:%s%s" % (",".join(args.repos), mds[0],
                                        key_suffix)

    if args.author:
        edition = gather_author(args.author, mds, week_days,
                                ticket_url=args.ticket_url,
                                memory=memory, edition_key=edition_key)
    else:
        edition = gather_repos(args.repos, mds, args.no_comments,
                               week_days, ticket_url=args.ticket_url,
                               memory=memory, edition_key=edition_key)

    if args.card:
        link = ("github.com/%s" % args.author if args.author
                else "github.com/%s" % args.repos[0])
        svg = render_card(edition["label"], md_long, link,
                          edition["counts"], args.card_size,
                          args.card_style)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(svg)
        else:
            print(svg)
        return

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
            .replace("{{TITLE_LINK}}", _title_link(edition["title"]))
            .replace("{{KICKER}}", "A weekly chronicle of repository history"
                     if args.week else "A daily chronicle of repository history")
            .replace("{{DATELINE}}", esc(md_long))
            .replace("{{CLOCK_FALLBACK}}", dt.datetime.now().strftime("%I:%M %p"))
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
