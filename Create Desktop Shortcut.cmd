@echo off
setlocal
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_shortcut.ps1"
if errorlevel 1 (
  echo Shortcut creation failed. You can still open the current Agora executable directly.
  pause
  exit /b 1
)
pause
