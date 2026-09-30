@echo off
rem Drag the game's .iso onto this file: it is extracted next to it into "<name>_extracted" (correct lower-case names).
cd /d "%~dp0"
if "%~1"=="" (
  echo Drag the Midnight Club: Los Angeles .iso file onto extract_iso.bat
  pause
  exit /b 1
)
py iso_extract.py "%~1" "%~dpn1_extracted"
pause
