@echo off
title Panalangin Server
color 0A

REM Always run from the folder where this batch file lives
cd /d "%~dp0"

echo.
echo  ============================================
echo   Panalangin - Mass Intentions Platform
echo  ============================================
echo.
echo  Working folder: %~dp0
echo.

REM Skip C:\Python312 — it is broken. Use the good install directly.
SET PYEXE="C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python312\python.exe"

REM Confirm it exists
IF NOT EXIST %PYEXE% (
    echo  ERROR: Could not find Python at:
    echo  C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python312\python.exe
    echo.
    echo  Please install Python from https://python.org
    echo  Check "Add Python to PATH" during install.
    pause
    EXIT /B 1
)

echo  Using Python: %PYEXE%
echo.

REM Fix pip if missing by downloading get-pip.py
%PYEXE% -m pip --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo  pip missing — installing it now...
    powershell -Command "Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile 'get-pip.py'"
    %PYEXE% get-pip.py
    del get-pip.py
)

REM Confirm pip now works
%PYEXE% -m pip --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo  ERROR: pip still not available. 
    echo  Right-click this file and choose Run as administrator.
    pause
    EXIT /B 1
)

REM Install dependencies if needed
echo  Checking dependencies...
%PYEXE% -m pip show fastapi >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo  Installing dependencies - one moment...
    echo.
    %PYEXE% -m pip install -r requirements.txt
    IF %ERRORLEVEL% NEQ 0 (
        echo.
        echo  ERROR: Could not install dependencies.
        pause
        EXIT /B 1
    )
) ELSE (
    echo  Dependencies already installed.
)

echo.
echo  ============================================
echo   Server is starting...
echo  ============================================
echo.
echo  Dashboard : http://localhost:8000
echo  Display   : http://localhost:8000/demo/display
echo.
echo  Login with:
echo    Email   : admin@demo.com
echo    Password: admin1234
echo.
echo  Press Ctrl+C to stop the server.
echo  ============================================
echo.

REM Open browser after short delay
start "" cmd /c "timeout /t 3 >nul && start http://localhost:8000"

REM Start server
%PYEXE% -m uvicorn main:app --reload --host 0.0.0.0 --port 8000

pause
