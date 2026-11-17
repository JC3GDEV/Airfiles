#!/usr/bin/env bash
# Package Airfiles as an APPLaunch-style .deb (pure Python: no compiling, no
# network needed, builds on any machine with dpkg-deb; the package is
# labelled arm64 because that is what the CardputerZero app store expects).
#
# Usage: ./build-deb.sh
# Env:   VERSION     default: VERSION in airfiles.py
#        MAINTAINER  "Name <email>" for the package Maintainer field
#                    (APP_MAINTAINER works too; default: JackyC <iphone3gdevteam@gmail.com>)
#        OUT_DIR     where the .deb goes (default: ./dist)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PKG="airfiles"
VERSION="${VERSION:-$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$HERE/airfiles.py")}"
MAINTAINER="${MAINTAINER:-${APP_MAINTAINER:-JackyC <iphone3gdevteam@gmail.com>}}"
OUT_DIR="${OUT_DIR:-$HERE/dist}"
mkdir -p "$OUT_DIR"

[ -f "$HERE/airfiles.py" ] || { echo "airfiles.py not found next to this script" >&2; exit 1; }
python3 -m py_compile "$HERE/airfiles.py"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
ROOT="$WORK/root"
mkdir -p "$ROOT/DEBIAN" "$ROOT/usr/bin" "$ROOT/usr/lib/$PKG" \
         "$ROOT/usr/share/APPLaunch/applications" "$ROOT/usr/share/APPLaunch/share/images" \
         "$ROOT/usr/share/doc/$PKG"

install -m 755 "$HERE/airfiles.py" "$ROOT/usr/lib/$PKG/airfiles.py"
[ -f "$HERE/assets/airfiles.png" ] || python3 "$HERE/tools/make_icon.py" 64 "$HERE/assets/airfiles.png"
install -m 644 "$HERE/assets/airfiles.png" "$ROOT/usr/share/APPLaunch/share/images/$PKG.png"

cat > "$ROOT/usr/bin/$PKG" <<'EOF'
#!/bin/sh
exec python3 /usr/lib/airfiles/airfiles.py "$@"
EOF
chmod 755 "$ROOT/usr/bin/$PKG"

cat > "$ROOT/usr/share/APPLaunch/applications/$PKG.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Airfiles
Comment=Receive files from iPhone, Android and PCs
Exec=/usr/bin/$PKG
Icon=/usr/share/APPLaunch/share/images/$PKG.png
Terminal=false
Categories=Utility;
EOF

cat > "$ROOT/usr/share/doc/$PKG/README" <<'EOF'
Airfiles - receive files on the M5Stack CardputerZero.

Scan the QR code on the screen (or open the address shown) from any phone or
computer on the same network, enter nothing else (the PIN is in the link), pick
files. Files land in ~/Airfiles.

Press f for the Files browser: open a file to get a download link (QR code) or to
send it by AirDrop (beta), or rename it. Press p to change the port, l to set the
per-file size limit, q or hold Esc to exit.

Command line:  curl -T photo.jpg "http://<ip>:8088/upload/photo.jpg?pin=<pin>"

Optional config: ~/.config/airfiles/config.json
  {"port": 8088, "inbox": "/home/pi/Airfiles", "pin": true, "max_mb": 2048,
   "airdrop": false, "airdrop_iface": "wlan1", "rotate": 0}

AirDrop is experimental and off by default (needs OWL + OpenDrop and a Wi-Fi
adapter that supports monitor mode). Press "a" in the app to toggle it.
EOF

chmod -R u+rwX,go+rX,go-w "$ROOT"
chmod 755 "$ROOT/usr/lib/$PKG/airfiles.py" "$ROOT/usr/bin/$PKG"

SIZE_KB="$(du -sk --exclude=DEBIAN "$ROOT" | cut -f1)"
cat > "$ROOT/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Architecture: arm64
Maintainer: $MAINTAINER
Installed-Size: $SIZE_KB
Depends: python3, python3-pil, python3-numpy, python3-evdev, python3-qrcode, fonts-dejavu-core
Section: utils
Priority: optional
Description: Airfiles - receive files from phones and PCs on CardputerZero
 Shows a QR code and PIN on the 320x170 screen; scan it from an iPhone,
 Android phone or computer on the same network and upload files from the
 browser. Also accepts curl/Shortcuts uploads. Experimental AirDrop support
 via OWL and OpenDrop is available but off by default.
EOF

OUT="$OUT_DIR/${PKG}_${VERSION}_arm64.deb"
dpkg-deb --root-owner-group --build "$ROOT" "$OUT" >/dev/null
echo "Built $OUT"
echo "Install:  sudo apt install ./$(basename "$OUT")"
