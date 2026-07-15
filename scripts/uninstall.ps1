param([switch]$RemoveVenv, [switch]$RemoveProjectState, [switch]$RemoveUserData, [switch]$DryRun, [switch]$Confirm)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..")
Write-Host "Disable integration: remove mcpServers.skilllayer from your MCP client configuration."
Write-Host "Project .skilllayer state is retained by default."
if ($DryRun) { Write-Host "Dry run: no files changed."; exit 0 }
if (($RemoveVenv -or $RemoveProjectState -or $RemoveUserData) -and -not $Confirm) { throw "Refusing destructive removal without -Confirm." }
if ($RemoveVenv) {
  $Venv = Join-Path $Root ".venv"
  if (-not (Test-Path (Join-Path $Venv "pyvenv.cfg"))) { throw "Refusing: .venv is not a recognizable environment." }
  Remove-Item -Recurse -Force $Venv; Write-Host "Removed .venv"
}
if ($RemoveProjectState) { Remove-Item -Recurse -Force (Join-Path $Root ".skilllayer"); Write-Host "Removed .skilllayer" }
if ($RemoveUserData) { Remove-Item -Recurse -Force (Join-Path $env:LOCALAPPDATA "SkillLayer"); Write-Host "Removed user-level SkillLayer state" }
