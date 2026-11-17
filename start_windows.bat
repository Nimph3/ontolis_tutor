@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel% equ 0 (set "PYCMD=py -3") else (set "PYCMD=python")
if not exist ".venv\Scripts\python.exe" (
    %PYCMD% -m venv .venv
    if errorlevel 1 goto :fail
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" start_web.py
if errorlevel 1 goto :fail
exit /b 0
:fail
echo.
echo Не удалось запустить. Убедитесь, что установлен Python 3.10+ и доступны зависимости.
pause
exit /b 1
