<#
  Agent Island installer (Windows 10/11).
  Creates .venv next to this script, installs dependencies, checks the environment.
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

function Fail([string]$msg) {
  Write-Host ''
  Write-Host "ERROR: $msg" -ForegroundColor Red
  exit 1
}
function Warn([string]$msg) { Write-Host "WARNING: $msg" -ForegroundColor Yellow }

if ($NoAutostart) {
  Remove-Item $link -ErrorAction SilentlyContinue
  Write-Host 'Autostart removed.'
  return
}

$uv = Get-Command uv -ErrorAction SilentlyContinue

# --- virtual environment -------------------------------------------------
if (-not (Test-Path $py)) {
  if ($uv) {
    & $uv.Source venv $venv --python 3.12 | Out-Host
    if ($LASTEXITCODE -ne 0) { Fail 'uv could not create the virtual environment.' }
  } else {
    # Python 3.10+ is needed; the Microsoft Store "python" stub and old versions do not count.
    $found = $null
    $ErrorActionPreference = 'Continue'     # probing may print to stderr (e.g. the Store stub); that is not an error here
    foreach ($cand in @(@('py', '-3'), @('python'))) {
      $cmd = Get-Command $cand[0] -ErrorAction SilentlyContinue
      if (-not $cmd) { continue }
      $extra = @($cand | Select-Object -Skip 1)
      $ok = & $cmd.Source @extra -c "import sys; print(int(sys.version_info >= (3, 10)))" 2>$null
      if ($LASTEXITCODE -eq 0 -and "$ok".Trim() -eq '1') { $found = @($cmd.Source) + $extra; break }
    }
    $ErrorActionPreference = 'Stop'
    if (-not $found) {
      Fail "Python 3.10 or newer was not found. Install it first, e.g.:  winget install Python.Python.3.12   (or install uv: https://docs.astral.sh/uv/ ), then run this script again."
    }
    $rest = @($found | Select-Object -Skip 1)
    & $found[0] @rest -m venv $venv | Out-Host
    if ($LASTEXITCODE -ne 0) { Fail 'python -m venv failed.' }
  }
}
if (-not (Test-Path $py)) { Fail "The virtual environment was not created ($py is missing)." }

# --- dependencies --------------------------------------------------------
function Pip([string[]]$pipArgs) {
  if ($uv) { & $uv.Source pip install --python $py @pipArgs | Out-Host }
  else { & $py -m pip install @pipArgs | Out-Host }
  return $LASTEXITCODE
}
if ((Pip @('-r', (Join-Path $root 'requirements.txt'))) -ne 0) {
  Fail 'Installing the dependencies failed (see the messages above; check your internet connection or proxy).'
}
$ErrorActionPreference = 'Continue'
& $py -c "import webview" 2>$null
$importOk = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = 'Stop'
if (-not $importOk) { Fail 'pywebview was installed but cannot be imported.' }

# Optional: AutoSwarm lets the island read the delegation history. It needs Git; without it
# the island still shows quotas and API spend.
$haveSwarm = $false
if (Get-Command git -ErrorAction SilentlyContinue) {
  $haveSwarm = ((Pip @('-r', (Join-Path $root 'requirements-autoswarm.txt'))) -eq 0)
  if (-not $haveSwarm) { Warn 'AutoSwarm could not be installed. The island works, but the delegation history will be empty. Re-run this script after fixing the error above.' }
} else {
  Warn 'Git is not installed, so AutoSwarm (needed for the delegation history) was skipped. Install Git (winget install Git.Git) and re-run this script if you want it.'
}

# --- environment checks (warnings only) -----------------------------------
$wv2 = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
$hasWebView2 = @(
  "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$wv2",
  "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$wv2",
  "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$wv2"
) | Where-Object { Test-Path $_ }
if (-not $hasWebView2) {
  Warn 'The Microsoft Edge WebView2 runtime was not detected. The island needs it: https://developer.microsoft.com/microsoft-edge/webview2/'
}

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
Write-Host 'Installed.' -ForegroundColor Green
Write-Host '  Start the island:           island.cmd   (or dashboard.cmd to open the dashboard too)'
Write-Host '  Live Claude quota:          run  claude auth login  once in a terminal'
Write-Host '  Delegation history:         needs AutoSwarm ' -NoNewline
Write-Host $(if ($haveSwarm) { '(installed)' } else { '(not installed)' })
Write-Host '  Problems on start-up are logged to data\island.log'
