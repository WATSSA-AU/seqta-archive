<#
.SYNOPSIS
  Build the SEQTA Archive database from an export and put it in place (Windows).
.EXAMPLE
  .\windows\import.ps1 -Export "D:\au_wa_example"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Export,
    [switch]$NoCorrFts,
    [switch]$NoRestart
)
$ErrorActionPreference = "Stop"
$App  = Join-Path $env:ProgramFiles "SEQTA Archive"
$Base = Join-Path $env:ProgramData "SEQTA Archive"
$Data = Join-Path $Base "data"
$Exe  = Join-Path $App "venv\Scripts\seqta-archive.exe"
$env:SEQTA_CONFIG = Join-Path $Base "seqta-archive.env"
if (-not (Test-Path $Exe)) { Write-Host "Run windows\install.ps1 first" -ForegroundColor Red; exit 1 }

$new = Join-Path $Data "seqta.db.new"
$buildArgs = @("build", "--export", $Export, "--out", $new)
if ($NoCorrFts) { $buildArgs += "--no-corr-fts" }
& $Exe @buildArgs
if ($LASTEXITCODE -ne 0) { Write-Host "Build failed" -ForegroundColor Red; exit 1 }

# Attachment files: copy from the export the DB was built from
$src = & (Join-Path $App "venv\Scripts\python.exe") -c "import sqlite3,sys,os; c=sqlite3.connect(sys.argv[1]); print(os.path.join(dict(c.execute('select key,value from meta'))['source_export'],'files'))" $new
if (Test-Path $src) {
    Write-Host "Copying attachment files..."
    & robocopy $src (Join-Path $Data "files") /MIR /R:2 /W:2 /NFL /NDL /NJH /NP /XF .DS_Store | Out-Null
    if ($LASTEXITCODE -ge 8) { Write-Host "robocopy failed ($LASTEXITCODE)" -ForegroundColor Red; exit 1 }
}

$task = Get-ScheduledTask -TaskName "SEQTA Archive" -ErrorAction SilentlyContinue
if ($task) { Stop-ScheduledTask -TaskName "SEQTA Archive" -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2 }
$db = Join-Path $Data "seqta.db"
if (Test-Path $db) { Move-Item -Force $db (Join-Path $Data "seqta.db.prev") }
Move-Item -Force $new $db
if ($task -and -not $NoRestart) { Start-ScheduledTask -TaskName "SEQTA Archive" }
Write-Host "Imported. The previous database (if any) is kept as seqta.db.prev - delete it once you're happy."
exit 0
