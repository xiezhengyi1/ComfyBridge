@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

REM --- ASCII only on purpose: CJK text inside .bat breaks cmd parsing ---
REM --- Strategy: use .vendor if present (no pip needed). Otherwise check
REM --- imports and install via pip if missing. _vendor_install.py is the
REM --- no-pip fallback (downloads pure-python wheels directly).

if exist ".vendor\fastapi" goto :start

python -c "import fastapi, uvicorn" >nul 2>&1
if not errorlevel 1 goto :start

echo [ComfyBridge] Dependencies missing, installing via pip...
pip install -r requirements.txt
if not errorlevel 1 goto :start

echo [ComfyBridge] pip failed, trying vendored packages...
python _vendor_install.py
if errorlevel 1 (
    echo [ComfyBridge] FATAL: could not install dependencies.
    echo [ComfyBridge] Try manually: pip install -r requirements.txt
    pause
    exit /b 1
)

:start
if exist ".vendor\fastapi" set "PYTHONPATH=%CD%\.vendor"
echo [ComfyBridge] Starting server at http://127.0.0.1:8000
echo [ComfyBridge] API key is printed on first run (also in config.json)
python -m uvicorn app:app --host 127.0.0.1 --port 8000
pause
