#!/bin/sh
# Extract translatable strings from the 'derp' directory

set -e # Exit immediately if a command exits with a non-zero status.

# Ensure the script is run from the project root
cd "$(dirname "$0")/.."

echo "Extracting messages from derp/ to derp/locales/messages.pot..."

uv run pybabel extract \
    -k _:1,1t -k _:1,2 \
    --input-dirs=derp \
    -o derp/locales/messages.pot \
    --project=derp \
    --version=0.1

echo "Extraction complete." 