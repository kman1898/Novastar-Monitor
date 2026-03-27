#!/usr/bin/env bash
# NovaStar Monitor — Quick Start
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "Error: python3 not found. Install Python 3.10+ first."
    exit 1
fi

# Install dependencies if needed
if ! python3 -c "import flask" 2>/dev/null; then
    echo "Installing dependencies..."
    pip3 install -r requirements.txt
fi

echo ""
echo "  NovaStar Monitor"
echo "  ════════════════"
echo "  Open http://127.0.0.1:8050 in your browser"
echo ""

python3 app.py
