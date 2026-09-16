param(
    [string]$Target = 'jjiwon@10.12.194.1',
    [string]$Key = 'C:/Users/USER/.ssh/obscal_rfi_ed25519'
)
$ErrorActionPreference = 'Stop'
# SSH timing is measured on the host. Select the narrowest of three brackets.
$best = $null
for ($i = 0; $i -lt 3; $i++) {
    $before = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $reply = & ssh -i $Key -o BatchMode=yes -o ConnectTimeout=8 $Target 'python3 /home/jjiwon/owon-rfi/clock_anchor.py'
    $elapsed = $timer.Elapsed.TotalMilliseconds
    $after = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    if ($LASTEXITCODE -ne 0) { throw 'SSH clock query failed' }
    if ([Math]::Abs(($after - $before) - $elapsed) -gt 100) { throw 'Host clock jumped; retry' }
    $pi = $reply | ConvertFrom-Json
    $sample = [pscustomobject]@{
        UtcNs = ([long]($before + [Math]::Floor(($after-$before)/2)) * 1000000)
        UncertaintyNs = ([long][Math]::Ceiling(($after-$before)/2+1) * 1000000)
        MonoNs = [long]$pi.monotonic_ns
        BootId = $pi.boot_id
    }
    if ($null -eq $best -or $sample.UncertaintyNs -lt $best.UncertaintyNs) { $best = $sample }
}
$command = 'python3 /home/jjiwon/owon-rfi/clock_anchor.py --utc-ns {0} --monotonic-ns {1} --boot-id {2} --uncertainty-ns {3}' -f $best.UtcNs,$best.MonoNs,$best.BootId,$best.UncertaintyNs
& ssh -i $Key -o BatchMode=yes $Target $command
if ($LASTEXITCODE -ne 0) { throw 'Clock anchor installation failed' }
Write-Output ('Recorder UTC anchored; initial timing uncertainty <= {0:N3} seconds. OS/RTC time was not changed.' -f ($best.UncertaintyNs/1e9))
