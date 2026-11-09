@echo off
REM Material Tracking & Poka-Yoke System - Application Launcher
REM This script runs the Streamlit application

echo.
echo ========================================
echo Material Tracking & Poka-Yoke System
echo ========================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    echo Please install Python 3.10 or higher
    pause
    exit /b 1
)

REM Only install when something is actually missing - an already provisioned
REM PC never contacts PyPI, so the launcher also works with no internet.
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

REM Run the application
echo.
echo Starting Material Tracking System...
echo.
echo The application will open in your browser at: http://localhost:8501
echo.
echo Press Ctrl+C to stop the server
echo.

python -m streamlit run app.py

pause
