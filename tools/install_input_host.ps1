param(
    [Parameter(Mandatory=$true)][string]$Package,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-fA-F0-9]{40}$')][string]$PublisherThumbprint,
    [switch]$TrustLocalPublisher,
    [switch]$Update
)
$ErrorActionPreference = 'Stop'
$inputPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (!$inputPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run this installer from an administrator PowerShell' }
$inputPackage = (Resolve-Path -LiteralPath $Package).Path
$inputPayload = Join-Path $inputPackage 'payload'
$inputCatalog = Join-Path $inputPackage 'NGSInput.cat'
if ($TrustLocalPublisher) {
    $inputPublisherPath = Join-Path $inputPackage 'publisher.cer'
    $inputPublisher = New-Object Security.Cryptography.X509Certificates.X509Certificate2($inputPublisherPath)
    $inputUsageExtension = $inputPublisher.Extensions | Where-Object { $_.Oid.Value -eq '2.5.29.37' }
    $inputUsage = @($inputUsageExtension.EnhancedKeyUsages | ForEach-Object { $_.Value })
    $inputConstraints = $inputPublisher.Extensions | Where-Object { $_.Oid.Value -eq '2.5.29.19' }
    if ($inputPublisher.Thumbprint -ne $PublisherThumbprint -or $inputUsage.Count -ne 1 -or $inputUsage[0] -ne '1.3.6.1.5.5.7.3.3' -or !$inputConstraints -or $inputConstraints.CertificateAuthority) { throw 'Unexpected local Code Signing certificate' }
    Import-Certificate -FilePath $inputPublisherPath -CertStoreLocation Cert:\LocalMachine\Root | Out-Null
}
function Assert-InputSignature([string]$Path) {
    $inputSignature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($inputSignature.Status -ne 'Valid' -or $inputSignature.SignerCertificate.Thumbprint -ne $PublisherThumbprint) {
        throw ('Untrusted or unexpected publisher: ' + $Path)
    }
}
Assert-InputSignature $inputCatalog
Assert-InputSignature (Join-Path $inputPayload 'NGSInput.exe')
if ((Test-FileCatalog -Path $inputPayload -CatalogFilePath $inputCatalog) -ne 'Valid') { throw 'The signed package has changed' }
$inputSourceItems = @(Get-Item -LiteralPath $inputPayload) + @(Get-ChildItem -LiteralPath $inputPayload -Recurse -Force)
if ($inputSourceItems | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }) { throw 'Package links are not allowed' }
$inputProgramFiles = [Environment]::GetFolderPath('ProgramFiles')
$inputDestination = [IO.Path]::GetFullPath((Join-Path $inputProgramFiles 'NGS Input'))
if ([IO.Path]::GetDirectoryName($inputDestination) -ne $inputProgramFiles) { throw 'Invalid protected destination' }
foreach ($inputPath in @($inputProgramFiles,$inputDestination)) {
    if ((Test-Path -LiteralPath $inputPath) -and ((Get-Item -LiteralPath $inputPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Protected destination links are not allowed' }
}
$inputUpdating = Test-Path -LiteralPath $inputDestination
if ($inputUpdating) {
    if (!$Update) { throw 'NGS Input is already installed. Use the explicit Update option to replace this component.' }
    Assert-InputSignature (Join-Path $inputDestination 'NGSInput.exe')
    if (Get-ChildItem -LiteralPath $inputDestination -Recurse -Force | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }) { throw 'Installed component links are not allowed' }
    if (Get-Process NGSInput -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq (Join-Path $inputDestination 'NGSInput.exe') }) { throw 'Close all NGS input windows before updating this component' }
}
$inputStage = Join-Path $inputProgramFiles ('NGS-Input-stage-' + [Guid]::NewGuid().ToString('N'))
$inputAcl = New-Object Security.AccessControl.DirectorySecurity
$inputAcl.SetAccessRuleProtection($true,$false)
$inputAdmins = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')
$inputAcl.SetOwner($inputAdmins)
$inputFileAcl = New-Object Security.AccessControl.FileSecurity
$inputFileAcl.SetAccessRuleProtection($true,$false)
$inputFileAcl.SetOwner($inputAdmins)
foreach ($inputRule in @(@('S-1-5-18','FullControl'),@('S-1-5-32-544','FullControl'))) {
    $inputSid = New-Object Security.Principal.SecurityIdentifier($inputRule[0])
    $inputAccess = New-Object Security.AccessControl.FileSystemAccessRule($inputSid,$inputRule[1],'ContainerInherit,ObjectInherit','None','Allow')
    $inputAcl.AddAccessRule($inputAccess)
    $inputFileAcl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($inputSid,'FullControl','Allow')))
}
[IO.Directory]::CreateDirectory($inputStage,$inputAcl) | Out-Null
# Keep staged bytes private until signatures and hashes pass; never copy source ACLs or links.
$inputSourceRoot = (Resolve-Path -LiteralPath $inputPayload).Path.TrimEnd('\') + '\'
foreach ($inputSource in ($inputSourceItems | Where-Object { $_.FullName -ne $inputSourceRoot.TrimEnd('\') } | Sort-Object { $_.FullName.Length })) {
    if (!$inputSource.FullName.StartsWith($inputSourceRoot,[StringComparison]::OrdinalIgnoreCase)) { throw 'Unexpected package path' }
    $inputTarget = [IO.Path]::GetFullPath((Join-Path $inputStage $inputSource.FullName.Substring($inputSourceRoot.Length)))
    if (!$inputTarget.StartsWith($inputStage + '\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid staged path' }
    if ($inputSource.PSIsContainer) { [IO.Directory]::CreateDirectory($inputTarget,$inputAcl) | Out-Null }
    else {
        $inputSourceStream = [IO.File]::OpenRead($inputSource.FullName)
        try {
            $inputTargetStream = [IO.FileStream]::new($inputTarget,[IO.FileMode]::CreateNew,[Security.AccessControl.FileSystemRights]::Write,[IO.FileShare]::None,65536,[IO.FileOptions]::None,$inputFileAcl)
            try { $inputSourceStream.CopyTo($inputTargetStream); $inputTargetStream.Flush($true) }
            finally { $inputTargetStream.Dispose() }
        } finally { $inputSourceStream.Dispose() }
    }
}
foreach ($inputChild in Get-ChildItem -LiteralPath $inputStage -Recurse -Force) {
    $inputChildAcl = Get-Acl -LiteralPath $inputChild.FullName
    foreach ($inputExplicitRule in $inputChildAcl.GetAccessRules($true,$false,[Security.Principal.SecurityIdentifier])) {
        $inputChildAcl.RemoveAccessRuleSpecific($inputExplicitRule)
    }
    $inputChildAcl.SetAccessRuleProtection($false,$false)
    if ($inputChild.PSIsContainer) { [IO.Directory]::SetAccessControl($inputChild.FullName,$inputChildAcl) }
    else { [IO.File]::SetAccessControl($inputChild.FullName,$inputChildAcl) }
}
Assert-InputSignature $inputCatalog
Assert-InputSignature (Join-Path $inputStage 'NGSInput.exe')
if ((Test-FileCatalog -Path $inputStage -CatalogFilePath $inputCatalog) -ne 'Valid') { throw ('Staged package validation failed; preserved at ' + $inputStage) }
$inputUsers = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-545')
$inputAcl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($inputUsers,'ReadAndExecute','ContainerInherit,ObjectInherit','None','Allow')))
[IO.Directory]::SetAccessControl($inputStage,$inputAcl)
$inputBackup = $null
if ($inputUpdating) {
    Assert-InputSignature (Join-Path $inputDestination 'NGSInput.exe')
    $inputBackup = Join-Path $inputProgramFiles ('NGS-Input-previous-' + [Guid]::NewGuid().ToString('N'))
    if ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($inputBackup)) -ne $inputProgramFiles) { throw 'Invalid backup path' }
    [IO.Directory]::Move($inputDestination,$inputBackup)
}
try { [IO.Directory]::Move($inputStage,$inputDestination) }
catch {
    if ($inputBackup -and !(Test-Path -LiteralPath $inputDestination)) { [IO.Directory]::Move($inputBackup,$inputDestination) }
    throw
}
# Temporary device diagnostics: Windows Error Reporting keeps up to five small
# minidumps of NGS processes in %LOCALAPPDATA%\CrashDumps of the crashing user.
# LocalDumps is honoured only under HKLM; failure here never blocks installation.
foreach ($inputDumpImage in @('NGS.exe','NGSInput.exe')) {
    try {
        $inputDumpKey = 'HKLM:\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\' + $inputDumpImage
        if (!(Test-Path -LiteralPath $inputDumpKey)) { New-Item -Path $inputDumpKey -Force | Out-Null }
        New-ItemProperty -LiteralPath $inputDumpKey -Name DumpType -PropertyType DWord -Value 1 -Force | Out-Null
        New-ItemProperty -LiteralPath $inputDumpKey -Name DumpCount -PropertyType DWord -Value 5 -Force | Out-Null
    } catch { Write-Output ('Crash dump registration skipped for ' + $inputDumpImage) }
}
if ($inputBackup) { Write-Output ('Previous signed component preserved: ' + $inputBackup) }
Write-Output ('Installed ' + $inputDestination + '. Restart NGS. Publisher: ' + $PublisherThumbprint)
