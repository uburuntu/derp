#!/usr/bin/env bash
# Renders the demo and the social card, then encodes the README GIF (960 px wide, 12 fps, about 6 MB).
set -euo pipefail
cd "$(dirname "$0")"
node_modules/.bin/remotion render Demo out/derp-demo.mp4 --codec=h264 --crf=20
node_modules/.bin/remotion still Social out/social-preview.png
ffmpeg -loglevel error -y -i out/derp-demo.mp4 -vf "fps=12,scale=960:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128:stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" out/demo.gif
cp out/demo.gif ../.github/assets/demo.gif
cp out/social-preview.png ../.github/assets/social-preview.png
ls -la ../.github/assets
