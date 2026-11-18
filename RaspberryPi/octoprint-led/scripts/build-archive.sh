#!/usr/bin/env bash
# Build a zip that OctoPrint's Plugin Manager can install:
#   Settings -> Plugin Manager -> Get More... -> "...from an uploaded file"
# Output: dist/OctoPrint-LedControl-<version>.zip (pyproject.toml at the archive root).
set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT"

version="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)"
name="OctoPrint-LedControl-$version"
mkdir -p dist
out="$PROJECT/dist/$name.zip"
rm -f "$out"

# Stage a clean copy so pycache, egg-info and deploy config never end up in the archive.
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir "$stage/$name"
rsync -a --exclude '__pycache__/' --exclude '*.egg-info/' --exclude '*.pyc' --exclude 'dist/' \
  --exclude 'build/' --exclude 'scripts/deploy.env' --exclude '.installed-sum' ./ "$stage/$name/"

# Plugin Manager wants the files at the archive root, not inside a folder.
(cd "$stage/$name" && zip -qr "$out" .)
echo "Built $out"
