@echo off
rem Double-click to start Voiceprint: worker, API, MCP tunnel and the GUI-driven runtime in this one window.
rem First run asks for the controller identity once; everything else happens in the browser at /ui.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\dev.ps1" up %*
if errorlevel 1 pause
