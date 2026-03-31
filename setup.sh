#!/bin/bash
set -e

echo "Setting up Article Reader Agent..."

# Check python3
if ! command -v python3 &>/dev/null; then
    echo "Error: python3 not found. Install it from https://python3.org"
    exit 1
fi

# Create a virtual environment
cd "$(dirname "$0")"
python3 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip -q
pip install -r requirements.txt -q

echo ""
echo "Done! To run the reader:"
echo "  source .venv/bin/activate"
echo "  python reader.py"
echo ""
echo "Or one-liner:"
echo "  ~/article_reader/.venv/bin/python ~/article_reader/reader.py"
