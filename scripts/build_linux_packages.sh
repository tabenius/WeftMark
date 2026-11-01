#!/usr/bin/env bash
set -euo pipefail

# GitHub Actions Linux AMD64 packaging. Run inside the locked uv environment.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
VERSION="$(python -c 'import importlib.metadata; print(importlib.metadata.version("weftmark"))')"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Invalid version: $VERSION" >&2; exit 1; }
ARCH="$(dpkg --print-architecture)"
[[ "$ARCH" == amd64 ]] || { echo "Only amd64 currently supported; got $ARCH" >&2; exit 1; }
mkdir -p dist
python -m PyInstaller --noconfirm --clean --onefile --name weftmark --paths src src/weftmark/cli/main.py
test -x dist/weftmark
dist/weftmark --help >/dev/null
cp dist/weftmark "dist/weftmark-${VERSION}-linux-amd64"
chmod +x "dist/weftmark-${VERSION}-linux-amd64"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/DEBIAN" "$stage/usr/bin" "$stage/usr/share/doc/weftmark"
install -m 0755 "dist/weftmark-${VERSION}-linux-amd64" "$stage/usr/bin/weftmark"
install -m 0644 README.md "$stage/usr/share/doc/weftmark/README.md"
cat > "$stage/DEBIAN/control" <<EOF
Package: weftmark
Version: $VERSION
Section: devel
Priority: optional
Architecture: $ARCH
Maintainer: RAGBAZ <noreply@ragbaz.cc>
Description: WeftMark local coordination and evidence review CLI
 Standalone WeftMark CLI for workspace coordination and evidence inspection.
 This experimental package is not a production attestation authority.
EOF
dpkg-deb --build --root-owner-group "$stage" "dist/weftmark_${VERSION}_${ARCH}.deb"
test -s "dist/weftmark_${VERSION}_${ARCH}.deb"
dpkg-deb --info "dist/weftmark_${VERSION}_${ARCH}.deb"
