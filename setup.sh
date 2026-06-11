#!/usr/bin/env bash
set -euo pipefail

# Resolve the directory where the script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Setting up Hive Rage stack ==="

# 1. Create/Validate Virtual Environment
RECREATE_VENV=false
if [ ! -d ".venv" ]; then
    RECREATE_VENV=true
else
    # Verify virtualenv python executable is working and points to correct path
    if ! .venv/bin/python3 -c "import sys; import pathlib" 2>/dev/null; then
        echo "Existing .venv is invalid, broken, or has incorrect paths. Recreating..."
        RECREATE_VENV=true
    fi
fi

if [ "$RECREATE_VENV" = true ]; then
    echo "Creating virtual environment..."
    rm -rf .venv
    if command -v uv &> /dev/null; then
        echo "Using uv for fast environment setup..."
        uv venv --color always
        # Ensure pip is upgraded inside venv using uv if needed
    else
        echo "Using python3 -m venv..."
        python3 -m venv .venv
    fi
fi

# 2. Install dependencies in editable mode
echo "Installing/Updating Hive Rage package and dependencies..."
if command -v uv &> /dev/null; then
    uv pip install -e .
else
    .venv/bin/python3 -m pip install -e .
fi

# 3. Ensure necessary directories exist
echo "Creating data and log directories..."
mkdir -p hive
mkdir -p var/log

# 4. Check Ollama service and models
echo "Checking Ollama status..."
if curl -s -f http://127.0.0.1:11434/ > /dev/null; then
    echo "Ollama is running."
    
    # Check chat model
    echo "Verifying Ollama models..."
    if command -v ollama &> /dev/null; then
        if ! ollama list | grep -q "llama3"; then
            echo "Pulling llama3:8b..."
            ollama pull llama3:8b
        else
            echo "Model llama3:8b is available."
        fi
        
        if ! ollama list | grep -q "nomic-embed-text"; then
            echo "Pulling nomic-embed-text..."
            ollama pull nomic-embed-text
        else
            echo "Model nomic-embed-text is available."
        fi
    else
        echo "Ollama command line tool not found in PATH, skipping automatic pull."
    fi
else
    echo "WARNING: Ollama service is not running or not listening on http://127.0.0.1:11434"
    echo "Please ensure Ollama is installed and running."
fi

# 5. Start the coordinated stack
echo "Starting the Hive Rage stack..."
.venv/bin/hive-rage start-stack

echo "=== Hive Rage setup and startup complete ==="
