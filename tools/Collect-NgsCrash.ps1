param([string]$Root = '')
$ErrorActionPreference = 'Stop'
$env:PSModulePath = Join-Path $PSHOME 'Modules'
# Shipped inside runtime\: the NGS folder is the parent. Also works when copied beside NGS.exe.
if (!$Root) {
    $Root = $PSScriptRoot
    if (!(Test-Path -LiteralPath (Join-Path $Root 'runtime\NGS.exe') -PathType Leaf)) { $Root = Split-Path -Parent $PSScriptRoot }
}
$Root = [IO.Path]::GetFullPath($Root).TrimEnd('\')
if (!(Test-Path -LiteralPath (Join-Path $Root 'runtime\NGS.exe') -PathType Leaf)) {
    throw 'Run Collect-NgsCrash.cmd from the NGS runtime folder or place it beside NGS.exe.'
}
function Assert-PlainPath([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    if (!$current.StartsWith($Root + '\', [StringComparison]::OrdinalIgnoreCase) -and $current -ne $Root) { throw 'Path outside NGS' }
    while ($current.Length -ge $Root.Length) {
        if ((Test-Path -LiteralPath $current) -and ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Redirected path' }
        if ($current -eq $Root) { break }
        $current = [IO.Path]::GetDirectoryName($current)
    }
}
$stage = Join-Path $Root ('data\tmp\crash-collect-' + [Guid]::NewGuid().ToString('N'))
Assert-PlainPath $stage
$null = [IO.Directory]::CreateDirectory($stage)
$copied = New-Object 'Collections.Generic.List[string]'
$notes = New-Object 'Collections.Generic.List[string]'
function Copy-Log([string]$Relative, [long]$Limit = 1048576) {
    $source = Join-Path $Root $Relative
    try {
        Assert-PlainPath $source
        if (!(Test-Path -LiteralPath $source -PathType Leaf)) { return }
        if ((Get-Item -LiteralPath $source).Length -gt $Limit) { $notes.Add('Skipped oversized file: ' + $Relative); return }
        $target = Join-Path $stage $Relative
        $null = [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target))
        Copy-Item -LiteralPath $source -Destination $target
        $copied.Add($Relative)
    } catch { $notes.Add('Could not read: ' + $Relative) }
}
try {
    foreach ($relative in @('runtime\release.json','data\updates\result.json','data\updates\transaction.json',
            'data\updates\first-start.json','data\updates\first-start-ok.json','data\updates\pending.json','data\updates\recovery.json',
            'data\logs\ngs.log','data\logs\ngs.log.1','data\logs\ngs.log.2','data\logs\process-exit.jsonl',
            'data\logs\native-crash.log','data\logs\native-crash.log.1','data\logs\crash-state.json',
            'data\input-host\data\logs\input-host.log','data\input-host\data\logs\input-host.log.1',
            'data\input-host\data\logs\input-host.log.2','data\input-host\data\logs\native-crash.log')) { Copy-Log $relative }
    foreach ($relative in @('data\logs\sessions','data\logs\archives')) {
        $folder = Join-Path $Root $relative
        Assert-PlainPath $folder
        if (Test-Path -LiteralPath $folder -PathType Container) {
            Get-ChildItem -LiteralPath $folder -File | Where-Object {
                $_.Name -match '^[0-9a-f]{32}-[0-9a-f]{32}\.(active|jsonl)$|^[0-9a-f]{32}-[0-9a-f]{64}\.zip$'
            } | Sort-Object LastWriteTime -Descending | Select-Object -First 12 | ForEach-Object {
                Copy-Log (Join-Path $relative $_.Name)
            }
        }
    }
    # Windows Error Reporting minidumps: NGS copies recent ones to data\logs\crashdumps;
    # also take the newest directly from %LOCALAPPDATA%\CrashDumps.
    $dumpTarget = Join-Path $stage 'crashdumps'
    $dumps = @()
    $ownDumps = Join-Path $Root 'data\logs\crashdumps'
    if (Test-Path -LiteralPath $ownDumps -PathType Container) { $dumps += @(Get-ChildItem -LiteralPath $ownDumps -File -Filter *.dmp) }
    if ($env:LOCALAPPDATA) {
        $werDumps = Join-Path $env:LOCALAPPDATA 'CrashDumps'
        if (Test-Path -LiteralPath $werDumps -PathType Container) {
            $dumps += @(Get-ChildItem -LiteralPath $werDumps -File | Where-Object { $_.Name -match '^(NGS|NGSInput)\.exe\.\d+\.dmp$' })
        }
    }
    $taken = @{}
    foreach ($dump in ($dumps | Sort-Object LastWriteTime -Descending)) {
        if ($taken.Count -ge 3) { break }
        if ($taken.ContainsKey($dump.Name)) { continue }
        if ($dump.Length -gt 96MB) { $notes.Add('Skipped oversized dump: ' + $dump.Name); continue }
        $null = [IO.Directory]::CreateDirectory($dumpTarget)
        Copy-Item -LiteralPath $dump.FullName -Destination (Join-Path $dumpTarget $dump.Name)
        $taken[$dump.Name] = $true
        $copied.Add('crashdumps\' + $dump.Name)
    }
    $werKeys = @()
    foreach ($image in @('NGS.exe','NGSInput.exe')) {
        $key = 'HKLM:\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\' + $image
        $werKeys += @{image=$image; registered=(Test-Path -LiteralPath $key)}
    }
    $events = @()
    try {
        $events = @(Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1000,1001,1002,1026; StartTime=(Get-Date).AddDays(-3)} -MaxEvents 1024 -ErrorAction Stop |
            Where-Object { $_.Message -match '(?i)\bNGS(Input)?\.exe\b' } | Select-Object -First 40 |
            ForEach-Object { @{time=$_.TimeCreated.ToString('o'); id=$_.Id; provider=$_.ProviderName; message=$_.Message} })
    } catch { $notes.Add('Windows event query: ' + $_.Exception.Message) }
    ConvertTo-Json -InputObject @($events) -Depth 5 | Set-Content -LiteralPath (Join-Path $stage 'windows-ngs-faults.json') -Encoding UTF8
    $processes = @(Get-Process -Name NGS,NGSInput -ErrorAction SilentlyContinue | ForEach-Object {
        try { @{id=$_.Id; name=$_.ProcessName; path=$_.Path; started=$_.StartTime.ToString('o')} }
        catch { @{id=$_.Id; name=$_.ProcessName} }
    })
    $os = $null
    try { $os = (Get-CimInstance Win32_OperatingSystem | Select-Object -First 1 | ForEach-Object { $_.Caption + ' ' + $_.Version }) } catch { }
    $gpu = @()
    try { $gpu = @(Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name + ' ' + $_.DriverVersion }) } catch { }
    @{collected=(Get-Date).ToString('o'); root=$Root; copied=@($copied); notes=@($notes); processes=@($processes);
      wer_local_dumps=@($werKeys); os=$os; gpu=@($gpu)} |
        ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $stage 'collection.json') -Encoding UTF8
    $archive = Join-Path $Root ('NGS-crash-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0,6) + '.zip')
    Assert-PlainPath $archive
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::CreateFromDirectory($stage, $archive)
    Write-Host ('Created: ' + $archive)
} finally {
    $stageFull = [IO.Path]::GetFullPath($stage)
    $expected = [IO.Path]::GetFullPath((Join-Path $Root 'data\tmp')) + '\crash-collect-'
    if (!$stageFull.StartsWith($expected,[StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid collector cleanup path' }
    Assert-PlainPath $stageFull
    if (Test-Path -LiteralPath $stageFull) { Remove-Item -LiteralPath $stageFull -Recurse -Force }
}
