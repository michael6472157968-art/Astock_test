@echo off
REM A股量化分析助手 — 生产环境启动（无 --reload）
REM 配合 Nginx + systemd 使用

cd /d "%~dp0backend"
pip install -r requirements.txt -q
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
