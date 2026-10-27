<#
.SYNOPSIS
  Install or update the SEQTA Archive on Windows (Server 2016+ / Windows 10+).

.DESCRIPTION
  - Installs Python 3.12 with winget if no Python 3.10+ is found.
  - Installs the app into "C:\Program Files\SEQTA Archive".
  - Keeps data, accounts and the audit log in "C:\ProgramData\SEQTA Archive",
    locked down to Administrators and SYSTEM.
  - Runs the archive at startup as a scheduled task (no extra service software).
  - Optionally imports your SEQTA export and creates the first administrator.
  - Optionally puts HTTPS in front with Caddy (-Https).

  Safe to re-run: updates the code and keeps your settings, data and accounts.

.EXAMPLE
  # From an elevated PowerShell in the unzipped repository folder:
  powershell -ExecutionPolicy Bypass -File .\windows\install.ps1 -SchoolName "Example SHS" -Export "D:\au_wa_example"

.EXAMPLE
  # Staff browse to https://archive.example.school (certificate from your school):
  .\windows\install.ps1 -Https -SiteAddress archive.example.school -CertFile C:\certs\archive.pem -KeyFile C:\certs\archive.key
#>
[CmdletBinding()]
param(
    [string]$SchoolName,
    [string]$SchoolShort = "",
    [string]$Export,
    [int]$Port = 8080,
    [switch]$Https,
    [string]$SiteAddress = "",
    [string]$CertFile = "",
    [string]$KeyFile = "",
    [string]$AdminUser = "",
    [string]$AdminPassword = "",     # for unattended installs/CI only; otherwise you're prompted
    [switch]$NoCorrFts,
    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Say($m) { Write-Host ""; Write-Host "== $m" -ForegroundColor Cyan }
function Die($m) { Write-Host "!! $m" -ForegroundColor Red; exit 1 }

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Die "Run this from an elevated (Run as administrator) PowerShell."
}

$Src   = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$App   = Join-Path $env:ProgramFiles "SEQTA Archive"
$Base  = Join-Path $env:ProgramData "SEQTA Archive"
$Data  = Join-Path $Base "data"
$State = Join-Path $Base "state"
$Conf  = Join-Path $Base "seqta-archive.env"
$Exe   = Join-Path $App "venv\Scripts\seqta-archive.exe"
$TaskName = "SEQTA Archive"

# ---------------------------------------------------------------- Python
Say "Python"
function Find-Python {
    foreach ($cmd in @(@("py", "-3.12"), @("py", "-3.13"), @("py", "-3.11"), @("py", "-3.10"), @("py", "-3"), @("python"))) {
        $exe = $cmd[0]; $args_ = @(); if ($cmd.Length -gt 1) { $args_ = @($cmd[1]) }
        try {
            $v = & $exe @args_ -c "import sys; print('%d.%d' % sys.version_info[:2]); print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $v) {
                $parts = $v[0].Split("."); $path = $v[1]
                if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 10 -and $path -notmatch "WindowsApps") { return $path }
            }
        } catch { }
    }
    return $null
}
$Python = Find-Python
if (-not $Python) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Die "Python 3.10+ not found and winget isn't available. Install Python 3.12 for all users from python.org (tick 'Add to PATH'), then re-run."
    }
    Write-Host "   installing Python 3.12 with winget..."
    winget install -e --id Python.Python.3.12 --scope machine --silent --accept-package-agreements --accept-source-agreements | Out-Null
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    $Python = Find-Python
    if (-not $Python) { Die "Python install didn't take. Install Python 3.12 from python.org for all users, then re-run." }
}
Write-Host "   using $Python"

# ---------------------------------------------------------------- folders + ACLs
Say "Folders"
foreach ($d in @($App, $Base, $Data, $State)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
# Student records: Administrators and SYSTEM only.
& icacls $Base /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" /T /Q | Out-Null
if ($LASTEXITCODE -ne 0) { Die "couldn't restrict permissions on $Base (icacls exit $LASTEXITCODE)" }
Write-Host "   $Base is restricted to Administrators and SYSTEM"

# ---------------------------------------------------------------- app
Say "Application"
if (-not (Test-Path (Join-Path $App "venv\Scripts\python.exe"))) {
    & $Python -m venv (Join-Path $App "venv")
    if ($LASTEXITCODE -ne 0) { Die "couldn't create the virtual environment" }
}
$VPy = Join-Path $App "venv\Scripts\python.exe"
& $VPy -m pip install --quiet --upgrade pip
& $VPy -m pip install --quiet --upgrade "$Src"
if ($LASTEXITCODE -ne 0) { Die "pip install failed (is this server allowed to reach pypi.org?)" }

# ---------------------------------------------------------------- config
Say "Settings ($Conf)"
$Listen = "127.0.0.1"
if (-not (Test-Path $Conf)) {
    if (-not $SchoolName) {
        if ($NonInteractive) { $SchoolName = "Our school" } else { $SchoolName = Read-Host "School name (shown on every page)" }
    }
    $cookieSecure = "1"
    @"
# SEQTA Archive settings. After editing: Stop-ScheduledTask "$TaskName"; Start-ScheduledTask "$TaskName"
SCHOOL_NAME=$SchoolName
SCHOOL_SHORT=$SchoolShort
TIMEZONE=Australia/Perth
SEQTA_DATA_DIR=$Data
SEQTA_STATE_DIR=$State
HOST=$Listen
PORT=$Port
AUTH_MODE=local
REQUIRE_TOTP=1
SESSION_HOURS=8
COOKIE_SECURE=$cookieSecure
"@ | Set-Content -Path $Conf -Encoding UTF8
    Write-Host "   created"
} else {
    Write-Host "   kept existing"
}
$env:SEQTA_CONFIG = $Conf

# ---------------------------------------------------------------- import
if ($Export) {
    Say "Importing $Export (10-30 minutes for a large school)"
    & (Join-Path $PSScriptRoot "import.ps1") -Export $Export -NoRestart -NoCorrFts:$NoCorrFts
    if ($LASTEXITCODE -ne 0) { Die "import failed" }
}

# ---------------------------------------------------------------- scheduled task
Say "Startup task '$TaskName'"
$action = New-ScheduledTaskAction -Execute $Exe -Argument "serve" -WorkingDirectory $Base
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$taskPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $taskPrincipal -Description "SEQTA Archive web app (read-only student records)" | Out-Null

if (Test-Path (Join-Path $Data "seqta.db")) {
    Start-ScheduledTask -TaskName $TaskName
    $ok = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        try { Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$Port/healthz" -TimeoutSec 3 | Out-Null; $ok = $true; break } catch { }
    }
    if ($ok) { Write-Host "   running on http://127.0.0.1:$Port/" } else { Write-Host "   !! didn't answer on port $Port yet - check: & '$Exe' check" -ForegroundColor Yellow }
} else {
    Write-Host "   no database yet - run windows\import.ps1 -Export <path to au_wa_yourschool>"
}

# ---------------------------------------------------------------- first admin
& $Exe user list | Out-String | Tee-Object -Variable users | Out-Null
if ($users -match "No users yet") {
    Say "First administrator"
    if (-not $AdminUser) {
        if ($NonInteractive) { $AdminUser = "admin" } else { $AdminUser = Read-Host "Username for the first administrator" }
    }
    if ($AdminPassword) {
        $AdminPassword | & $Exe user add $AdminUser --admin --password-stdin
    } elseif ($NonInteractive) {
        & $Exe user add $AdminUser --admin --random
    } else {
        & $Exe user add $AdminUser --admin
    }
}

# ---------------------------------------------------------------- HTTPS (optional)
if ($Https) {
    Say "HTTPS with Caddy"
    if (-not $SiteAddress) { Die "-Https needs -SiteAddress (the name staff will browse to)" }
    $CaddyDir = Join-Path $App "caddy"
    $CaddyExe = Join-Path $CaddyDir "caddy.exe"
    New-Item -ItemType Directory -Force -Path $CaddyDir | Out-Null
    if (-not (Test-Path $CaddyExe)) {
        $zip = Join-Path $env:TEMP "caddy.zip"
        Invoke-WebRequest -UseBasicParsing "https://github.com/caddyserver/caddy/releases/download/v2.8.4/caddy_2.8.4_windows_amd64.zip" -OutFile $zip
        # SHA-512 from caddy_2.8.4_checksums.txt on the official release
        $expected = "89f8fc9ece9941a15a0981b3c69543d3b9b5fe095e747875a05fc1775d4d78d4505a7fe54a58d496dade601e85f6053a00a1b0382a781d3e8b6eec044384f6e6"
        $actual = (Get-FileHash -Algorithm SHA512 $zip).Hash.ToLower()
        if ($actual -ne $expected) { Remove-Item $zip; Die "Caddy download failed its checksum - not installing it" }
        Expand-Archive -Path $zip -DestinationPath $CaddyDir -Force
        Remove-Item $zip
    }
    if ($CertFile -and $KeyFile) { $tls = "tls `"$CertFile`" `"$KeyFile`"" } else { $tls = "tls internal" }
    @"
{
	admin off
}

$SiteAddress {
	$tls
	encode gzip
	reverse_proxy 127.0.0.1:$Port {
		transport http {
			response_header_timeout 300s
		}
	}
}
"@ | Set-Content -Path (Join-Path $CaddyDir "Caddyfile") -Encoding UTF8
    $cAction = New-ScheduledTaskAction -Execute $CaddyExe -Argument "run --config Caddyfile --adapter caddyfile" -WorkingDirectory $CaddyDir
    if (Get-ScheduledTask -TaskName "$TaskName HTTPS" -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName "$TaskName HTTPS" -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName "$TaskName HTTPS" -Confirm:$false
    }
    Register-ScheduledTask -TaskName "$TaskName HTTPS" -Action $cAction -Trigger $trigger -Settings $settings `
        -Principal $taskPrincipal -Description "HTTPS front end for the SEQTA Archive" | Out-Null
    Start-ScheduledTask -TaskName "$TaskName HTTPS"
    if (-not (Get-NetFirewallRule -DisplayName "SEQTA Archive HTTPS" -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName "SEQTA Archive HTTPS" -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow -Profile Domain,Private | Out-Null
    }
    Write-Host "   staff browse to https://$SiteAddress/"
    if ($tls -eq "tls internal") {
        Write-Host "   Using Caddy's own certificate authority: browsers will warn until its root certificate" -ForegroundColor Yellow
        Write-Host "   is trusted. Its root is C:\Windows\System32\config\systemprofile\AppData\Roaming\Caddy\pki\authorities\local\root.crt" -ForegroundColor Yellow
        Write-Host "   and deploy via Group Policy, or re-run with -CertFile/-KeyFile for a school certificate." -ForegroundColor Yellow
    }
}

Say "Done"
& $Exe check
Write-Host ""
if (-not $Https) {
    Write-Host "The archive listens on http://127.0.0.1:$Port/ (this server only)."
    Write-Host "For staff access over the network, re-run with -Https -SiteAddress <name>, or publish it through"
    Write-Host "your existing HTTPS reverse proxy. Don't expose it over plain HTTP."
}
