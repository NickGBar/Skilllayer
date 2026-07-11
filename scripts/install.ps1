param(
    [switch]$CliOnly
)

$ErrorActionPreference = "Stop"

function Fail-Install {
    param([string]$Reason)
    Write-Host ""
    Write-Host "INSTALL FAILED" -ForegroundColor Red
    Write-Host "Reason: $Reason" -ForegroundColor Red
    exit 1
}

function Invoke-RequiredCommand {
    param(
        [string]$Label,
        [string]$Executable,
        [string[]]$Arguments
    )
    Write-Host ""
    Write-Host "== $Label =="
    & $Executable @Arguments
    $ExitCode = $LASTEXITCODE
    if ($ExitCode -ne 0) {
        Fail-Install "$Label failed with exit code $ExitCode"
    }
}

function Get-PythonVersionText {
    param([string]$PythonPath)
    $VersionText = & $PythonPath -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
    $ExitCode = $LASTEXITCODE
    if ($ExitCode -ne 0) {
        Fail-Install "Could not determine Python version from $PythonPath"
    }
    return ($VersionText | Select-Object -First 1).Trim()
}

function Assert-Python310OrNewer {
    param([string]$PythonPath)
    $VersionText = Get-PythonVersionText $PythonPath
    Write-Host "Detected Python: $VersionText"
    $Parts = $VersionText.Split(".")
    if ($Parts.Count -lt 2) {
        Fail-Install "Could not parse Python version: $VersionText"
    }
    $Major = [int]$Parts[0]
    $Minor = [int]$Parts[1]
    if (($Major -lt 3) -or (($Major -eq 3) -and ($Minor -lt 10))) {
        Fail-Install "SkillLayer requires Python >=3.10. Detected Python: $VersionText"
    }
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
$VenvDir = Join-Path $RepoRoot ".venv"
$Python = Join-Path $VenvDir "Scripts\python.exe"

Set-Location $RepoRoot

Write-Host "SkillLayer Windows installer"
Write-Host "  repo: $RepoRoot"
Write-Host "  admin rights: not required"

if (-not (Test-Path "pyproject.toml")) {
    Fail-Install "pyproject.toml not found. Run this script from a SkillLayer checkout."
}

if ((Test-Path $VenvDir) -and -not (Test-Path $VenvDir -PathType Container)) {
    Fail-Install ".venv exists but is not a directory. Refusing to overwrite it."
}

if (-not (Test-Path $VenvDir)) {
    Write-Host "Creating virtual environment at .venv"
    & py -3 -m venv $VenvDir
    $ExitCode = $LASTEXITCODE
    if ($ExitCode -ne 0) {
        Fail-Install "Virtual environment creation failed with exit code $ExitCode"
    }
} else {
    Write-Host "Using existing virtual environment at .venv"
}

if (-not (Test-Path $Python)) {
    Fail-Install "$Python was not found. Recreate .venv with Python >=3.10 and try again."
}

Assert-Python310OrNewer $Python

Invoke-RequiredCommand "Upgrade pip, setuptools, and wheel" $Python @("-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel")

if ($CliOnly) {
    Write-Host "Installing explicit CLI-only mode; MCP integration will not be available." -ForegroundColor Yellow
    Invoke-RequiredCommand "Install SkillLayer CLI-only" $Python @("-m", "pip", "install", ".", "--no-build-isolation")
} else {
    Invoke-RequiredCommand "Install SkillLayer with required MCP runtime extra" $Python @("-m", "pip", "install", ".[mcp]", "--no-build-isolation")
}

Invoke-RequiredCommand "Run installed-runtime doctor" $Python @("-m", "skilllayer", "doctor", "--json")

Write-Host ""
Write-Host "SkillLayer install validation completed." -ForegroundColor Green
Write-Host "Next steps:"
Write-Host "  .venv\Scripts\python.exe -m skilllayer doctor --json"
Write-Host "  .venv\Scripts\python.exe -m skilllayer workflows --json"
Write-Host "  .venv\Scripts\python.exe -m skilllayer mcp-config --output skilllayer-mcp.json"
