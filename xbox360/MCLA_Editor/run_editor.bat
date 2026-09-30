@echo off
cd /d "%~dp0"
py mcla_gui.py %*
if errorlevel 1 pause
