@echo off
setlocal
cd /d "%~dp0"
for /f "usebackq delims=" %%F in (`powershell.exe -NoProfile -Command "$root='%~dp0'; Get-ChildItem -LiteralPath $root -File -Filter 'CodexConductor-v*.exe' | ForEach-Object { if ($_.BaseName -match '^CodexConductor-v(\d+\.\d+\.\d+)$') { [pscustomobject]@{Path=$_.FullName;Version=[version]$Matches[1]} } } | Sort-Object Version -Descending | Select-Object -First 1 -ExpandProperty Path"`) do (
  start "" "%%F"
  exit /b 0
)
if exist "CodexConductor.exe" (
  start "" "CodexConductor.exe"
  exit /b 0
)
if not exist ".venv\Scripts\pythonw.exe" (
  echo Run setup.cmd first.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "codex_conductor.py"
