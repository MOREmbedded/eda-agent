# SPDX-License-Identifier: Apache-2.0
param(
    [string]$ScriptProject = (Join-Path $PSScriptRoot 'altium\Altium_API.PrjScr'),
    [string]$Procedure = 'Dispatcher.pas>StartMCPServer',
    [switch]$PrintOnly
)

$ErrorActionPreference = 'Stop'
$resolvedScriptProject = (Resolve-Path -LiteralPath $ScriptProject).Path
if (-not (Test-Path -LiteralPath $resolvedScriptProject -PathType Leaf)) {
    throw 'ScriptProject must name an existing Altium script project.'
}
if ($resolvedScriptProject.Contains('|') -or $Procedure.Contains('|')) {
    throw 'The Altium process parameter delimiter | is not allowed in these values.'
}
$altiumScriptUri = "dxpprocess://ScriptingSystem:RunScript?ProjectName=$resolvedScriptProject|ProcName=$Procedure"
if ($PrintOnly) {
    Write-Output $altiumScriptUri
    return
}

# This invokes the Windows Shell COM object and the Altium URI handler.
# Stop an existing Altium script before running this launcher again.
$altiumComShell = New-Object -ComObject Shell.Application
try {
    $altiumComShell.ShellExecute($altiumScriptUri, '', '', 'open', 0)
}
finally {
    [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($altiumComShell)
}
