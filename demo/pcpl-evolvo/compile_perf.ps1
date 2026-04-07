#!/usr/bin/env pwsh
[CmdletBinding()]
param(
  [string]$Python = "",
  [switch]$Clean,
  [switch]$SkipWarmup,
  [switch]$SkipEvolvo
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Python) {
  $VenvPython = Join-Path $ScriptDir "venv/Scripts/python.exe"
  if (Test-Path $VenvPython) {
    $Python = $VenvPython
  } else {
    $Python = "python"
  }
}

try {
  & $Python --version | Out-Null
} catch {
  throw "[pcpl-evolvo] python not found: $Python"
}

$Cores = [Environment]::ProcessorCount
if ($Cores -lt 1) { $Cores = 1 }

if ($Clean) {
  Write-Host "[pcpl-evolvo] cleaning previous __pycache__ directories"
  Get-ChildItem -Path $ScriptDir -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
}

$Targets = @(
  (Join-Path $ScriptDir "src"),
  (Join-Path $ScriptDir "run_experiments.py"),
  (Join-Path $ScriptDir "config.py")
)
if (-not $SkipEvolvo) {
  $Targets += (Join-Path $ScriptDir "evolvo/src")
}

Write-Host "[pcpl-evolvo] python=$Python"
Write-Host "[pcpl-evolvo] cores=$Cores"
Write-Host "[pcpl-evolvo] compiling optimized bytecode (-O1 and -O2)"

& $Python -m compileall -f -q -j $Cores -o 1 -o 2 @Targets

if (-not $SkipWarmup) {
  Write-Host "[pcpl-evolvo] warming runtime import/cache path"
  $env:PYTHONOPTIMIZE = "2"
  & $Python (Join-Path $ScriptDir "run_experiments.py") --mode dynamic --profile fast --print-effective-config | Out-Null
  Remove-Item Env:PYTHONOPTIMIZE -ErrorAction SilentlyContinue
}

Write-Host "[pcpl-evolvo] compile completed"
