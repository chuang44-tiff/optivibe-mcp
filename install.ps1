<#
.SYNOPSIS
  One-command install of optivibe-mcp from a clone of the repository.

.DESCRIPTION
  Run from the repository root:

      powershell -ExecutionPolicy Bypass -File install.ps1

  It does what the README's Install and Build-the-reference-data sections describe, in order:

    1. creates a virtual environment in .venv, from the Python 3.11+ on PATH or the one
       given by -Python;
    2. installs optivibe-reference (with the manual extra) and optivibe-harness, editable;
    3. with -Probe, runs the two operand probes. Each opens a headless OpticStudio session
       and needs a ZOS-API licence. Without -Probe no OpticStudio session is opened;
    4. builds the reference data from your own OpticStudio install. A step whose input is
       missing is reported as skipped;
    5. runs the doctor (python -m optivibe_doctor) and shows its report;
    6. prints the command that registers the server with Claude Code. With -Register it
       runs that command.

  Nothing is downloaded except the Python dependencies pip resolves. No vendor data leaves
  your machine.

.PARAMETER Python
  The Python 3.11+ interpreter to create the virtual environment from. Use it when the
  python on PATH is older.

.PARAMETER NoVenv
  Install straight into the interpreter given by -Python instead of creating .venv.

.PARAMETER Probe
  Also run probe_operands.py and probe_tolerances.py (opens OpticStudio twice).

.PARAMETER Register
  Run "claude mcp add optivibe ..." at the end. Needs the claude command on PATH.

.PARAMETER SkipData
  Do not build the reference data.
#>
[CmdletBinding()]
param(
    [string]$Python = "",
    [switch]$NoVenv,
    [switch]$Probe,
    [switch]$Register,
    [switch]$SkipData
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$harness = Join-Path $root "packages\optivibe-harness"
$reference = Join-Path $root "packages\optivibe-reference"

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }

function Invoke-Checked($exe, $arguments, $what) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)." }
}

function Test-Python311($exe) {
    try {
        & $exe -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

if (-not (Test-Path (Join-Path $harness "pyproject.toml"))) {
    throw "Run this script from a clone of optivibe-mcp: $harness\pyproject.toml was not found."
}

# 1. interpreter
$base = $null
if ($Python) {
    if (-not (Test-Python311 $Python)) { throw "$Python is not a working Python 3.11 or newer." }
    $base = $Python
}
if ($NoVenv) {
    if (-not $base) { throw "-NoVenv needs -Python <path to python.exe>: the interpreter to install into." }
    $py = $base
    Step "Installing into the interpreter $py (no virtual environment)"
} else {
    $venv = Join-Path $root ".venv"
    $py = Join-Path $venv "Scripts\python.exe"
    if (Test-Path $py) {
        Step "Reusing the virtual environment in .venv"
    } else {
        if (-not $base) {
            foreach ($candidate in @("python", "py")) {
                if (Get-Command $candidate -ErrorAction SilentlyContinue) {
                    if (Test-Python311 $candidate) { $base = $candidate; break }
                }
            }
        }
        if (-not $base) {
            throw "No Python 3.11 or newer was found on PATH. Install one, or pass -Python <path to python.exe>."
        }
        Step "Creating a virtual environment in .venv"
        Invoke-Checked $base @("-m", "venv", $venv) "Creating the virtual environment"
    }
}
$py = (& $py -c "import sys; print(sys.executable)").Trim()

# 2. packages -- the reference layer first: the harness loads it in-process.
Step "Installing optivibe-reference and optivibe-harness"
Invoke-Checked $py @("-m", "pip", "install", "--upgrade", "pip", "--quiet") "Upgrading pip"
Invoke-Checked $py @("-m", "pip", "install", "-e", "$reference[manual]") "Installing optivibe-reference"
Invoke-Checked $py @("-m", "pip", "install", "-e", $harness) "Installing optivibe-harness"

# 3 and 4. reference data
$dataFailed = $false
if (-not $SkipData) {
    if ($Probe) {
        Step "Probing the operand and tolerance surface (opens OpticStudio twice)"
        Invoke-Checked $py @((Join-Path $reference "scripts\probe_operands.py")) "probe_operands.py"
        Invoke-Checked $py @((Join-Path $reference "scripts\probe_tolerances.py")) "probe_tolerances.py"
    }
    Step "Building the reference data from your OpticStudio install"
    & $py (Join-Path $reference "scripts\build_vendor_data.py")
    if ($LASTEXITCODE -ne 0) {
        $dataFailed = $true
        Write-Host "build_vendor_data.py reported $LASTEXITCODE failed step(s); see its output above." -ForegroundColor Yellow
    }
    if (-not $Probe) {
        Write-Host ""
        Write-Host "The operand and tolerance catalogs need one live probe of your install."
        Write-Host "When OpticStudio is free, run this script again with -Probe."
    }
}

# 5. doctor
Step "Checking the install (python -m optivibe_doctor)"
& $py -m optivibe_doctor
$doctorExit = $LASTEXITCODE

# 6. registration
$registerArgs = @("mcp", "add", "optivibe", "--", $py, "-m", "optivibe_harness")
Step "Register with Claude Code"
if ($Register) {
    if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
        throw "-Register was given but the claude command is not on PATH."
    }
    Invoke-Checked "claude" $registerArgs "claude mcp add"
} else {
    Write-Host "Run this once (or run the script again with -Register):"
    Write-Host ""
    Write-Host "    claude mcp add optivibe -- `"$py`" -m optivibe_harness"
}

Write-Host ""
if ($doctorExit -eq 0 -and -not $dataFailed) {
    Write-Host "Install complete: every doctor check passed." -ForegroundColor Green
    exit 0
}
Write-Host "Installed, but the doctor reported something to look at (exit code $doctorExit). Its report above names each check and its remedy." -ForegroundColor Yellow
exit 1
