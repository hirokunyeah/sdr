@echo off
rem Extract tools\windows\archives (rtl-sdr-blog DLLs, SatDump). Add -Force to re-extract.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\windows\setup.ps1" %*
pause
