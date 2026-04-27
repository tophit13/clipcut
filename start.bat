@echo off
echo Installing dependencies...
pip install -r requirements.txt
echo.
echo Starting ClipCut at http://localhost:5000
echo Press Ctrl+C to stop.
echo.
python server.py
pause
