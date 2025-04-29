#!/bin/sh
# Initialize a new language .po file

set -e
cd "$(dirname "$0")/.."

if [ -z "$1" ]; then
  echo "Usage: $0 <language_code>"
  echo "Example: $0 fr" 
  exit 1
fi

LOCALE=$1

echo "Initializing language '$LOCALE' in derp/locales..."

uv run pybabel init \
    -i derp/locales/messages.pot \
    -d derp/locales \
    -D messages \
    -l "$LOCALE"

echo "Initialization complete for '$LOCALE'. Please translate derp/locales/$LOCALE/LC_MESSAGES/messages.po" 