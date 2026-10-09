#!/bin/sh
# Build a signed source package and upload it to a Launchpad PPA.
# Usage: packaging/ppa-upload.sh <ppa-user>/<ppa-name> [version-suffix]
# Example: packaging/ppa-upload.sh roguealg0/daily-commit
#
# Prerequisites (one time):
#   - a Launchpad account with the Ubuntu Code of Conduct signed
#   - a GPG key registered on Launchpad, available locally for signing
#   - the PPA created at https://launchpad.net/people/+me/+create-ppa
#   - dput and devscripts installed: sudo apt-get install dput devscripts
set -eu

PPA="$1"
SUFFIX="${2:-ppa1}"
VER=$(dpkg-parsechangelog -S Version | cut -d- -f1)
PPAVER="${VER}-1~${SUFFIX}"

# stamp the changelog; the trap restores it even if the build fails
cp debian/changelog /tmp/tdc-changelog.bak
trap 'cp /tmp/tdc-changelog.bak debian/changelog' EXIT
dch --newversion "$PPAVER" --distribution unstable --force-distribution \
    "PPA build."

dpkg-buildpackage -S -sa

CHANGES="../the-daily-commit_${PPAVER}_source.changes"
dput "ppa:$PPA" "$CHANGES"

echo "uploaded $PPAVER to ppa:$PPA"
