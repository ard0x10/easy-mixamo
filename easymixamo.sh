#!/usr/bin/env bash
# easy-mixamo launcher for macOS and Linux.
#   ./easymixamo.sh              -> opens the GUI
#   ./easymixamo.sh build --...  -> forwards to the command line front end
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

py=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then py="$c"; break; fi
done
if [ -z "$py" ]; then
    echo "Python 3 was not found. Install it from https://www.python.org/downloads/" >&2
    exit 1
fi

if [ $# -eq 0 ]; then
    exec "$py" "$here/gui.py"
fi
exec "$py" "$here/easymixamo.py" "$@"
