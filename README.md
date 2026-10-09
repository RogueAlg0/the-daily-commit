# The Daily Commit

[![Install with apt](https://img.shields.io/badge/apt-install-blue?logo=debian)](https://roguealg0.github.io/the-daily-commit/)
[![Latest release](https://img.shields.io/github/v/release/RogueAlg0/the-daily-commit)](https://github.com/RogueAlg0/the-daily-commit/releases)
[![CI](https://github.com/RogueAlg0/the-daily-commit/actions/workflows/check.yml/badge.svg)](https://github.com/RogueAlg0/the-daily-commit/actions/workflows/check.yml)

Generate a vintage-newspaper "on this day in history" page for any GitHub
repository, on demand.

The generator collects everything that happened on one month-day across
every year of a repository's history: commits, issues opened and closed,
pull requests merged, releases published, and notable comments. It renders
the result as one self-contained HTML file styled like a vintage
newspaper. The page uses no external assets, so the output file stays small
and works offline.

Run it locally whenever you want an edition. Nothing leaves your machine
unless you choose to share the file.

## Install

### apt (Debian/Ubuntu)

```sh
echo "deb [trusted=yes] https://roguealg0.github.io/the-daily-commit/ ./" \
  | sudo tee /etc/apt/sources.list.d/the-daily-commit.list
sudo apt update && sudo apt install the-daily-commit
```

### From source

Python 3.10 or later is required. No dependencies are required.

```sh
python3 generate.py owner/repo --out today.html
```

Open `today.html` in a browser to read the edition. For convenience, add an
alias in your shell profile:

```sh
alias the-daily-commit='python3 /path/to/the-daily-commit/generate.py'
```

Then each edition is one command:

```sh
the-daily-commit owner/repo --out today.html
```

## Modes

### Repository mode (default)

Pass one or more repositories. Everything that happened on the month-day
across every year of each repository's history lands in one paper; with
several repositories, each article's byline names its home repository.

```sh
the-daily-commit torvalds/linux --out today.html
the-daily-commit myorg/web myorg/api --out today.html
```

### User mode

Pass `--author` to cover one user's whole footprint on the month-day:
their commits, issues and pull requests opened, pull requests merged,
issues closed, and their own comments, across every repository they
touched. Handy as a personal "on this day" page or a living portfolio.

```sh
the-daily-commit --author RogueAlg0 --out today.html
```

User mode uses the GitHub search APIs, which have stricter rate limits
than the REST API, so it pauses a few seconds between searches. Setting
a token (see below) raises the limits. Releases and tags have no global
search, so they appear in repository mode only.

A "Merges" section flags the user's merge commits: cross-author merges
(the user merging another author's branch) are called out, as are
direct merges into main or master. Pull request headlines show their
`head → base` branches. Branch creation and deletion dates are not
available: GitHub keeps no historical branch record beyond 90 days of
events.

## Options

| Flag            | Effect                                              |
|-----------------|-----------------------------------------------------|
| `--date MM-DD`  | Cover a different month-day (default: today, local time) |
| `--out FILE`    | Write the HTML page to FILE                         |
| `--no-comments` | Skip the comments section                           |
| `--share`       | Publish the edition to here.now anonymously and print the shareable link (24-hour expiry, no login) |

## Private repositories

If you are logged in with the GitHub CLI (`gh auth login`), private
repositories you can access just work. No token setup is needed.

Otherwise, set `THE_DAILY_COMMIT_TOKEN` (or `GITHUB_TOKEN`) to a personal
access token to raise the API rate limit and to cover private
repositories. Without any credential, the script uses the lower
unauthenticated rate limit and can only read public repositories.

```sh
THE_DAILY_COMMIT_TOKEN=ghp_xxx python3 generate.py myorg/private-repo \
  --out today.html
```

## How it works

`generate.py` queries the GitHub REST API and filters by month-day on the
client side:

- Commits: one request per calendar year of the repository's life, bounded
  to the requested month-day.
- Issues: pages through issues sorted by creation date, then keeps the ones
  opened or closed on the month-day. Pull requests are excluded here.
- Pull requests: pages through closed pull requests and keeps the ones with
  a merge date on the month-day.
- Releases: pages through releases and keeps the ones published on the
  month-day. They render in a starred section near the front.
- Tags: pages through tags and resolves each tag's commit date (one call
  per tag, at most 25, most recent first). Tags that already have a GitHub
  release are skipped. Best-effort for repositories with many tags.
- Comments: scans recent issue comments and keeps up to five written on the
  month-day.

User mode (`--author`) instead uses the commit and issue search APIs:
`author:` plus `committer-date:` for commits, `author:` plus `created:`
for opened issues and PRs, `author:` plus `updated:` filtered by close
date for closed issues, one pulls-API read per candidate for merged PRs
(the issue search carries no merge date), and `commenter:` plus
`updated:` for the user's own comments. Merge commits are detected from
their messages ("Merge pull request #N from ...", "Merge branch 'x' into
main") and reported in a dedicated section, with cross-author merges
flagged. Search calls are paced a few seconds apart for the search API's
stricter rate limits.

`template.html` holds the newspaper layout and CSS. `generate.py` fills in
the title, dateline, lede paragraph, and article sections. All repository
content is HTML-escaped before rendering. Each section shows the first
five articles; the rest hide behind a "show all" expander, so long days
stay scannable without forced scrolling.

The front page carries a gossip column, "Scandals & Corrections": revert
commits (a revert of a revert is flagged as such) and the day's
most-commented issue threads. A pull-quote box features the day's most
quotable commit message or comment, attributed to its author, when one
clears the funniness bar.

## Sharing an edition

Pass `--share` to generate and publish in one step:

```sh
the-daily-commit owner/repo --share
```

This publishes the page to here.now as an anonymous site and prints the
shareable link. Anonymous publishing needs no account and no login. Links
expire after 24 hours. The link is unlisted, but anyone with the URL can
open it, so only share editions you are comfortable making visible.

The file never leaves your machine until you share it.

### Longer-lived links

For a link that lasts 30 days instead of 24 hours, upload the finished
HTML file to htmldoc.space (free; one-time GitHub login):

```sh
npx -y htmldoc-cli login  # once
npx -y htmldoc-cli today.html

## Project layout

- `generate.py`: the generator (standard library only).
- `template.html`: the vintage-newspaper HTML and CSS template.
- `LICENSE`: MIT.

## License

MIT. See `LICENSE`.
