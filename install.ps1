<#
  Agent Island installer (Windows).
  Creates .venv next to this script, installs dependencies and the launchers.
    powershell -ExecutionPolicy Bypass -File install.ps1              # install
    powershell -ExecutionPolicy Bypass -File install.ps1 -Autostart   # also start at sign-in
    powershell -ExecutionPolicy Bypass -File install.ps1 -NoAutostart # remove the sign-in entry
#>
param([switch]$Autostart, [switch]$NoAutostart)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$venv = Join-Path $root '.venv'
$py = Join-Path $venv 'Scripts\python.exe'
$pyw = Join-Path $venv 'Scripts\pythonw.exe'
$link = Join-Path ([Environment]::GetFolderPath('Startup')) 'Agent Island.lnk'

if ($NoAutostart) {
  Remove-Item $link -ErrorAction SilentlyContinue
  Write-Host 'Autostart removed.'
  return
}

if (-not (Test-Path $py)) {
  $uv = Get-Command uv -ErrorAction SilentlyContinue
  if ($uv) { & $uv.Source venv $venv --python 3.12 }
  else { python -m venv $venv }
}
$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($uv) { & $uv.Source pip install --python $py -r (Join-Path $root 'requirements.txt') }
else { & $py -m pip install -r (Join-Path $root 'requirements.txt') }

if ($Autostart) {
  $shell = New-Object -ComObject WScript.Shell
  $s = $shell.CreateShortcut($link)
  $s.TargetPath = $pyw
  $s.Arguments = '"' + (Join-Path $root 'app\island.py') + '"'
  $s.WorkingDirectory = $root
  $s.Save()
  Write-Host "Autostart enabled: $link"
}

Write-Host ''
Write-Host 'Installed. Start the island with island.cmd, or island.cmd + browser with dashboard.cmd.'
