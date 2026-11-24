#!/bin/sh
# Render the Odoo Apps Store images from the HTML sources next to this script.
# Needs Google Chrome; output goes to tk_pushnotification/static/description/.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
out="$here/../tk_pushnotification/static/description"
chrome=${CHROME:-"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"}

shot() {  # source width height output
    "$chrome" --headless=new --disable-gpu --hide-scrollbars --no-first-run \
        --force-device-scale-factor=2 --default-background-color=00000000 \
        --window-size="$2,$3" --screenshot="$4" "file://$here/$1" 2>/dev/null
}

shot icon.html 128 128 "$out/icon.png"      # 256x256
shot banner.html 560 280 "$out/banner.png"  # 1120x560
