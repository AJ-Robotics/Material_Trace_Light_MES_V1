@echo off
REM Material Tracking System - LAN server launcher (Waitress / Flask)
REM Serves the app on ALL network interfaces so other PCs on the same
REM LAN can open it at  http://<this-PC-IP>:5000

echo.
echo ================================================
echo   Material Tracking System - LAN server
echo ================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    pause
    exit /b 1
)

REM --- dependencies: only pip-install when something is missing, so an
REM --- already-installed / offline PC never needs the internet to start
python -c "import flask, waitress, openpyxl" >nul 2>&1
if errorlevel 1 (
    echo Dependencies missing - installing from requirements.txt ...
    pip install --disable-pip-version-check -q -r requirements.txt
    if errorlevel 1 (
        echo WARNING: pip install failed - the PC may be offline.
        echo          If the requirements are already installed, continue.
    )
) else (
    echo Dependencies OK - no internet access needed.
)

echo.
echo Starting Waitress server on 0.0.0.0:5000 ...
echo.

REM Local LAN address taken from the Windows host entry only - no DNS query,
REM no packet sent anywhere else (the old trick used a UDP connect() to a
REM public DNS server, which meant an internet route on every start).
python -c "import socket; ips=socket.gethostbyname_ex(socket.gethostname())[2]; c=[x for x in ips if x.startswith(('192.168.','10.','172.','169.254.'))] or [x for x in ips if not x.startswith('127.')] or ['127.0.0.1']; print('Access from other PCs on the network at:  http://'+c[0]+':5000')" 2>nul
echo Local access:                     http://127.0.0.1:5000
echo.
echo Press Ctrl+C to stop the server
echo.

cd /d "%~dp0"
python -m waitress --host=0.0.0.0 --port=5000 --threads=8 flask_app:app

pause