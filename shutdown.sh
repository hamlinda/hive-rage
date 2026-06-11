#!/usr/bin/env bash
set -euo pipefail

# Resolve the directory where the script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Stopping Hive Rage stack ==="

if [ -f ".venv/bin/hive-rage" ]; then
    .venv/bin/hive-rage stop-stack --force
else
    echo "Virtual environment or hive-rage CLI not found. Stack might not be running or not initialized."
    echo "Trying alternative stop method..."
    if [ -f ".venv/bin/python3" ]; then
        .venv/bin/python3 -m hive_rage.cli stop-stack --force || true
    fi
fi

echo "=== Hive Rage stack stopped ==="
