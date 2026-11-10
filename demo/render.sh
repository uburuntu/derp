#!/usr/bin/env bash
# Renders the demo video (with sound) and the social card, and copies the card into .github/assets/.
# The README plays the video from a GitHub attachment: to replace it, drag out/derp-demo.mp4 into the
# README editor on github.com and swap the attachment URL in README.md.
set -euo pipefail
cd "$(dirname "$0")"
node_modules/.bin/remotion render Demo out/derp-demo.mp4 --codec=h264 --crf=20
node_modules/.bin/remotion still Social out/social-preview.png
cp out/social-preview.png ../.github/assets/social-preview.png
ls -la out/derp-demo.mp4 ../.github/assets/social-preview.png
