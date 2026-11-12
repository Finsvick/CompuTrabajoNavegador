@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if errorlevel 1 (
    echo No se encontro pythonw. Instala Python 3.13 y marca Add python.exe to PATH.
    pause
    exit /b 1
)
start "" pythonw -m compuapply.gui
