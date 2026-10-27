@echo off
rem Doble clic aquí para instalar AudioTranscript
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo  No encontre Python. Instalalo desde https://www.python.org/downloads/
  echo  ^(marca "Add python.exe to PATH" al instalar^) y vuelve a abrir este archivo.
  echo.
  pause
  exit /b 1
)
python instalar.py
pause
