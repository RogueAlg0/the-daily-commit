# The Daily Commit

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

## Quick start

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

## Options

| Flag            | Effect                                              |
|-----------------|-----------------------------------------------------|
| `--date MM-DD`  | Cover a different month-day (default: today, UTC)   |
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
  month-day.
- Comments: scans recent issue comments and keeps up to five written on the
  month-day.

`template.html` holds the newspaper layout and CSS. `generate.py` fills in
the title, dateline, lede paragraph, and article sections. All repository
content is HTML-escaped before rendering. Each section shows at most 25
articles; longer sections note how many more wait in the archives.

The front page carries a gossip column, "Scandals & Corrections": revert
commits (a revert of a revert is flagged as such) and the day's
most-commented issue threads.

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
