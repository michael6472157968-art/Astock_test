@echo off
REM A股量化分析助手 — 本地开发一键启动
REM 生产环境请使用 deploy/ 下的 systemd 服务 + Nginx

echo ========================================
echo   A股量化分析助手 — 开发模式
echo ========================================
echo.

cd /d "%~dp0backend"

echo [1/2] 安装依赖...
pip install -r requirements-prod.txt -q

echo [2/2] 启动服务...
echo.
echo   前端页面: http://localhost:8000
echo   API文档:  http://localhost:8000/docs
echo   管理后台: http://localhost:8000/admin-trigger.html
echo   按 Ctrl+C 停止
echo.
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

pause
