#!/bin/bash
# Built-in drift checks auto-run by the air pre-commit hook when the repo has
# no `.air-checks.sh`. Also callable directly from a custom `.air-checks.sh`:
#
#   "$AIR_PLUGIN_ROOT/hooks/builtin-checks.sh" || status=1
#
# Exit 0 silent on success. Exit 1 with [FAIL] lines on drift. Never exits >1
# (the hook reserves exit 2 for "block the tool call").

set -u
status=0
fail() { printf '  [FAIL] %s\n' "$1" >&2; status=1; }

# --- Manifest detection ---
# First match wins. Each branch sets MANIFEST + VERSION or continues.
MANIFEST=""
VERSION=""

extract_json_version() {
  python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('version',''))" "$1" 2>/dev/null
}

extract_toml_version() {
  # Handle both "1.2.3" and '1.2.3' quote styles. Reject workspace-inherit
  # (version = { workspace = true }) and any non-semver shape by validating
  # the extracted value looks like X.Y.Z before returning.
  local line v
  line=$(grep -E '^version[[:space:]]*=' "$1" 2>/dev/null | head -1)
  [ -z "$line" ] && return
  v=$(printf '%s' "$line" | sed -E "s/.*=[[:space:]]*[\"']([^\"']+)[\"'].*/\1/")
  # If sed didn't match (line printed verbatim) or value isn't semver-shaped, bail.
  case "$v" in
    [0-9]*.[0-9]*.[0-9]*) printf '%s' "$v" ;;
    *) return ;;
  esac
}

for candidate in package.json plugin.json pyproject.toml Cargo.toml composer.json; do
  [ -f "$candidate" ] || continue
  case "$candidate" in
    *.json) v=$(extract_json_version "$candidate") ;;
    *.toml) v=$(extract_toml_version "$candidate") ;;
  esac
  if [ -n "${v:-}" ]; then
    MANIFEST="$candidate"
    VERSION="$v"
    break
  fi
done

# Plugin-style layout (air itself, and plugins using this convention):
if [ -z "$VERSION" ]; then
  for candidate in plugins/*/.claude-plugin/plugin.json; do
    [ -f "$candidate" ] || continue
    v=$(extract_json_version "$candidate")
    if [ -n "${v:-}" ]; then
      MANIFEST="$candidate"
      VERSION="$v"
      break
    fi
  done
fi

# No manifest found → nothing to check.
[ -z "$VERSION" ] && exit 0

# Escape dots for regex (1.2.3 → 1\.2\.3)
VERSION_RE="${VERSION//./\\.}"

# --- Check 1: Shields.io version badge in README.md ---
if [ -f README.md ]; then
  BAD_BADGE=$(grep -oE "shields\\.io/badge/version-[0-9]+\\.[0-9]+\\.[0-9]+-" README.md 2>/dev/null \
    | grep -v "version-${VERSION_RE}-" | head -1)
  if [ -n "$BAD_BADGE" ]; then
    fail "README.md shields.io version badge is '$BAD_BADGE' but $MANIFEST version is $VERSION"
  fi
fi

# Enumerate candidate doc files via find so nested docs/ trees are covered on
# macOS bash 3.2 (which lacks `shopt -s globstar`).
DOC_FILES=$(find CLAUDE.md README.md docs -type f -name '*.md' 2>/dev/null)

# --- Check 2: "currently X.Y.Z" lines in common doc files ---
for f in $DOC_FILES; do
  BAD=$(grep -En "currently [0-9]+\\.[0-9]+\\.[0-9]+" "$f" 2>/dev/null \
    | grep -v "currently $VERSION_RE" | head -1)
  if [ -n "$BAD" ]; then
    fail "$f has 'currently <version>' line that doesn't match $MANIFEST version $VERSION: $BAD"
  fi
done

# --- Check 3: '**Version:** X.Y.Z' markdown headers ---
for f in $DOC_FILES; do
  BAD=$(grep -En "^\\*\\*Version:\\*\\* [0-9]+\\.[0-9]+\\.[0-9]+" "$f" 2>/dev/null \
    | grep -v "Version:\\*\\* $VERSION_RE" | head -1)
  if [ -n "$BAD" ]; then
    fail "$f has '**Version:**' header that doesn't match $MANIFEST version $VERSION: $BAD"
  fi
done

exit $status
