# SPDX-License-Identifier: Apache-2.0
<#
.SYNOPSIS
Reflect Altium's SDK interface assembly into JSON.

.DESCRIPTION
Altium.SDK.Interfaces.dll is the machine-readable declaration of every
interface the scripting engine exposes: ISch_*, IPCB_*, IDXP_* and the
EDP/ComponentLib families. Delphi properties cross the COM boundary as
GetState_<Name> / SetState_<Name> methods, so the assembly states, for
every property, its exact type and whether it is readable, writable or
both. That is precisely what the Pascal property chains need, and the
HTML API reference does not list all of it.

The output is DERIVED FROM A NON-REDISTRIBUTABLE ASSEMBLY, so it is
written under reference/, which .gitignore excludes. Nothing generated
from it may be committed verbatim either; the generator emits our own
Pascal and names only members, which are facts about an API.

.PARAMETER Dll
Path to Altium.SDK.Interfaces.dll. Defaults to the AD26 install found
beside the running X2.EXE, then to a scan of the usual install roots.

.PARAMETER Out
Where to write the JSON. Defaults to reference/altium_sdk.json.

.EXAMPLE
pwsh -File scripts/altium_sdk_reflect.ps1
#>
[CmdletBinding()]
param(
    [string] $Dll,
    [string] $Out
)

$ErrorActionPreference = 'Stop'

function Resolve-SdkDll {
    param([string] $Explicit)
    if ($Explicit) {
        if (-not (Test-Path $Explicit)) { throw "no such file: $Explicit" }
        return (Resolve-Path $Explicit).Path
    }
    # The running Altium is the one whose surface we care about: a machine
    # may hold several versions and they do not agree member for member.
    $proc = Get-Process X2 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($proc -and $proc.Path) {
        $cand = Join-Path (Split-Path $proc.Path) 'System\Altium.SDK.Interfaces.dll'
        if (Test-Path $cand) { return $cand }
    }
    foreach ($root in @('C:\Program Files\Altium', 'F:\Program Files\Altium',
                        'D:\Altium', 'C:\Program Files (x86)\Altium')) {
        if (-not (Test-Path $root)) { continue }
        $hit = Get-ChildItem $root -Recurse -Filter 'Altium.SDK.Interfaces.dll' `
                 -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    throw 'Altium.SDK.Interfaces.dll not found. Pass -Dll with its path.'
}

$dllPath = Resolve-SdkDll -Explicit $Dll
Write-Verbose "reflecting $dllPath"

if (-not $Out) {
    $repo = Split-Path (Split-Path $PSCommandPath -Parent) -Parent
    $Out = Join-Path $repo 'reference\altium_sdk.json'
}
$outDir = Split-Path $Out -Parent
if ($outDir -and -not (Test-Path $outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

$asm = [System.Reflection.Assembly]::LoadFrom($dllPath)

# GetTypes() throws when a referenced assembly is absent, and several are:
# the SDK references editor and server assemblies that only load inside a
# running Altium. The exception still carries every type that DID resolve,
# which on AD 26.11 is ~1,946 of them, so the partial result is the result.
try {
    $types = $asm.GetTypes()
    $unresolved = 0
} catch [System.Reflection.ReflectionTypeLoadException] {
    $types = $_.Exception.Types | Where-Object { $_ -ne $null }
    $unresolved = @($_.Exception.LoaderExceptions).Count
}

$fileVer = (Get-Item $dllPath).VersionInfo.FileVersion

$interfaces = [ordered] @{}

$skippedMethods = 0
$skippedTypes = 0

foreach ($t in ($types | Where-Object { $_.IsInterface } | Sort-Object FullName)) {
    $props = [ordered] @{}
    $plain = New-Object System.Collections.ArrayList

    # Windows PowerShell runs on .NET Framework, and a few EDP interfaces
    # carry signatures typed against .NET 8's System.Runtime. Reflecting
    # those members throws FileNotFoundException on the TYPE, not on the
    # assembly, so the only way through is to skip the member and count it.
    # Every ISch_* and IPCB_* member resolves; the losses are elsewhere.
    try { $methods = $t.GetMethods() } catch { $skippedTypes += 1; continue }

    foreach ($m in $methods) {
        $name = $m.Name
        try {
            $pars = @($m.GetParameters())
            $ret  = $m.ReturnType.Name
        } catch {
            $skippedMethods += 1
            continue
        }

        if ($name.StartsWith('GetState_')) {
            $key = $name.Substring(9)
            if (-not $props.Contains($key)) {
                $props[$key] = [ordered] @{ name = $key }
            }
            $props[$key]['get'] = $ret
            # An indexed read takes its index as the only argument.
            if ($pars.Count -gt 0) {
                $props[$key]['get_index'] = @($pars | ForEach-Object { $_.ParameterType.Name })
            }
            continue
        }
        if ($name.StartsWith('SetState_')) {
            $key = $name.Substring(9)
            if (-not $props.Contains($key)) {
                $props[$key] = [ordered] @{ name = $key }
            }
            # The written type is the LAST argument; anything before it
            # indexes the property (SetState_AliasAt(Int32, String)).
            if ($pars.Count -gt 0) {
                $props[$key]['set'] = $pars[$pars.Count - 1].ParameterType.Name
                if ($pars.Count -gt 1) {
                    $props[$key]['set_index'] =
                        @($pars[0..($pars.Count - 2)] | ForEach-Object { $_.ParameterType.Name })
                }
            } else {
                $props[$key]['set'] = 'Void'
            }
            continue
        }

        [void] $plain.Add([ordered] @{
            name   = $name
            ret    = $ret
            params = @($pars | ForEach-Object {
                [ordered] @{ name = $_.Name; type = $_.ParameterType.Name }
            })
        })
    }

    try {
        $inherits = @($t.GetInterfaces() | ForEach-Object { $_.FullName } | Sort-Object)
    } catch { $inherits = @() }

    $interfaces[$t.FullName] = [ordered] @{
        name       = $t.Name
        namespace  = $t.Namespace
        inherits   = $inherits
        properties = $props
        methods    = @($plain | Sort-Object { $_.name })
    }
}

$doc = [ordered] @{
    schema     = 1
    source     = $dllPath
    assembly   = $asm.FullName
    file_version = $fileVer
    reflected_at = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss')
    unresolved_types = $unresolved
    skipped_types = $skippedTypes
    skipped_methods = $skippedMethods
    interfaces = $interfaces
}

# -Depth 10 because properties nest four deep and the default of 2 silently
# replaces the rest with type names.
$doc | ConvertTo-Json -Depth 10 -Compress | Out-File -FilePath $Out -Encoding utf8

$n = @($interfaces.Keys).Count
$p = ($interfaces.Values | ForEach-Object { @($_.properties.Keys).Count } | Measure-Object -Sum).Sum
Write-Output "wrote $Out"
Write-Output "  interfaces: $n"
Write-Output "  properties: $p"
Write-Output "  assembly:   $fileVer"
if ($skippedMethods -or $skippedTypes -or $unresolved) {
    Write-Output "  not reflected: $unresolved types unresolved, $skippedTypes types and $skippedMethods methods skipped"
}
