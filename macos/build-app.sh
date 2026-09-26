#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DIST_DIR="$PROJECT_ROOT/dist"
APP_DIR="$DIST_DIR/Image Factory.app"
ICON_SOURCE="$SCRIPT_DIR/assets/image-factory-app-icon-v2.png"

die() {
  print -u2 "构建失败：$1"
  exit 1
}

[[ "$(uname -s)" == "Darwin" ]] || die "这个 App 只能在 macOS 上构建。"
for tool in swiftc codesign sips iconutil plutil ditto; do
  command -v "$tool" >/dev/null 2>&1 || die "找不到 $tool。请安装 Xcode Command Line Tools。"
done
[[ -f "$SCRIPT_DIR/ImageFactoryLauncher.swift" ]] || die "缺少 macos/ImageFactoryLauncher.swift。"
[[ -f "$SCRIPT_DIR/Info.plist" ]] || die "缺少 macos/Info.plist。"
[[ -f "$ICON_SOURCE" ]] || die "缺少 App 图标源文件。"
[[ -x "$PROJECT_ROOT/.venv/bin/python" ]] || die "找不到项目 Python：.venv/bin/python。"
[[ -f "$PROJECT_ROOT/config.local.json" ]] || die "找不到 config.local.json。"

mkdir -p "$DIST_DIR"
STAGING_DIR="$(mktemp -d "$DIST_DIR/.image-factory-build.XXXXXX")"
cleanup() {
  if [[ -n "${STAGING_DIR:-}" && -d "$STAGING_DIR" ]]; then
    rm -rf "$STAGING_DIR"
  fi
}
trap cleanup EXIT

BUNDLE="$STAGING_DIR/Image Factory.app"
CONTENTS="$BUNDLE/Contents"
MACOS_DIR="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"
ICONSET="$STAGING_DIR/AppIcon.iconset"
mkdir -p "$MACOS_DIR" "$RESOURCES" "$ICONSET"
cp "$SCRIPT_DIR/Info.plist" "$CONTENTS/Info.plist"
cp "$ICON_SOURCE" "$RESOURCES/AppIcon.png"

make_icon() {
  local size="$1"
  local name="$2"
  /usr/bin/sips -s format png -z "$size" "$size" "$ICON_SOURCE" --out "$ICONSET/$name" >/dev/null
}

make_icon 16 icon_16x16.png
make_icon 32 icon_16x16@2x.png
make_icon 32 icon_32x32.png
make_icon 64 icon_32x32@2x.png
make_icon 128 icon_128x128.png
make_icon 256 icon_128x128@2x.png
make_icon 256 icon_256x256.png
make_icon 512 icon_256x256@2x.png
make_icon 512 icon_512x512.png
make_icon 1024 icon_512x512@2x.png
/usr/bin/iconutil -c icns "$ICONSET" -o "$RESOURCES/AppIcon.icns"

/usr/bin/swiftc -parse-as-library -O \
  -framework SwiftUI \
  -framework AppKit \
  -framework Foundation \
  -o "$MACOS_DIR/ImageFactoryLauncher" \
  "$SCRIPT_DIR/ImageFactoryLauncher.swift"
/usr/bin/plutil -lint "$CONTENTS/Info.plist" >/dev/null
/usr/bin/codesign --force --deep --sign - "$BUNDLE" >/dev/null
/usr/bin/codesign --verify --deep --strict "$BUNDLE" >/dev/null

BACKUP_PATH=""
OLD_BUNDLE="$STAGING_DIR/Previous Image Factory.app"
if [[ -e "$APP_DIR" ]]; then
  BACKUP_DIR="$DIST_DIR/previous-builds"
  mkdir -p "$BACKUP_DIR"
  BACKUP_PATH="$BACKUP_DIR/Image Factory $(date +%Y%m%d-%H%M%S)-$$.zip"
  /usr/bin/ditto -c -k --sequesterRsrc --keepParent "$APP_DIR" "$BACKUP_PATH"
  mv "$APP_DIR" "$OLD_BUNDLE"
fi

if ! mv "$BUNDLE" "$APP_DIR"; then
  if [[ -e "$OLD_BUNDLE" && ! -e "$APP_DIR" ]]; then
    mv "$OLD_BUNDLE" "$APP_DIR"
  fi
  die "无法将 App 放到 dist。"
fi

print "Built: $APP_DIR"
print "签名：本机 ad-hoc；未公证。"
