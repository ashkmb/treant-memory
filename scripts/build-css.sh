#!/usr/bin/env bash
# Rebuild the dashboard stylesheet (src/treant/dashboard/static/app.css) from css/app.css.
#
# Tools: the Tailwind CSS standalone CLI and the daisyUI plugin files, both pinned by
# version and SHA-256 and cached in .cache/css/ (git-ignored). The output is committed,
# so neither the image build nor the deploy needs the internet or Node.
#
# Run after changing a template's classes or css/app.css:  scripts/build-css.sh
set -euo pipefail

TAILWIND_VERSION=v4.3.3
DAISYUI_VERSION=v5.7.50
DAISYUI_SHA256=d69de07c7463bb1f41548e2688f88ee105cdadf9779fce5b3c47b3bfaa6be472
DAISYUI_THEME_SHA256=e48a35d8326d650181ca600560e8520cf6b44cf9242bbd9d45825607e641f251

root=$(cd "$(dirname "$0")/.." && pwd)
cache="$root/.cache/css"
mkdir -p "$cache"

case "$(uname -s)-$(uname -m)" in
  Darwin-arm64) asset=tailwindcss-macos-arm64 sha=cdf646702987a743464dff4d9c60fd4480d1c1e73dd819a9a67f1078815dce9d ;;
  Darwin-x86_64) asset=tailwindcss-macos-x64 sha=7922e0953f2110c05976e3bf58f14e643d90427575e766b7d433f5f80cbee7e1 ;;
  Linux-x86_64) asset=tailwindcss-linux-x64 sha=dc61b3ac6b8c9ca874c0cc4c57b2409791a64c5540404ca5f5367360babc313a ;;
  Linux-aarch64 | Linux-arm64) asset=tailwindcss-linux-arm64 sha=55fd0b241214eff3de1e8ee4f22796662f2d2e7a49bcfca7477cfd0bac398195 ;;
  *) echo "unsupported platform: $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

# fetch URL DEST SHA256: download unless DEST already matches, then verify.
fetch() {
  local url=$1 dest=$2 want=$3
  if [[ -f $dest ]] && [[ $(shasum -a 256 "$dest" | cut -d' ' -f1) == "$want" ]]; then
    return
  fi
  curl -fsSL -o "$dest.part" "$url"
  local got
  got=$(shasum -a 256 "$dest.part" | cut -d' ' -f1)
  if [[ $got != "$want" ]]; then
    rm -f "$dest.part"
    echo "checksum mismatch for $url: got $got, want $want" >&2
    exit 1
  fi
  mv "$dest.part" "$dest"
}

fetch "https://github.com/tailwindlabs/tailwindcss/releases/download/$TAILWIND_VERSION/$asset" \
  "$cache/$asset-$TAILWIND_VERSION" "$sha"
chmod +x "$cache/$asset-$TAILWIND_VERSION"
fetch "https://github.com/saadeghi/daisyui/releases/download/$DAISYUI_VERSION/daisyui.mjs" \
  "$cache/daisyui.mjs" "$DAISYUI_SHA256"
fetch "https://github.com/saadeghi/daisyui/releases/download/$DAISYUI_VERSION/daisyui-theme.mjs" \
  "$cache/daisyui-theme.mjs" "$DAISYUI_THEME_SHA256"

cd "$root"
"$cache/$asset-$TAILWIND_VERSION" --input css/app.css \
  --output src/treant/dashboard/static/app.css --minify
