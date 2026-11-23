#!/bin/bash
# backup_index.sh - backs up see_also.db (calls backup_index.py).
# Run in the project folder:  tools/backup_index.sh [target folder]
set -eu
exec venv/bin/python3 tools/backup_index.py "$@"
