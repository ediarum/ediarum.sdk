#!/usr/bin/env bash
# Publish a release to the public GitHub mirror as one snapshot commit.
#
#   scripts/publish.sh 0.2.0            publish the internal release v0.2.0 (asks first)
#   scripts/publish.sh --yes 0.2.0      same, without asking (scripts/release.sh)
#   scripts/publish.sh --check 0.2.0    only check that HEAD could be published as
#                                       v0.2.0; changes nothing (scripts/release.sh)
#
# The public history is a chain of release snapshots (Release 0.1.1 → Release 0.2.0
# → …), not the internal history: each snapshot is a new commit on top of the public
# `main` whose files are exactly those of the internal tag vX.Y.Z, minus EXCLUDE.
# Nothing comes back: no local branch or tag ever points at a public commit (the
# public tag vX.Y.Z is pushed straight from a tag object, since the local vX.Y.Z is
# the internal one). Commit and tag carry PUBLIC_IDENT, not the committer's own.
#
# Before pushing, the snapshot is searched for internal hosts and secret-looking
# strings (FORBIDDEN, minus ALLOWED). The GitHub release (and Zenodo) is made by
# hand from the tag.
set -euo pipefail

REMOTE=github
PUBLIC_URL=git@github.com:ediarum/ediarum.sdk.git
PUBLIC_BRANCH=main
PUBLIC_NAME=TELOTA
PUBLIC_EMAIL=telota@bbaw.de

# Paths left out of the public snapshot.
EXCLUDE=(
  .gitlab-ci.yml
)

# Extended regexes that must not appear anywhere in the snapshot.
FORBIDDEN=(
  'gitup\.uni-potsdam\.de'
  'pages\.uni-potsdam\.de'
  'bbawgitlab'
  'cluster-preussen'
  'telotawebdev'
  'x_[a-z]+_[0-9]+'
  'bbaw\.de'
  'php-prod'
  'telota-intermediate-proxy'
  '/home/[a-z]'
  '/opt/projects'
  'glpat-[A-Za-z0-9_-]{20,}'
  'gh[pousr]_[A-Za-z0-9]{20,}'
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
)

# Hits that are fine, as `file:regex` matched against the reported line.
ALLOWED=(
  'README.md:www\.bbaw\.de/telota|telota@bbaw\.de|ediarum@bbaw\.de'
  'pyproject.toml:telota@bbaw\.de'
  'scripts/publish.sh:.*'
)

die() { echo "publish: $*" >&2; exit 1; }

mode=ask
case "${1:-}" in
  --yes) mode=yes; shift ;;
  --check) mode=check; shift ;;
esac
version="${1:-}"
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "usage: $0 [--yes|--check] X.Y.Z"
tag="v$version"

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

git remote get-url "$REMOTE" >/dev/null 2>&1 \
  || die "no remote '$REMOTE' (git remote add $REMOTE $PUBLIC_URL)"

# --check looks at HEAD, the commit about to be released; otherwise publish exactly
# what the internal tag points at.
if [[ $mode == check ]]; then
  source=HEAD
else
  source="refs/tags/$tag"
  git rev-parse -q --verify "$source^{commit}" >/dev/null \
    || die "no internal tag $tag (release it first: scripts/release.sh)"
fi

# --no-tags: the public tags would clash with the internal ones of the same name
git fetch -q --no-tags "$REMOTE"
if git ls-remote --exit-code --tags "$REMOTE" "refs/tags/$tag" >/dev/null 2>&1; then
  die "$REMOTE already has the tag $tag"
fi
public="refs/remotes/$REMOTE/$PUBLIC_BRANCH"
if git rev-parse -q --verify "$public" >/dev/null; then
  parent=(-p "$public")
else
  public=$(git hash-object -t tree /dev/null)   # the empty tree: first publish
  parent=()
fi

# --- the snapshot tree, built in a throwaway index (the working tree is untouched)
index=$(mktemp)
trap 'rm -f "$index"' EXIT
export GIT_INDEX_FILE=$index
git read-tree "$source^{tree}"
for path in "${EXCLUDE[@]}"; do
  git rm -r -q --cached --ignore-unmatch -- "$path"
done
tree=$(git write-tree)
unset GIT_INDEX_FILE

# --- anything internal in it?
problems=0
for pattern in "${FORBIDDEN[@]}"; do
  while IFS= read -r hit; do
    [[ -z $hit ]] && continue
    line=${hit#"$tree":}   # file:lineno:text
    file=${line%%:*}
    text=${line#*:*:}
    allowed=0
    for rule in "${ALLOWED[@]}"; do
      if [[ $file == "${rule%%:*}" ]] && grep -qE -- "${rule#*:}" <<<"$text"; then
        allowed=1
        break
      fi
    done
    if ((allowed == 0)); then
      echo "  forbidden ($pattern): $line" >&2
      problems=$((problems + 1))
    fi
  done < <(git grep -I -n -E -e "$pattern" "$tree" || true)
done
((problems == 0)) || die "$problems forbidden match(es) in the snapshot; fix them on main first"

if [[ -z $(git diff --name-only "$public" "$tree") ]]; then
  [[ $mode == check ]] || die "the snapshot is identical to $REMOTE/$PUBLIC_BRANCH; nothing to publish"
fi

if [[ $mode != check ]]; then
  cff=$(git show "$tree:CITATION.cff" 2>/dev/null | sed -n 's/^version: *//p' | tr -d '"' || true)
  [[ -z $cff || $cff == "$version" ]] || echo "warning: CITATION.cff says version $cff, not $version"
fi

# --- show (and ask)
if ((${#parent[@]})); then
  base="$REMOTE/$PUBLIC_BRANCH ($(git rev-parse --short "$public"))"
else
  base="an empty $REMOTE (first publish)"
fi
echo "GitHub: $source ($(git rev-parse --short "$source")) as $tag on top of $base, without ${EXCLUDE[*]}"
git diff --stat=100 "$public" "$tree" | tail -n 15
[[ $mode == check ]] && exit 0
if [[ $mode == ask ]]; then
  read -r -p "Push to $REMOTE/$PUBLIC_BRANCH and tag $tag? [y/N] " answer
  [[ $answer == [yY] ]] || { echo "Nothing pushed."; exit 0; }
fi

# --- commit and tag object (no local refs), then push both
export GIT_AUTHOR_NAME=$PUBLIC_NAME GIT_AUTHOR_EMAIL=$PUBLIC_EMAIL
export GIT_COMMITTER_NAME=$PUBLIC_NAME GIT_COMMITTER_EMAIL=$PUBLIC_EMAIL
snapshot=$(git commit-tree "$tree" "${parent[@]}" -m "Release $version")
tagobj=$(printf 'object %s\ntype commit\ntag %s\ntagger %s <%s> %s\n\nRelease %s\n' \
  "$snapshot" "$tag" "$PUBLIC_NAME" "$PUBLIC_EMAIL" "$(date +'%s %z')" "$version" | git mktag)
git push -q "$REMOTE" "$snapshot:refs/heads/$PUBLIC_BRANCH" "$tagobj:refs/tags/$tag" \
  || die "push failed; nothing was published"

echo "published $tag to $REMOTE ($(git rev-parse --short "$snapshot")); create the GitHub release from the tag"
