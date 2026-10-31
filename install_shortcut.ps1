param([string]$DesktopPath = [Environment]::GetFolderPath('DesktopDirectory'))
$ErrorActionPreference = 'Stop'
$versioned = Get-ChildItem -LiteralPath $PSScriptRoot -File -Filter 'CodexConductor-v*.exe' |
    ForEach-Object {
        if ($_.BaseName -match '^CodexConductor-v(?<Version>\d+\.\d+\.\d+)$') {
            try { [pscustomobject]@{ Path = $_.FullName; Version = [version]$Matches.Version } } catch { }
        }
    } | Sort-Object Version -Descending | Select-Object -First 1
$executable = if ($versioned) { $versioned.Path } else { Join-Path $PSScriptRoot 'CodexConductor.exe' }
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw 'Extract the entire ZIP before creating the shortcut.'
}
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '_internal') -PathType Container)) {
    throw 'Keep the _internal runtime folder beside the application executable.'
}
if (-not (Test-Path -LiteralPath $DesktopPath -PathType Container)) {
    throw 'Desktop directory does not exist.'
}
# Executables created in a low-integrity workspace otherwise launch at low integrity.
$icacls = Join-Path $env:SystemRoot 'System32\icacls.exe'
$launchers = @($executable, (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'), (Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'))
foreach ($launcher in $launchers) {
    if (Test-Path -LiteralPath $launcher -PathType Leaf) {
        & $icacls $launcher /setintegritylevel M | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to restore normal user integrity on launcher: $launcher"
        }
    }
}
$shell = New-Object -ComObject WScript.Shell
$displayName = 'Agora ' + [char]0x00b7 + ' ' + [char]0x8bae + [char]0x5ead
$shortcutPath = Join-Path $DesktopPath ($displayName + '.lnk')
$legacyPath = Join-Path $DesktopPath 'Codex Conductor.lnk'
if ((Test-Path -LiteralPath $legacyPath -PathType Leaf) -and -not (Test-Path -LiteralPath $shortcutPath)) {
    $legacy = $shell.CreateShortcut($legacyPath)
    if ($legacy.TargetPath -eq $executable) {
        Move-Item -LiteralPath $legacyPath -Destination $shortcutPath
    }
}
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $executable
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.IconLocation = "$executable,0"
$shortcut.Description = $displayName + ' / Codex and DeepSeek Harness collaboration'
$shortcut.Save()
Write-Output 'Desktop shortcut created. Keep the extracted application folder.'
