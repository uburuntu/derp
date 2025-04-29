#!/bin/sh
# Update existing .po files from the .pot template

set -e
cd "$(dirname "$0")/.."

echo "Updating .po files from derp/locales/messages.pot..."

uv run pybabel update \
    -d derp/locales \
    -D messages \
    -i derp/locales/messages.pot

echo "Update complete. Please review and translate the .po files." 