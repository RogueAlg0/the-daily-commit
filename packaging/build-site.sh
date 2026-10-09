#!/bin/sh
# Assemble the full GitHub Pages site for the-daily-commit:
#   /          -> redirects to today's paper for the viewer's local date
#   /daily/    -> the repo's own paper, yesterday/today/tomorrow (UTC)
#   /apt/      -> installable apt repository
# Usage: packaging/build-site.sh   (needs gh, dpkg-scanpackages, python3)
set -eu

SITE="site"
rm -rf "$SITE"
mkdir -p "$SITE/apt" "$SITE/daily" "$SITE/author"

# apt repository from every release .deb
for tag in $(gh release list --limit 100 --json tagName -q '.[].tagName'); do
  gh release download "$tag" -p 'the-daily-commit_*_all.deb' \
    -D "$SITE/apt/" >/dev/null 2>&1 || true
done
if ls "$SITE/apt"/*.deb >/dev/null 2>&1; then
  cd "$SITE/apt"
  dpkg-scanpackages --multiversion . /dev/null > Packages
  gzip -9c Packages > Packages.gz
  cd ../..
fi
cp packaging/apt-index.html "$SITE/apt/index.html"

# the repo's own paper for the three UTC dates covering every timezone
DATES=""
for offset in -1 0 1; do
  MD=$(date -u -d "$offset day" +%m-%d)
  DATES="$DATES \"$MD\","
  python3 generate.py RogueAlg0/the-daily-commit --date "$MD" \
    --out "$SITE/daily/$MD.html" >/dev/null
done
printf '{"dates": [%s]}\n' "$(echo "$DATES" | sed 's/,$//')" \
  > "$SITE/daily/latest.json"
# shields.io endpoint badge: live on every view, no badge CI needed
TODAY_HUMAN=$(date -u +"%-d %b")
printf '{"schemaVersion": 1, "label": "today'"'"'s paper", "message": "%s", "color": "blue"}\n' \
  "$TODAY_HUMAN" > "$SITE/daily/badge.json"
cp packaging/daily-index.html "$SITE/daily/index.html"
cp packaging/root-index.html "$SITE/index.html"

# the author's own paper, same three dates, plus a profile stats card
ADATES=""
for offset in -1 0 1; do
  MD=$(date -u -d "$offset day" +%m-%d)
  ADATES="$ADATES \"$MD\","
  if [ "$offset" -eq 0 ]; then
    python3 generate.py --author RogueAlg0 --date "$MD" --card \
      --out "$SITE/author/card.svg" >/dev/null
    python3 generate.py --author RogueAlg0 --date "$MD" \
      --out "$SITE/author/$MD.html" >/dev/null
  else
    python3 generate.py --author RogueAlg0 --date "$MD" \
      --out "$SITE/author/$MD.html" >/dev/null
  fi
done
printf '{"dates": [%s]}\n' "$(echo "$ADATES" | sed 's/,$//')" \
  > "$SITE/author/latest.json"
cp packaging/author-index.html "$SITE/author/index.html"
echo "site built in $SITE/"
