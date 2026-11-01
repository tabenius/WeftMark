#!/usr/bin/env bash
# Shared packaging acceptance gate for CI and release (Linux amd64).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
version="$(python -c 'import weftmark; print(weftmark.__version__)')"
arch="$(dpkg --print-architecture)"
[[ "$arch" == amd64 ]] || { echo "unsupported package architecture: $arch" >&2; exit 1; }
for kind in cli tui; do
  binary="dist/weftmark-${kind}-${version}-linux-amd64"
  package="dist/weftmark-${kind}_${version}_${arch}.deb"
  test -s "$binary"
  test -s "$package"
  test -x "$binary"
  "$binary" --help >/dev/null
  stage="$(mktemp -d)"
  dpkg-deb -x "$package" "$stage"
  "$stage/usr/bin/weftmark-$kind" --help >/dev/null
  if [[ "$kind" == cli ]]; then
    "$stage/usr/bin/weftmark" --help >/dev/null
  fi
  rm -rf "$stage"
done
