#!/usr/bin/env bash
# bump-version.sh — update the version field in pyproject.toml.
# Called by cocogitto's pre_bump_hooks with the new version as $1.
# Usage: ./scripts/bump-version.sh 0.2.0
set -euo pipefail

version="${1:?usage: bump-version.sh <version>}"

# Strip a leading 'v' if present (cog tag_prefix is 'v' but pyproject wants bare semver).
version="${version#v}"

pyproject="pyproject.toml"

if [[ ! -f "$pyproject" ]]; then
	echo "error: $pyproject not found" >&2
	exit 1
fi

# Replace the first `version = "..."` line under [project].
# Using python for portable, safe in-place editing (no sed portability issues).
python3 - "$pyproject" "$version" <<'PY'
import sys
path, version = sys.argv[1], sys.argv[2]
lines = open(path, encoding="utf-8").readlines()
replaced = False
for i, line in enumerate(lines):
    if line.strip().startswith("version") and "=" in line and '"' in line:
        lines[i] = f'version = "{version}"\n'
        replaced = True
        break
if not replaced:
    print(f"error: no version field found in {path}", file=sys.stderr)
    sys.exit(1)
open(path, "w", encoding="utf-8").writelines(lines)
print(f"bumped {path} -> {version}")
PY
