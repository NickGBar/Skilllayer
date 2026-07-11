param([switch]$RemoveVenv, [switch]$RemoveProjectState, [switch]$RemoveUserData)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..")
Write-Host "Disable integration: remove mcpServers.skilllayer from your MCP client configuration."
if ($RemoveVenv) { Remove-Item -Recurse -Force (Join-Path $Root ".venv"); Write-Host "Removed .venv" }
if ($RemoveProjectState) { Remove-Item -Recurse -Force (Join-Path $Root ".skilllayer"); Write-Host "Removed .skilllayer" }
if ($RemoveUserData) { Remove-Item -Recurse -Force (Join-Path $env:LOCALAPPDATA "SkillLayer"); Write-Host "Removed user-level SkillLayer state" }
if (-not $RemoveProjectState) { Write-Host "Project .skilllayer state was retained." }
