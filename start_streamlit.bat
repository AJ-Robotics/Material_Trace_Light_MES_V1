@echo off
REM Material Tracking System - Streamlit launcher
REM Same DB as the Flask build; run EITHER this or start_server.bat.

echo.
echo ================================================
echo   Material Tracking System - Streamlit
echo ================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    pause
    exit /b 1
)

REM Only install when something is actually missing - works with no internet.
echo Checking dependencies...
python -c "import streamlit, pandas, flask, openpyxl" >nul 2>&1
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

cd /d "%~dp0"
python -m streamlit run app.py --server.address 0.0.0.0 --server.port 8501

pause