@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
venv\Scripts\python.exe -m hayalet %*
if "%~1"=="" pause
