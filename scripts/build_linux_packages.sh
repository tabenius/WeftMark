#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
VERSION="$(python -c 'import importlib.metadata; print(importlib.metadata.version("weftmark"))')"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Invalid version" >&2; exit 1; }
ARCH="$(dpkg --print-architecture)"
[[ "$ARCH" == amd64 ]] || { echo "Unsupported architecture: $ARCH" >&2; exit 1; }
mkdir -p dist

build_one() {
  local kind="$1" entry="$2" extra="$3"
  python -m PyInstaller --noconfirm --clean --onefile --name "weftmark-$kind" \
    --paths src $extra "$entry"
  test -x "dist/weftmark-$kind"
  "dist/weftmark-$kind" --help >/dev/null
  install -m 0755 "dist/weftmark-$kind" "dist/weftmark-$kind-${VERSION}-linux-amd64"
  local stage
  stage="$(mktemp -d)"
  mkdir -p "$stage/DEBIAN" "$stage/usr/bin" "$stage/usr/share/doc/weftmark-$kind"
  install -m 0755 "dist/weftmark-$kind" "$stage/usr/bin/weftmark-$kind"
  install -m 0644 README.md "$stage/usr/share/doc/weftmark-$kind/README.md"
  if [[ "$kind" == cli ]]; then
    ln -s weftmark-cli "$stage/usr/bin/weftmark"
  fi
  cat > "$stage/DEBIAN/control" <<EOF
Package: weftmark-$kind
Version: $VERSION
Section: devel
Priority: optional
Architecture: $ARCH
Maintainer: RAGBAZ <noreply@ragbaz.cc>
Description: WeftMark $kind application
 Standalone WeftMark $kind application and local review tools.
 Experimental package; not a production attestation authority.
EOF
  dpkg-deb --build --root-owner-group "$stage" "dist/weftmark-$kind"_"$VERSION"_"$ARCH".deb
  dpkg-deb --contents "dist/weftmark-$kind"_"$VERSION"_"$ARCH".deb
  rm -rf "$stage"
}
build_one cli src/weftmark/cli/main.py ""
build_one tui src/weftmark/tui/app.py "--collect-all textual"
