@echo off
setlocal enabledelayedexpansion
title ETradeBot — Wheel Strategy UI
color 0A
echo.
echo  ============================================
echo   ETradeBot Wheel Strategy UI
echo  ============================================
echo.

cd /d "%~dp0"

:: ── Find Python ──────────────────────────────────────────────────────────
set PYTHON=
py --version >nul 2>&1
if not errorlevel 1 (
    set PYTHON=py
    goto :found_python
)
python3 --version >nul 2>&1
if not errorlevel 1 (
    set PYTHON=python3
    goto :found_python
)
python --version >nul 2>&1
if not errorlevel 1 (
    set PYTHON=python
    goto :found_python
)
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "C:\Python313\python.exe"
    "C:\Python312\python.exe"
    "C:\Python311\python.exe"
    "%PROGRAMFILES%\Python313\python.exe"
    "%PROGRAMFILES%\Python312\python.exe"
    "%PROGRAMFILES%\Python311\python.exe"
) do (
    if exist %%P (
        set PYTHON=%%P
        goto :found_python
    )
)
echo  ERROR: Python not found.
echo.
echo  Please install Python from https://python.org
echo  Make sure to check "Add Python to PATH" during install.
echo  Then close and re-run this file.
echo.
pause
exit /b 1

:found_python
echo  Found Python: %PYTHON%
%PYTHON% --version
echo.

:: ═════════════════════════════════════════════════════════════════════════
:: UPDATE PROMPT
:: ═════════════════════════════════════════════════════════════════════════
echo  ────────────────────────────────────────────
echo   Would you like to check for updates?
echo   This will update Python packages and
echo   optionally pull Ollama models.
echo  ────────────────────────────────────────────
echo.
set /p UPDATE_CHOICE="  Update dependencies? (Y/N): "
if /i "!UPDATE_CHOICE!"=="Y" goto :do_updates
if /i "!UPDATE_CHOICE!"=="YES" goto :do_updates
goto :skip_updates

:: ── UPDATE FLOW ──────────────────────────────────────────────────────────
:do_updates
echo.
echo  ============================================
echo   Updating Python packages...
echo  ============================================
echo.
echo  Installing/upgrading: flask flask-cors pyetrade yfinance pandas numpy requests requests-oauthlib pytest
echo.
%PYTHON% -m pip install --upgrade flask flask-cors pyetrade yfinance pandas numpy requests requests-oauthlib pytest --quiet
if errorlevel 1 (
    echo.
    echo  WARNING: Some packages may not have installed correctly.
    echo  Check errors above. Server will still attempt to start.
    echo.
) else (
    echo  All Python packages up to date.
    echo.
)

:: Verify key imports
%PYTHON% -c "import flask, pyetrade, yfinance, pandas, numpy; print('  Import check: all OK')" 2>nul
if errorlevel 1 (
    echo  WARNING: Some imports failed. Check terminal for errors.
    echo.
)

:: ── Ollama model updates ─────────────────────────────────────────────────
echo.
echo  ============================================
echo   Ollama model check
echo  ============================================
echo.

:: Check if Ollama is installed
ollama --version >nul 2>&1
if errorlevel 1 (
    echo  Ollama not found — skipping model updates.
    echo  Install from https://ollama.com if you want AI advisor features.
    echo.
    goto :updates_done
)

echo  Ollama found. Checking models from config.json...
echo.

:: Read model names from config.json using Python
for /f "delims=" %%M in ('%PYTHON% -c "import json; c=json.load(open('data/config.json')); print(c.get('model',''))" 2^>nul') do set MODEL1=%%M
for /f "delims=" %%M in ('%PYTHON% -c "import json; c=json.load(open('data/config.json')); print(c.get('model_deep',''))" 2^>nul') do set MODEL2=%%M

:: Ask for model 1
if "!MODEL1!"=="" goto :check_model2
echo  Model 1: !MODEL1!  (main advisor)
set /p PULL1="  Pull/update !MODEL1!? (Y/N): "
if /i "!PULL1!"=="Y" goto :pull_model1
if /i "!PULL1!"=="YES" goto :pull_model1
goto :check_model2

:pull_model1
echo  Pulling !MODEL1!...
ollama pull !MODEL1!
echo.

:check_model2
if "!MODEL2!"=="" goto :updates_done
if "!MODEL2!"=="!MODEL1!" goto :updates_done
echo  Model 2: !MODEL2!  (deep analysis)
set /p PULL2="  Pull/update !MODEL2!? (Y/N): "
if /i "!PULL2!"=="Y" goto :pull_model2
if /i "!PULL2!"=="YES" goto :pull_model2
goto :updates_done

:pull_model2
echo  Pulling !MODEL2!...
ollama pull !MODEL2!
echo.

:updates_done
echo.
echo  Updates complete.
echo.
goto :start_server

:: ── SKIP UPDATES ─────────────────────────────────────────────────────────
:skip_updates
echo.
echo  Skipping updates.
echo.

:: Quick dependency check (no upgrade)
echo  Checking dependencies...
%PYTHON% -m pip install flask flask-cors pyetrade yfinance pandas numpy --quiet
%PYTHON% -c "import flask, pyetrade, yfinance; print('  All dependencies OK')" 2>nul
if errorlevel 1 (
    echo  WARNING: Some imports failed. Run with updates to fix.
)
echo.

:: ── START SERVER ─────────────────────────────────────────────────────────
:start_server

:: Kill anything on port 5000
echo  Checking port 5000...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":5000 " 2^>nul') do (
    taskkill /f /pid %%a >nul 2>&1
)

echo  Starting server...
start /B %PYTHON% server.py

:: Wait for Flask to be ready
timeout /t 3 /nobreak >nul

:: Open browser
echo  Opening browser...
start http://127.0.0.1:5000/ui/index.html

echo.
echo  ============================================
echo   Server running at http://127.0.0.1:5000
echo   Press Ctrl+C or close this window to stop
echo  ============================================
echo.
pause
