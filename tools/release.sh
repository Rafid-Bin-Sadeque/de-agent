#!/usr/bin/env bash
# Build the clean release branch. Never pushes. Usage: tools/release.sh <github-owner> [repo-name=de-agent]
#
# 1. refuses on a dirty working tree
# 2. on a temp branch from main: fills the OWNER placeholder (OWNER/de-agent, OWNER/groundtruth, git@github.com:OWNER/...)
# 3. checks: ruff, python -m build cli, twine check
# 4. orphan branch release/v<version>, ONE commit of that tree, author "de-agent maintainers"
# 5. lightweight tag v<version> on it
# 6. leak greps over the release tree and `git log -p` (counts and file:line only, never the matches); any hit aborts
# 7. prints the exact push commands
set -euo pipefail

OWNER="${1:?usage: tools/release.sh <github-owner> [repo-name=de-agent]}"
REPO="${2:-de-agent}"
cd "$(git rev-parse --show-toplevel)"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' cli/pyproject.toml | head -1)"
BRANCH="release/v${VERSION}"
TAG="v${VERSION}"
START="$(git rev-parse --abbrev-ref HEAD)"
TMP_BRANCH="tmp-release-$$"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/dea-release.XXXXXX")"
VENV_BIN="cli/.venv/bin"
py() { if [ -x "$VENV_BIN/python" ]; then "$VENV_BIN/python" "$@"; else python3 "$@"; fi; }

[ -z "$(git status --porcelain)" ] || { echo "refusing: working tree is dirty (commit or stash first)" >&2; exit 1; }
git rev-parse --verify -q main >/dev/null || { echo "refusing: no local main branch" >&2; exit 1; }

cleanup() {
  git switch -q "$START" 2>/dev/null || git switch -q main 2>/dev/null || true
  git branch -D "$TMP_BRANCH" >/dev/null 2>&1 || true
  rm -rf "$SCRATCH"
}
trap cleanup EXIT

echo "== 2. fill placeholders: owner=$OWNER repo=$REPO"
git switch -q -c "$TMP_BRANCH" main
FILES="$(git ls-files README.md cli/pyproject.toml cli/README.md '.claude-plugin/*.json' 'docs/*' | tr '\n' ' ')"
# shellcheck disable=SC2086
perl -pi -e "s{OWNER/groundtruth}{$OWNER/$REPO}g; s{OWNER/de-agent}{$OWNER/$REPO}g; s{github\.com/OWNER/}{github.com/$OWNER/}g; s{github\.com:OWNER/}{github.com:$OWNER/}g" $FILES
git add -A
git -c user.name="de-agent maintainers" -c user.email="noreply@example.invalid" commit -q -m "fill placeholders" --allow-empty

echo "== 3. checks"
"$VENV_BIN/ruff" check . || ruff check .
BUILD_ARGS=(--outdir "$SCRATCH/dist")
if py -c 'import hatchling' 2>/dev/null; then BUILD_ARGS+=(--no-isolation); fi  # no network needed when hatchling is installed
py -m build "${BUILD_ARGS[@]}" cli >"$SCRATCH/build.log" 2>&1 || { tail -20 "$SCRATCH/build.log" >&2; exit 1; }
py -m twine check "$SCRATCH"/dist/*

echo "== 4. orphan branch $BRANCH"
git branch -D "$BRANCH" >/dev/null 2>&1 || true
git checkout -q --orphan "$BRANCH"
git add -A
GIT_AUTHOR_NAME="de-agent maintainers" GIT_AUTHOR_EMAIL="noreply@example.invalid" \
GIT_COMMITTER_NAME="de-agent maintainers" GIT_COMMITTER_EMAIL="noreply@example.invalid" \
  git commit -q -m "de-agent v${VERSION}"
COMMIT="$(git rev-parse HEAD)"

echo "== 5. tag $TAG"
git tag -f "$TAG" "$COMMIT" >/dev/null

echo "== 6. leak greps (counts, file:line only)"
# built from fragments so this file does not match its own greps
PATTERNS=("/Us""ers/" "~/Des""ktop" "/priv""ate/tmp" "claude""-504")
DENY=""
if [ -n "${DEA_DENYLIST_FILE:-}" ] && [ -f "$DEA_DENYLIST_FILE" ]; then
  grep -v '^[[:space:]]*$' "$DEA_DENYLIST_FILE" >"$SCRATCH/deny.txt" || true
  [ -s "$SCRATCH/deny.txt" ] && DENY="$SCRATCH/deny.txt"
fi
FAIL=0
for pat in "${PATTERNS[@]}"; do
  n="$({ git grep -I -n -F -e "$pat" "$BRANCH" || true; } | wc -l | tr -d ' ')"
  m="$({ git log -p "$BRANCH" | grep -c -F -e "$pat"; } || true)"
  echo "  '$pat': tree $n, log -p $m"
  if [ "$n" != 0 ]; then { git grep -I -n -F -e "$pat" "$BRANCH" || true; } | cut -d: -f2,3 | sed 's/^/    /'; FAIL=1; fi
  [ "$m" != 0 ] && FAIL=1
done
if [ -n "$DENY" ]; then
  n="$({ git grep -I -n -i -F -f "$DENY" "$BRANCH" || true; } | wc -l | tr -d ' ')"
  m="$({ git log -p "$BRANCH" | grep -c -i -F -f "$DENY"; } || true)"
  echo "  deny-list (DEA_DENYLIST_FILE): tree $n, log -p $m"
  if [ "$n" != 0 ]; then { git grep -I -n -i -F -f "$DENY" "$BRANCH" || true; } | cut -d: -f2,3 | sed 's/^/    /'; FAIL=1; fi
  [ "$m" != 0 ] && FAIL=1
else
  echo "  deny-list: DEA_DENYLIST_FILE not set, skipped"
fi
if [ "$FAIL" != 0 ]; then
  echo "ABORT: leak greps found hits; removing $BRANCH and $TAG" >&2
  git switch -q "$TMP_BRANCH"; git branch -D "$BRANCH" >/dev/null 2>&1 || true; git tag -d "$TAG" >/dev/null 2>&1 || true
  exit 1
fi

echo "== 7. done. Release commit $COMMIT on $BRANCH (tag $TAG). Nothing was pushed. To publish:"
echo "  git remote add origin git@github.com:${OWNER}/${REPO}.git && git push origin ${BRANCH}:main --tags"
