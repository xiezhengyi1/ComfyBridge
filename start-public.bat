@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if exist ".vendor\fastapi" set "PYTHONPATH=%CD%\.vendor"

echo [ComfyBridge] 对外启动: 监听 0.0.0.0:8000
echo [ComfyBridge] 请确保防火墙/安全组放行 8000 端口
echo [ComfyBridge] Public deployment requires HTTPS reverse proxy; direct HTTP will not set secure browser sessions.
python -m uvicorn app:app --host 0.0.0.0 --port 8000
pause
