#!/bin/bash
# Copyright (C) 2026 OpenMV, LLC.
#
# This software is licensed under terms that can be found in the
# LICENSE file in the root directory of this software component.
#
# Converts the built .deb package into an Arch Linux .pkg.tar.zst.
# Usage: ./scripts/build-arch-pkg.sh <version>

set -euo pipefail

VER="${1:?Usage: $0 <version>}"
PKG="openmv-studio-${VER}-1-x86_64"

DEB=$(find src-tauri/target -name '*.deb' | head -1)
if [ -z "$DEB" ]; then
    echo "ERROR: No .deb found in src-tauri/target/" >&2
    exit 1
fi

echo "Converting ${DEB} to Arch package..."

mkdir -p archpkg
cd archpkg
ar x "../${DEB}"
tar xf data.tar.*

INSTALLED_SIZE=$(du -sb usr/ | awk '{print $1}')
BUILD_DATE=$(date +%s)

cat > .PKGINFO <<EOF
pkgname = openmv-studio
pkgbase = openmv-studio
xdata = pkgtype=pkg
pkgver = ${VER}-1
pkgdesc = OpenMV Studio - Machine Vision Made Simple
url = https://github.com/openmv/openmv-studio
builddate = ${BUILD_DATE}
packager = OpenMV, LLC <info@openmv.io>
size = ${INSTALLED_SIZE}
arch = x86_64
license = MIT
depend = cairo
depend = desktop-file-utils
depend = gdk-pixbuf2
depend = glib2
depend = gtk3
depend = hicolor-icon-theme
depend = libsoup3
depend = openssl
depend = pango
depend = webkit2gtk-4.1
depend = systemd-libs
EOF

tar cf "../${PKG}.pkg.tar.zst" --zstd .PKGINFO usr/
cd ..
rm -rf archpkg

echo "Created ${PKG}.pkg.tar.zst"
