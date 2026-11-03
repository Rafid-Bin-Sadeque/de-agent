#!/usr/bin/env bash
# Build the clean release branch. Never pushes.
#
# Usage: tools/release.sh <github-owner> [repo-name=de-agent] [--version X.Y.Z] [--base <ref>|none] [--skip-checks]
#
#   --version X.Y.Z  the version being released; must equal cli/pyproject.toml (default: read from it). Tag and branch use it.
#   --base <ref>     parent of the release commit. Default: origin/main when it exists, else none.
#   --base none      orphan release commit (the first release, or a deliberate restart of the public history).
#   --skip-checks    skip ruff/build/twine. ONLY for testing this script itself; a real release never uses it.
#
# 1. refuses on a dirty working tree
# 2. fills the OWNER placeholder (OWNER/de-agent, OWNER/groundtruth, git@github.com:OWNER/...) on a temp branch from main
# 3. checks: ruff, python -m build cli, twine check
# 4. release/v<version>: ONE commit of that tree, author "de-agent maintainers". Its parent is the base ref (so the public
#    history stays linear, one commit per release, and every push is a fast-forward) or nothing (orphan) for `--base none`
# 5. annotated-free lightweight tag v<version> on it (refused if the tag already exists in the public history)
# 6. leak greps over the release tree, over the new commit's diff, and over the WHOLE public history; any hit aborts
# 7. prints the exact push commands (fast-forward when there is a base)
set -euo pipefail

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }

POS=(); VERSION_ARG=""; BASE_ARG=""; SKIP_CHECKS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --version) [ $# -ge 2 ] || usage; VERSION_ARG="$2"; shift 2 ;;
    --version=*) VERSION_ARG="${1#--version=}"; shift ;;
    --base) [ $# -ge 2 ] || usage; BASE_ARG="$2"; shift 2 ;;
    --base=*) BASE_ARG="${1#--base=}"; shift ;;
    --skip-checks) SKIP_CHECKS=1; shift ;;
    -h|--help) usage ;;
    --*) echo "unknown option: $1" >&2; usage ;;
    *) POS+=("$1"); shift ;;
  esac
done
[ "${#POS[@]}" -ge 1 ] && [ "${#POS[@]}" -le 2 ] || usage
OWNER="${POS[0]}"
REPO="${POS[1]:-de-agent}"

cd "$(git rev-parse --show-toplevel)"
PY_VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' cli/pyproject.toml | head -1)"
VERSION="${VERSION_ARG:-$PY_VERSION}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.]+)?$ ]] || { echo "refusing: '$VERSION' is not a version like 0.2.0" >&2; exit 1; }
[ "$VERSION" = "$PY_VERSION" ] || { echo "refusing: --version $VERSION does not match cli/pyproject.toml ($PY_VERSION)" >&2; exit 1; }
BRANCH="release/v${VERSION}"
TAG="v${VERSION}"
START="$(git rev-parse --abbrev-ref HEAD)"
TMP_BRANCH="tmp-release-$$"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/dea-release.XXXXXX")"
VENV_BIN="${DEA_VENV_BIN:-cli/.venv/bin}"
py() { if [ -x "$VENV_BIN/python" ]; then "$VENV_BIN/python" "$@"; else python3 "$@"; fi; }
AUTHOR_NAME="de-agent maintainers"; AUTHOR_EMAIL="noreply@example.invalid"

[ -z "$(git status --porcelain)" ] || { echo "refusing: working tree is dirty (commit or stash first)" >&2; exit 1; }
git rev-parse --verify -q main >/dev/null || { echo "refusing: no local main branch" >&2; exit 1; }

# base: --base none | --base <ref> | origin/main when present
BASE=""
if [ "$BASE_ARG" = "none" ]; then
  BASE=""
elif [ -n "$BASE_ARG" ]; then
  BASE="$(git rev-parse --verify -q "${BASE_ARG}^{commit}")" || { echo "refusing: --base $BASE_ARG does not exist" >&2; exit 1; }
elif BASE="$(git rev-parse --verify -q refs/remotes/origin/main^{commit})"; then
  :
else
  BASE=""
fi

cleanup() {
  git switch -q "$START" 2>/dev/null || git switch -q main 2>/dev/null || true
  git branch -D "$TMP_BRANCH" >/dev/null 2>&1 || true
  rm -rf "$SCRATCH"
}
trap cleanup EXIT

if [ -n "$BASE" ]; then
  echo "== base: $(git rev-parse --short "$BASE") (the release commit goes on top of it: fast-forward)"
else
  echo "== base: none (orphan release commit)"
fi
echo "== 2. fill placeholders: owner=$OWNER repo=$REPO version=$VERSION"
git switch -q -c "$TMP_BRANCH" main
FILES="$(git ls-files README.md cli/pyproject.toml cli/README.md '.claude-plugin/*.json' 'docs/*' 'skills/*' 'agents/*' | tr '\n' ' ')"
# shellcheck disable=SC2086
[ -z "$FILES" ] || perl -pi -e "s{OWNER/groundtruth}{$OWNER/$REPO}g; s{OWNER/de-agent}{$OWNER/$REPO}g; s{github\.com/OWNER/}{github.com/$OWNER/}g; s{github\.com:OWNER/}{github.com:$OWNER/}g" $FILES
git add -A
git -c user.name="$AUTHOR_NAME" -c user.email="$AUTHOR_EMAIL" commit -q -m "fill placeholders" --allow-empty
LEFT="$(git grep -n -e 'github\.com/OWNER/' -e 'github\.com:OWNER/' "$TMP_BRANCH" -- . ':!tools/release.sh' ':!cli/tests' || true)"
if [ -n "$LEFT" ]; then echo "refusing: unfilled OWNER placeholders (add the file to FILES):" >&2; echo "$LEFT" | cut -d: -f2,3 >&2; exit 1; fi
TREE="$(git rev-parse "${TMP_BRANCH}^{tree}")"

if [ -n "$BASE" ] && [ "$(git rev-parse "${BASE}^{tree}")" = "$TREE" ]; then
  echo "refusing: the release tree is identical to the base; nothing to release" >&2; exit 1
fi
if git rev-parse --verify -q "refs/tags/$TAG" >/dev/null; then
  if [ -n "$BASE" ] && git merge-base --is-ancestor "refs/tags/$TAG" "$BASE" 2>/dev/null; then
    echo "refusing: $TAG is already part of the public history; bump the version" >&2; exit 1
  fi
fi

echo "== 3. checks"
if [ "$SKIP_CHECKS" = 1 ]; then
  echo "  WARNING: --skip-checks given: ruff/build/twine NOT run (only for testing the release script)"
else
  "$VENV_BIN/ruff" check . || ruff check .
  BUILD_ARGS=(--outdir "$SCRATCH/dist")
  if py -c 'import hatchling' 2>/dev/null; then BUILD_ARGS+=(--no-isolation); fi  # no network needed when hatchling is installed
  py -m build "${BUILD_ARGS[@]}" cli >"$SCRATCH/build.log" 2>&1 || { tail -20 "$SCRATCH/build.log" >&2; exit 1; }
  py -m twine check "$SCRATCH"/dist/*
fi

echo "== 4. release commit on $BRANCH"
git branch -D "$BRANCH" >/dev/null 2>&1 || true
PARENT_ARGS=()
[ -z "$BASE" ] || PARENT_ARGS=(-p "$BASE")
COMMIT="$(GIT_AUTHOR_NAME="$AUTHOR_NAME" GIT_AUTHOR_EMAIL="$AUTHOR_EMAIL" GIT_COMMITTER_NAME="$AUTHOR_NAME" \
  GIT_COMMITTER_EMAIL="$AUTHOR_EMAIL" git commit-tree "$TREE" ${PARENT_ARGS[@]+"${PARENT_ARGS[@]}"} -m "de-agent v${VERSION}")"
git branch "$BRANCH" "$COMMIT"

echo "== 5. tag $TAG"
git tag -f "$TAG" "$COMMIT" >/dev/null

echo "== 6. leak greps (counts and file:line only, never the matches): tree / new commit diff / whole public history"
# built from fragments so this file does not match its own greps
PATTERNS=("/Us""ers/" "~/Des""ktop" "/priv""ate/tmp" "claude""-504")
DENY=""
if [ -n "${DEA_DENYLIST_FILE:-}" ] && [ -f "$DEA_DENYLIST_FILE" ]; then
  # an entry that is part of the owner name is dropped: the filled-in repo URLs name the owner on purpose
  { grep -v '^[[:space:]]*$' "$DEA_DENYLIST_FILE" || true; } | while IFS= read -r e; do
    case "$(printf '%s' "$OWNER" | tr '[:upper:]' '[:lower:]')" in
      *"$(printf '%s' "$e" | tr '[:upper:]' '[:lower:]')"*) echo "  deny-list: one entry skipped (part of the owner name)" >&2 ;;
      *) printf '%s\n' "$e" ;;
    esac
  done >"$SCRATCH/deny.txt"
  [ -s "$SCRATCH/deny.txt" ] && DENY="$SCRATCH/deny.txt"
fi
# the new commit's own diff (against its parent, or the empty tree for an orphan)
DIFF_BASE="${BASE:-$(git hash-object -t tree /dev/null)}"
FAIL=0
for pat in "${PATTERNS[@]}"; do
  n="$({ git grep -I -n -F -e "$pat" "$BRANCH" || true; } | wc -l | tr -d ' ')"
  d="$({ git diff "$DIFF_BASE" "$COMMIT" | grep '^+' | grep -c -F -e "$pat"; } || true)"
  m="$({ git log -p "$BRANCH" | grep -c -F -e "$pat"; } || true)"
  echo "  '$pat': tree $n, new-commit diff $d, whole history $m"
  if [ "$n" != 0 ]; then { git grep -I -n -F -e "$pat" "$BRANCH" || true; } | cut -d: -f2,3 | sed 's/^/    /'; FAIL=1; fi
  [ "$d" != 0 ] && FAIL=1
  [ "$m" != 0 ] && FAIL=1
done
if [ -n "$DENY" ]; then
  n="$({ git grep -I -n -i -F -f "$DENY" "$BRANCH" || true; } | wc -l | tr -d ' ')"
  d="$({ git diff "$DIFF_BASE" "$COMMIT" | grep '^+' | grep -c -i -F -f "$DENY"; } || true)"
  m="$({ git log -p "$BRANCH" | grep -c -i -F -f "$DENY"; } || true)"
  echo "  deny-list (DEA_DENYLIST_FILE): tree $n, new-commit diff $d, whole history $m"
  if [ "$n" != 0 ]; then { git grep -I -n -i -F -f "$DENY" "$BRANCH" || true; } | cut -d: -f2,3 | sed 's/^/    /'; FAIL=1; fi
  [ "$d" != 0 ] && FAIL=1
  [ "$m" != 0 ] && FAIL=1
else
  echo "  deny-list: DEA_DENYLIST_FILE not set, skipped"
fi
if [ "$FAIL" != 0 ]; then
  echo "ABORT: leak greps found hits; removing $BRANCH and $TAG" >&2
  git branch -D "$BRANCH" >/dev/null 2>&1 || true; git tag -d "$TAG" >/dev/null 2>&1 || true
  exit 1
fi

echo "== 7. done. Release commit $COMMIT on $BRANCH (tag $TAG), $(git rev-list --count "$BRANCH") commit(s) of public history. Nothing was pushed. To publish:"
if [ -n "$BASE" ]; then
  echo "  git push origin ${BRANCH}:main --tags        # fast-forward on top of $(git rev-parse --short "$BASE")"
else
  echo "  git remote add origin git@github.com:${OWNER}/${REPO}.git && git push origin ${BRANCH}:main --tags"
fi
