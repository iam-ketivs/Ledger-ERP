@echo off
cd /d "%~dp0"
if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
)
call venv\Scripts\activate
pip install -r requirements.txt >nul
echo.
echo Starting Ledger ERP... open http://127.0.0.1:5000 in your browser.
echo Press CTRL+C here to stop it.
echo.
python app.py
pause
