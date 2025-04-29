#!/bin/sh
# Compile .po files to .mo files

set -e
cd "$(dirname "$0")/.."

echo "Compiling translations in derp/locales..."

uv run pybabel compile \
    -d derp/locales \
    -D messages \
    --statistics

echo "Compilation complete." 