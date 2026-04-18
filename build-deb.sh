#!/usr/bin/env bash
# Build cruise-control_<version>_arm64.deb
# Run this on an arm64 machine (GB10/Hopper) or use dpkg-deb cross-build.
# Usage: ./build-deb.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION=$(grep '^Version:' "$SCRIPT_DIR/packaging/DEBIAN/control" | awk '{print $2}')
PKGNAME="cruise-control_${VERSION}_arm64"
BUILDDIR="/tmp/${PKGNAME}"

echo "==> Building ${PKGNAME}.deb"

# Clean
rm -rf "$BUILDDIR"

# DEBIAN control files
mkdir -p "$BUILDDIR/DEBIAN"
cp "$SCRIPT_DIR/packaging/DEBIAN/"* "$BUILDDIR/DEBIAN/"
chmod 755 "$BUILDDIR/DEBIAN/postinst" "$BUILDDIR/DEBIAN/prerm"

# App files
mkdir -p "$BUILDDIR/opt/cruise-control"
cp "$SCRIPT_DIR/app.py"             "$BUILDDIR/opt/cruise-control/app.py"
cp "$SCRIPT_DIR/config.example.json" "$BUILDDIR/opt/cruise-control/config.json"
chmod 644 "$BUILDDIR/opt/cruise-control/app.py"
chmod 644 "$BUILDDIR/opt/cruise-control/config.json"

# Systemd service
mkdir -p "$BUILDDIR/etc/systemd/system"
cp "$SCRIPT_DIR/cruise-control.service" "$BUILDDIR/etc/systemd/system/cruise-control.service"
chmod 644 "$BUILDDIR/etc/systemd/system/cruise-control.service"

# Build
dpkg-deb --build --root-owner-group "$BUILDDIR" "$SCRIPT_DIR/${PKGNAME}.deb"

echo "==> Done: $SCRIPT_DIR/${PKGNAME}.deb"
echo "    Install with: sudo dpkg -i ${PKGNAME}.deb"
