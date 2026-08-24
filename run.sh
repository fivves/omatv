#!/bin/bash
# Run omatv from wherever the repo lives.
cd "$(dirname "$0")"
exec python3 ./main.py "$@"
