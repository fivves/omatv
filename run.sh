#!/bin/bash
# Run omatv from wherever the repo lives. readlink -f resolves the
# ~/.local/bin/omatv symlink that install.sh creates, so ./main.py resolves
# next to the real repo dir, not next to the symlink.
cd "$(dirname "$(readlink -f "$0")")"
exec python3 ./main.py "$@"
