#!/bin/bash
cd "$(dirname "$0")"
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi
source venv/bin/activate
pip install -r requirements.txt -q

echo ""
echo "Starting Ledger ERP... open http://127.0.0.1:5000 in your browser."
echo "Press CTRL+C here to stop it."
echo ""
python app.py
