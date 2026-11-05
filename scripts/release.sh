#!/usr/bin/env bash
# Release: bump the version, commit, tag vX.Y.Z (annotated) and push. The tag
# pipeline does the rest (CONTRIBUTING.md / README.md, "Release"). Then the release is
# published to the public GitHub mirror as one snapshot commit, tagged vX.Y.Z
# there too (scripts/publish.sh); NO_GITHUB=1 skips that.
#
#   scripts/release.sh patch    0.2.1 → 0.2.2
#   scripts/release.sh minor    0.2.1 → 0.3.0
#   scripts/release.sh major    0.2.1 → 1.0.0
#
# Refuses unless on main, with a clean tree, level with origin/main, and the
# tag doesn't exist yet, and — before anything changes — unless the snapshot
# for GitHub passes publish.sh's checks. Asks once before committing and pushing.
set -euo pipefail

bump="${1:-}"
case "$bump" in
  patch|minor|major) ;;
  *) echo "usage: $0 patch|minor|major" >&2; exit 2 ;;
esac

die() { echo "release: $*" >&2; exit 1; }

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[ "$(git symbolic-ref --short HEAD)" = main ] || die "not on main"
[ -z "$(git status --porcelain)" ] || die "uncommitted changes"
git fetch -q origin main --tags
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || die "main differs from origin/main (pull or push first)"

current="$(uv version --short)"
next="$(uv version --bump "$bump" --dry-run --short)"
tag="v$next"
git rev-parse -q --verify "refs/tags/$tag" >/dev/null && die "tag $tag exists already"

echo "$(basename "$PWD"): $current → $next, tag $tag"
where=" to GitLab"
if [ -z "${NO_GITHUB:-}" ]; then
  scripts/publish.sh --check "$next" || die "not publishable to GitHub (NO_GITHUB=1 skips GitHub)"
  where=" to GitLab, then GitHub"
fi
echo "Is the pipeline for main green?"
read -r -p "Commit, tag and push$where? [y/N] " answer
[ "$answer" = y ] || [ "$answer" = Y ] || die "aborted, nothing changed"

uv version --quiet --no-sync --bump "$bump"   # pyproject.toml and uv.lock; installs nothing
sed -i "s/^Version: .*/Version: $next/" README.md
sed -i -e "s/^version: .*/version: $next/" -e "s/^date-released: .*/date-released: \"$(date +%F)\"/" CITATION.cff
git add pyproject.toml uv.lock README.md CITATION.cff
git commit -q -m "release $tag"
git tag -a "$tag" -m "$tag"
git push -q --follow-tags origin main

echo "pushed $tag — the tag pipeline builds and publishes it"

[ -z "${NO_GITHUB:-}" ] || exit 0
scripts/publish.sh --yes "$next" \
  || die "$tag is released on GitLab, but not on GitHub; retry with: scripts/publish.sh $next"
