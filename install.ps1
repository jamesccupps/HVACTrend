<#
.SYNOPSIS
  Install HVACTrend on this computer: a private Python environment, the program, and your
  building's configuration.

.DESCRIPTION
  1. Finds Python 3.11 or newer (python.org installer; the Microsoft Store "python" stub is
     skipped because Windows cannot start it as a subprocess).
  2. Creates .venv here and installs HVACTrend and its dependencies into it.
  3. Runs the setup wizard (scripts\configure.py), which writes config\config.yaml.

  Nothing is installed system-wide, and nothing here needs administrator rights. To keep
  HVACTrend running after reboots, run scripts\windows\install-service.ps1 as administrator
  afterwards (docs\install.md).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install.ps1
  powershell -ExecutionPolicy Bypass -File install.ps1 -WithAI      # also the AI summary
#>
param(
    [switch]$WithAI,      # install the optional Anthropic client for the AI summary
    [switch]$NoWizard     # install only; run scripts\configure.py yourself later
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Find-Python {
    $candidates = @(@("py", "-3"), @("python3"), @("python"))
    foreach ($c in $candidates) {
        $exe = $c[0]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $cmdArgs = @()
        if ($c.Count -gt 1) { $cmdArgs += $c[1..($c.Count - 1)] }
        $cmdArgs += @("-c", "import sys; print(sys.executable); print(int(sys.version_info >= (3, 11)))")
        try {
            $out = & $exe @cmdArgs 2>$null
        } catch { continue }
        if ($LASTEXITCODE -ne 0 -or -not $out) { continue }
        $lines = @($out)
        if ($lines.Count -ge 2 -and $lines[1].Trim() -eq "1" -and (Test-Path $lines[0].Trim())) {
            return $lines[0].Trim()
        }
    }
    return $null
}

Write-Host "HVACTrend installer" -ForegroundColor Green
$py = Find-Python
if (-not $py) {
    Write-Host ""
    Write-Host "Python 3.11 or newer was not found." -ForegroundColor Red
    Write-Host "Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'),"
    Write-Host "then run this again."
    exit 1
}
Write-Host "Using Python: $py"

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "Creating the private environment (.venv)..."
    & $py -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create .venv" }
}

Write-Host "Installing HVACTrend and its dependencies (a minute or two)..."
& $venvPy -m pip install --quiet --upgrade pip
$target = if ($WithAI) { ".[ai]" } else { "." }
& $venvPy -m pip install --quiet -e $target
if ($LASTEXITCODE -ne 0) { throw "pip install failed; see the messages above" }
Write-Host "Installed." -ForegroundColor Green

if ($NoWizard) {
    Write-Host ""
    Write-Host "Next: .venv\Scripts\python scripts\configure.py   (writes config\config.yaml)"
    exit 0
}
Write-Host ""
& $venvPy scripts\configure.py
exit $LASTEXITCODE
