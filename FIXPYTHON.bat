@echo off
title Fixing Python pip
color 0A

echo.
echo  ============================================
echo   Fixing pip - please wait...
echo  ============================================
echo.

SET PYEXE=C:\Python312\python.exe

REM Step 1: Download get-pip.py using PowerShell (built into all Windows 10/11)
echo  Downloading pip installer...
powershell -Command "Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile 'get-pip.py'"

IF NOT EXIST get-pip.py (
    echo  ERROR: Could not download pip. Check your internet connection.
    pause
    EXIT /B 1
)

REM Step 2: Install pip into the broken Python
echo  Installing pip into C:\Python312...
%PYEXE% get-pip.py

IF %ERRORLEVEL% NEQ 0 (
    echo.
    echo  ERROR: pip install failed.
    echo  Try right-clicking FIXPYTHON.bat and choosing "Run as administrator"
    pause
    EXIT /B 1
)

REM Step 3: Clean up
del get-pip.py

REM Step 4: Verify
echo.
%PYEXE% -m pip --version
IF %ERRORLEVEL% EQU 0 (
    echo.
    echo  ============================================
    echo   pip is now fixed!
    echo   You can close this window and
    echo   double-click START.bat to run MyParokya.
    echo  ============================================
) ELSE (
    echo.
    echo  Something went wrong. Please take a screenshot
    echo  of this window and share it.
)

echo.
pause
