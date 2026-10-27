<#
.SYNOPSIS
  Remove the SEQTA Archive. Data, accounts and the audit log are kept unless -RemoveData.
#>
param([switch]$RemoveData)
$ErrorActionPreference = "Continue"
foreach ($t in @("SEQTA Archive", "SEQTA Archive HTTPS")) {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $t
        Unregister-ScheduledTask -TaskName $t -Confirm:$false
    }
}
Get-NetFirewallRule -DisplayName "SEQTA Archive HTTPS" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Remove-Item -Recurse -Force (Join-Path $env:ProgramFiles "SEQTA Archive") -ErrorAction SilentlyContinue
if ($RemoveData) {
    Remove-Item -Recurse -Force (Join-Path $env:ProgramData "SEQTA Archive")
    Write-Host "Removed the app and all data."
} else {
    Write-Host "Removed the app. Data and the audit log remain in $(Join-Path $env:ProgramData 'SEQTA Archive')."
}
