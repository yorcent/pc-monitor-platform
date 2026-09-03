@echo off
chcp 65001 >nul
title PC Performance Monitor & Optimizer
cd /d "%~dp0"

rem ===== 检测 Python =====
set "PY=python"
where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if errorlevel 1 (
    echo [错误] 未检测到 Python 3。请先安装 Python：https://www.python.org/downloads/
    echo 安装时务必勾选 "Add Python to PATH"。
    pause
    exit /b 1
  )
  set "PY=py -3"
)

rem ===== 检测并安装 psutil =====
%PY% -c "import psutil" >nul 2>nul
if errorlevel 1 (
  echo 首次运行：正在安装依赖 psutil ...
  %PY% -m pip install psutil
)

echo ============================================
echo   PC Performance Monitor & Optimizer
echo   Starting local service on 127.0.0.1:8765
echo   Browser will open automatically...
echo   Stop via the button on the page or close this window
echo ============================================
start "PC-Monitor-Server" %PY% run.py
