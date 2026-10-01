@echo off
rem miniMaster 一键初始化：项目内 .conda 环境 + 依赖（不动本机 Anaconda base）
setlocal
set PYTHONUTF8=1
set PROJECT=%~dp0
set ENV_DIR=%PROJECT%.conda

if not exist "%ENV_DIR%\python.exe" (
    echo [1/2] Creating project-local conda env: %ENV_DIR%
    conda create -p "%ENV_DIR%" python=3.12 -y || goto :fail
) else (
    echo [1/2] .conda env exists, skip create
)

echo [2/2] Installing dependencies...
"%ENV_DIR%\python.exe" -m pip install -r "%PROJECT%requirements.txt" || goto :fail
"%ENV_DIR%\python.exe" -m pip install -e "%PROJECT%" --no-deps -q || goto :fail

echo.
echo Done. Run with:
echo   "%ENV_DIR%\Scripts\minimaster.exe" "your goal"
echo or: "%ENV_DIR%\python.exe" main.py "your goal"
goto :eof

:fail
echo Setup failed, check errors above.
exit /b 1
