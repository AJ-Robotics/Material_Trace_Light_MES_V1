@echo off
REM Material Tracking & Poka-Yoke System - Build Script
REM This script creates a Windows executable using PyInstaller

echo.
echo ========================================
echo Building Windows Executable
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

REM Check if PyInstaller is installed
pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    pip install pyinstaller
)

REM Install dependencies only when something is missing (offline friendly)
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

REM Create the executable
echo.
echo Building executable...
echo This may take a few minutes...
echo.

REM PyInstaller command to create a standalone executable
REM Note: If you have an icon.ico file, uncomment the icon parameter below
pyinstaller --name "Material Tracking" ^
    --onedir ^
    --windowed ^
    --add-data "config.py:." ^
    --add-data "auth:auth" ^
    --add-data "database:database" ^
    --add-data "models:models" ^
    --add-data "services:services" ^
    --add-data "ui:ui" ^
    --add-data "utils:utils" ^
    --add-data "templates:templates" ^
    --hidden-import=streamlit ^
    --hidden-import=pandas ^
    --hidden-import=openpyxl ^
    --hidden-import=reportlab ^
    --collect-all streamlit ^
    app.py
REM Uncomment the line below if you have an icon.ico file:
REM pyinstaller --name "Material Tracking" --onedir --windowed --icon=icon.ico ... (rest of parameters)

if errorlevel 1 (
    echo ERROR: Failed to build executable
    echo Please check the error messages above
    pause
    exit /b 1
)

echo.
echo ========================================
echo Build Completed Successfully!
echo ========================================
echo.
echo The executable is located in: dist\Material Tracking
echo.
echo To run the application:
echo 1. Copy the entire "dist\Material Tracking" folder to your target location
echo 2. Run "Material Tracking.exe" from that location
echo.
echo NOTE: For network access, you can run:
echo   streamlit run app.py --server.address 0.0.0.0 --server.port 8501
echo.

pause
