@echo off
rem miniMaster 快捷运行：run.bat "目标" [额外参数...]
setlocal
set PYTHONUTF8=1
chcp 65001 >nul
set PROJECT=%~dp0
if "%~1"=="" (
    echo 用法: run.bat "目标" [--max-rounds 3 ...]
    exit /b 1
)
"%PROJECT%.conda\python.exe" "%PROJECT%main.py" %*
