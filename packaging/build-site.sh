#!/bin/sh
# Assemble the full GitHub Pages site for the-daily-commit:
#   /          -> redirects to today's paper for the viewer's local date
#   /daily/    -> the repo's own paper, yesterday/today/tomorrow (UTC)
#   /apt/      -> installable apt repository
# Usage: packaging/build-site.sh   (needs gh, dpkg-scanpackages, python3)
set -eu

SITE="site"
rm -rf "$SITE"
mkdir -p "$SITE/apt" "$SITE/daily"

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
cp packaging/daily-index.html "$SITE/daily/index.html"
cp packaging/root-index.html "$SITE/index.html"
echo "site built in $SITE/"
