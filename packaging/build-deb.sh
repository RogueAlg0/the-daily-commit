#!/bin/sh
# Build the-daily-commit_<ver>_all.deb from the repo root.
# Usage: packaging/build-deb.sh <version>   (version without leading v)
set -eu

VER="$1"
ROOT="debroot"

rm -rf "$ROOT"
mkdir -p "$ROOT/DEBIAN" "$ROOT/usr/bin" "$ROOT/usr/share/the-daily-commit"

cat > "$ROOT/DEBIAN/control" <<EOF
Package: the-daily-commit
Version: $VER
Architecture: all
Maintainer: RogueAlg0
Depends: python3
Description: Vintage-newspaper "on this day" pages for GitHub repositories
 Generate a vintage-newspaper "on this day in history" page for any
 GitHub repository, on demand. No dependencies beyond Python 3.
EOF

cp generate.py template.html "$ROOT/usr/share/the-daily-commit/"

cat > "$ROOT/usr/bin/the-daily-commit" <<'EOF'
#!/bin/sh
exec python3 /usr/share/the-daily-commit/generate.py "$@"
EOF
chmod 755 "$ROOT/usr/bin/the-daily-commit"
chmod 755 "$ROOT/DEBIAN"
chmod 644 "$ROOT/DEBIAN/control"
# Normalize modes: the .deb must install world-readable files no matter
# the build machine's umask.
find "$ROOT" -type d -exec chmod 755 {} +
find "$ROOT" -type f -exec chmod 644 {} +
chmod 755 "$ROOT/usr/bin/the-daily-commit"

dpkg-deb --build "$ROOT" "the-daily-commit_${VER}_all.deb"
echo "built the-daily-commit_${VER}_all.deb"
