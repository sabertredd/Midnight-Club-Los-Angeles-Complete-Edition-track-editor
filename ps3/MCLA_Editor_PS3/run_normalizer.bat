@echo off
cd /d "%~dp0"
py mcla_normalize.py %*
if errorlevel 1 pause
