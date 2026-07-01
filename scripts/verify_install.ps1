$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Failures = @()

Set-Location $RepoRoot

if (-not (Test-Path $Python)) {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $PythonCommand) {
        Write-Host "Verification failed" -ForegroundColor Red
        Write-Host "Reason: No .venv Python found and python is not on PATH. Run scripts\install.ps1 first." -ForegroundColor Red
        exit 1
    }
    $Python = $PythonCommand.Source
}

Write-Host "SkillLayer install verification"
Write-Host "  repo: $RepoRoot"
Write-Host "  python: $Python"

function Run-Check {
    param(
        [string]$Label,
        [string[]]$Arguments,
        [bool]$Required = $true
    )
    Write-Host ""
    Write-Host "== $Label =="
    & $Python @Arguments
    $ExitCode = $LASTEXITCODE
    if ($ExitCode -ne 0) {
        $Message = "$Label failed with exit code $ExitCode"
        if ($Required) {
            $script:Failures += $Message
            Write-Warning $Message
        } else {
            Write-Warning "$Message (optional)"
        }
    }
}

Run-Check "tester-check" @("-m", "skilllayer", "tester-check") $true
Run-Check "workflows" @("-m", "skilllayer", "workflows") $true
Run-Check "doctor" @("-m", "skilllayer", "doctor") $true

Write-Host ""
if ($Failures.Count -gt 0) {
    Write-Host "Verification failed" -ForegroundColor Red
    Write-Host "Reasons:" -ForegroundColor Red
    foreach ($Failure in $Failures) {
        Write-Host "  - $Failure" -ForegroundColor Red
    }
    exit 1
}

Write-Host "Verification passed." -ForegroundColor Green
