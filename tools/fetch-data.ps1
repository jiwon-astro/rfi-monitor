[CmdletBinding()]
param(
    [string]$Target = 'jjiwon@10.12.194.1',
    [string]$Key = 'C:/Users/USER/.ssh/obscal_rfi_ed25519',
    [string]$Destination = 'D:/Research/01_ObsCAL_LAMBDA/04_Construction/05_RFI_Survey/data',
    [string]$Run,
    [string]$File,
    [switch]$ListOnly,
    [string]$PythonExe
)
$ErrorActionPreference = 'Stop'
if (-not $PythonExe) {
    $bundledCandidate = Join-Path $env:USERPROFILE 'miniforge3/envs/main/python.exe'
    if (Test-Path -LiteralPath $bundledCandidate) { $PythonExe = $bundledCandidate }
    else { $PythonExe = (Get-Command python -ErrorAction Stop).Source }
}
$fetchArguments = @((Join-Path $PSScriptRoot 'fetch_data.py'), '--target', $Target, '--key', $Key, '--destination', $Destination)
if ($Run) { $fetchArguments += @('--run', $Run) }
if ($File) { $fetchArguments += @('--file', $File) }
if ($ListOnly) { $fetchArguments += '--list-only' }
& $PythonExe @fetchArguments
if ($LASTEXITCODE -ne 0) { throw 'Fetch failed; existing local files were not overwritten. See the error above.' }
