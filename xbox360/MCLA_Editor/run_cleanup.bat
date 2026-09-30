@echo off
cd /d "%~dp0"
py mcla_cleanup.py %*
if errorlevel 1 pause
