param([Parameter(Mandatory=$true)][string]$Root, [int]$WaitPid=0, [int]$ParentPid=0, [switch]$Recover, [switch]$NoRestart)
$ErrorActionPreference = 'Stop'
$Root = [IO.Path]::GetFullPath($Root).TrimEnd('\')
$env:PSModuleAnalysisCachePath = [IO.Path]::Combine($Root, 'data\cache\powershell\ModuleAnalysisCache')
$null = [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($env:PSModuleAnalysisCachePath))
$env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules')
$Base = Join-Path $Root 'data\updates'
if ([IO.Path]::GetFullPath($PSScriptRoot) -ne $Base) { throw 'Updater must run inside its portable folder' }

function Assert-Safe([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if (!$full.StartsWith($Root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Path outside NGS' }
    $current = $full
    while ($current.Length -gt $Root.Length) {
        if (Test-Path -LiteralPath $current) {
            if ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Redirected updater path' }
        }
        $current = [IO.Path]::GetDirectoryName($current)
    }
}
function Write-Json([string]$Path, $Value) {
    Assert-Safe $Path
    $temporary = $Path + '.new'
    Assert-Safe $temporary
    [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Compress -Depth 10), (New-Object Text.UTF8Encoding($false)))
    if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($temporary, $Path, [NullString]::Value) }
    else { [IO.File]::Move($temporary, $Path) }
}
function Restore-Previous {
    foreach ($path in @('backup\runtime', 'backup\NGS.exe', 'restore.exe', 'transaction.json', 'first-start.json')) {
        Assert-Safe (Join-Path $Base $path)
    }
    if (!(Test-Path -LiteralPath "$Base\backup\NGS.exe" -PathType Leaf)) { throw 'Recovery has no previous launcher backup' }
    if ((Test-Path -LiteralPath "$Base\backup\runtime") -and !(Test-Path -LiteralPath "$Base\backup\runtime" -PathType Container)) {
        throw 'Recovery runtime backup is not a directory'
    }
    if (!(Test-Path -LiteralPath "$Base\backup\runtime") -and !(Test-Path -LiteralPath "$Root\runtime" -PathType Container)) {
        throw 'Recovery has no previous or partially restored runtime'
    }
    if ((Test-Path -LiteralPath "$Base\first-start.json") -and !(Test-Path -LiteralPath "$Base\transaction.json")) {
        if (!(Test-Path -LiteralPath "$Base\backup\runtime") -or !(Test-Path -LiteralPath "$Base\backup\NGS.exe")) {
            throw 'The unconfirmed first start has no complete recovery backup'
        }
        # Once recovery begins, a partially restored runtime is an interrupted
        # transaction. The next recovery can finish restoring the outer EXE even
        # when backup/runtime has already been moved back successfully.
        Write-Json "$Base\transaction.json" @{schema=1; recovery=$true}
    }
    if (Test-Path -LiteralPath "$Base\backup\runtime") {
        if (Test-Path -LiteralPath "$Root\runtime") {
            Assert-Safe "$Base\failed-runtime"
            if (Test-Path -LiteralPath "$Base\failed-runtime") { throw 'Failed runtime already exists; preserving both copies' }
            Move-Item -LiteralPath "$Root\runtime" -Destination "$Base\failed-runtime"
        }
        Move-Item -LiteralPath "$Base\backup\runtime" -Destination "$Root\runtime"
    }
    if (Test-Path -LiteralPath "$Base\backup\NGS.exe") {
        Copy-Item -LiteralPath "$Base\backup\NGS.exe" -Destination "$Base\restore.exe"
        if (Test-Path -LiteralPath "$Root\NGS.exe") { [IO.File]::Replace("$Base\restore.exe", "$Root\NGS.exe", [NullString]::Value) }
        else { [IO.File]::Move("$Base\restore.exe", "$Root\NGS.exe") }
    }
    # Clear the recovery journal last so an interrupted cleanup remains resumable.
    foreach ($marker in @('first-start.json', 'first-start-ok.json', 'transaction.json')) {
        $path = Join-Path $Base $marker
        Assert-Safe $path
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path }
    }
}

$lock = $null; $queue = $null; $restart = $false; $replacementStarted = $false
try {
    foreach ($id in @($WaitPid, $ParentPid)) {
        if ($id -gt 0 -and $id -ne $PID) {
            $old = Get-Process -Id $id -ErrorAction SilentlyContinue
            if ($old -and !$old.WaitForExit(15000)) {
                Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
                $null = $old.WaitForExit(5000)
            }
        }
    }
    $runtimePrefix = [IO.Path]::GetFullPath("$Root\runtime\")
    $bootstrapExe = [IO.Path]::GetFullPath("$Root\NGS.exe")
    foreach ($proc in @(Get-Process -Name 'NGS','NGSInput' -ErrorAction SilentlyContinue)) {
        if ($proc.Id -eq $PID) { continue }
        $procPath = $null
        try { $procPath = [IO.Path]::GetFullPath($proc.Path) } catch {}
        if ($procPath -and ($procPath.StartsWith($runtimePrefix, [StringComparison]::OrdinalIgnoreCase) -or $procPath.Equals($bootstrapExe, [StringComparison]::OrdinalIgnoreCase))) {
            if (!$proc.WaitForExit(3000)) {
                Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
                $null = $proc.WaitForExit(3000)
            }
        }
    }
    foreach ($path in @('update.lock','pending.json','package.zip','prepared','backup','failed-runtime','transaction.json','first-start.json','first-start-ok.json','owner.json')) {
        Assert-Safe (Join-Path $Base $path)
    }
    Assert-Safe "$Root\runtime"; Assert-Safe "$Root\NGS.exe"; Assert-Safe "$Root\data\state\queue.lock"
    $lock = [IO.File]::Open("$Base\update.lock", 'OpenOrCreate', 'ReadWrite', 'None')
    $queue = [IO.File]::Open("$Root\data\state\queue.lock", 'OpenOrCreate', 'ReadWrite', 'None')
    $interrupted = Test-Path -LiteralPath "$Base\transaction.json"
    $firstStartPending = Test-Path -LiteralPath "$Base\first-start.json"
    if ($interrupted -or ($Recover -and $firstStartPending)) {
        Restore-Previous
        $reason = if ($firstStartPending) { 'The new version exited before its ordinary startup was confirmed. The previous version was restored.' }
                  else { 'Interrupted update restored the previous version.' }
        Write-Json "$Base\result.json" @{status='rolled_back'; message=$reason}
        $restart = $true
    } elseif ($firstStartPending) {
        throw 'The previous update still awaits its first ordinary startup'
    } elseif (!$Recover) {
        $envelope = Get-Content -LiteralPath "$Base\pending.json" -Raw -Encoding UTF8 | ConvertFrom-Json
        $keyBytes = [IO.File]::ReadAllBytes("$Root\runtime\assets\update-public-key.bin")
        $reader = New-Object IO.BinaryReader((New-Object IO.MemoryStream(,$keyBytes)))
        if ($reader.ReadUInt32() -ne 0x31415352 -or $reader.ReadUInt32() -ne 2048) { throw 'Invalid pinned RSA key' }
        $exponentLength = $reader.ReadUInt32(); $modulusLength = $reader.ReadUInt32()
        $null = $reader.ReadUInt32(); $null = $reader.ReadUInt32()
        $parameters = New-Object Security.Cryptography.RSAParameters
        $parameters.Exponent = $reader.ReadBytes($exponentLength); $parameters.Modulus = $reader.ReadBytes($modulusLength)
        $rsa = [Security.Cryptography.RSA]::Create(); $rsa.ImportParameters($parameters)
        try {
            if (!$rsa.VerifyData([Text.Encoding]::UTF8.GetBytes($envelope.payload), [Convert]::FromBase64String($envelope.signature),
                    [Security.Cryptography.HashAlgorithmName]::SHA256, [Security.Cryptography.RSASignaturePadding]::Pkcs1)) {
                throw 'Invalid update signature'
            }
        } finally { $rsa.Dispose(); $reader.Dispose() }
        $release = $envelope.payload | ConvertFrom-Json
        $hash = [Security.Cryptography.SHA256]::Create(); $package = [IO.File]::OpenRead("$Base\package.zip")
        try { $digest = [BitConverter]::ToString($hash.ComputeHash($package)).Replace('-', '').ToLowerInvariant() }
        finally { $package.Dispose(); $hash.Dispose() }
        if ($digest -ne $release.sha256) { throw 'Update package SHA256 mismatch' }
        if ((Get-Item -LiteralPath "$Base\package.zip").Length -ne $release.size) { throw 'Update package size mismatch' }
        $preparedVersion = (Get-Content -LiteralPath "$Base\prepared\runtime\release.json" -Raw -Encoding UTF8 | ConvertFrom-Json).version
        if ($preparedVersion -ne $release.version) { throw 'Prepared version mismatch' }
        foreach ($entry in Get-ChildItem -LiteralPath "$Base\prepared" -Recurse -Force) { Assert-Safe $entry.FullName }
        if (Test-Path -LiteralPath "$Base\backup") { throw 'Unexpected existing backup' }
        $null = [IO.Directory]::CreateDirectory("$Base\backup")
        Copy-Item -LiteralPath "$Root\NGS.exe" -Destination "$Base\backup\NGS.exe"
        Write-Json "$Base\transaction.json" @{schema=1; version=$release.version}
        $replacementStarted = $true
        [IO.File]::Replace("$Base\prepared\NGS.exe", "$Root\NGS.exe", [NullString]::Value)
        Move-Item -LiteralPath "$Root\runtime" -Destination "$Base\backup\runtime"
        Move-Item -LiteralPath "$Base\prepared\runtime" -Destination "$Root\runtime"
        $info = New-Object Diagnostics.ProcessStartInfo
        $info.FileName = "$Root\runtime\NGS.exe"; $info.Arguments = '--update-probe'
        $info.WorkingDirectory = $Root; $info.UseShellExecute = $false; $info.CreateNoWindow = $true
        $probe = [Diagnostics.Process]::Start($info)
        if (!$probe.WaitForExit(45000)) { $probe.Kill(); $probe.WaitForExit(); throw 'New version startup timed out' }
        if ($probe.ExitCode -ne 0) { throw 'New version startup check failed' }
        # The hidden probe checks imports only. The ordinary rendered launch owns
        # the health acknowledgement; the bootstrap recovers an early exit.
        if (Test-Path -LiteralPath "$Base\first-start-ok.json") { Remove-Item -LiteralPath "$Base\first-start-ok.json" }
        Write-Json "$Base\first-start.json" @{schema=1; version=$release.version; token=[Guid]::NewGuid().ToString('N')}
        Write-Json "$Base\result.json" @{status='pending_first_start'; version=$release.version; message='Files installed; waiting for the first ordinary startup.'}
        Remove-Item -LiteralPath "$Base\transaction.json"
        Remove-Item -LiteralPath "$Base\package.zip"
        $restart = $true
    } else { throw 'There is no interrupted update to recover' }
} catch {
    $message = $_.Exception.Message
    if ($queue -and (Test-Path -LiteralPath "$Base\transaction.json")) {
        try { Restore-Previous; $restart = $true } catch { $message += '; rollback failed: ' + $_.Exception.Message }
    } elseif ($queue -and !$Recover -and $replacementStarted -and (Test-Path -LiteralPath "$Base\first-start.json")) {
        try { Restore-Previous; $restart = $true } catch { $message += '; rollback failed: ' + $_.Exception.Message }
    } elseif ($queue -and !$Recover -and !(Test-Path -LiteralPath "$Base\first-start.json") -and (Test-Path -LiteralPath "$Root\runtime\NGS.exe")) {
        $restart = $true
    }
    # Failed recovery must not relaunch the same unconfirmed runtime in a loop.
    Write-Json "$Base\result.json" @{status='error'; message=$message}
} finally {
    if ($queue) { $queue.Dispose() }
    if ($lock) { $lock.Dispose() }
}
if ($restart -and !$NoRestart -and !$env:NGS_UPDATE_TEST_NO_RESTART) {
    try {
        Add-Type 'public static class NgsForeground { [System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool AllowSetForegroundWindow(uint processId); }'
    } catch { Write-Warning "Could not prepare foreground permission: $_" }
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = "$Root\NGS.exe"
    $start.WorkingDirectory = $Root
    $start.UseShellExecute = $false
    $start.WindowStyle = [Diagnostics.ProcessWindowStyle]::Normal
    try {
        $launcher = [Diagnostics.Process]::Start($start)
        if ('NgsForeground' -as [type]) { $null = [NgsForeground]::AllowSetForegroundWindow($launcher.Id) }
    } catch {
        # A damaged outer launcher may not start at all, so it cannot recover
        # itself. No new app exists here; restore under the same exclusive locks.
        $message = 'Could not start the updated launcher: ' + $_.Exception.Message
        $lock = $null; $queue = $null; $restart = $false
        try {
            $lock = [IO.File]::Open("$Base\update.lock", 'OpenOrCreate', 'ReadWrite', 'None')
            $queue = [IO.File]::Open("$Root\data\state\queue.lock", 'OpenOrCreate', 'ReadWrite', 'None')
            if (Test-Path -LiteralPath "$Base\first-start.json") { Restore-Previous }
            Write-Json "$Base\result.json" @{status='error'; message=$message}
        } finally {
            if ($queue) { $queue.Dispose() }
            if ($lock) { $lock.Dispose() }
        }
    }
}
if (!$restart) { exit 1 }
