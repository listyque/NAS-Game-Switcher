param(
    [Parameter(Mandatory=$true)][string]$Package,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-fA-F0-9]{40}$')][string]$CertificateThumbprint,
    [string]$PrivateCertificateDirectory
)
$ErrorActionPreference = 'Stop'
$inputPackage = (Resolve-Path -LiteralPath $Package).Path
$inputPayload = Join-Path $inputPackage 'payload'
$inputExecutable = Join-Path $inputPayload 'NGSInput.exe'
if (!(Test-Path -LiteralPath $inputExecutable -PathType Leaf)) { throw 'NGSInput.exe is missing' }
if ($PrivateCertificateDirectory) {
    $inputPassword = [IO.File]::ReadAllText((Join-Path $PrivateCertificateDirectory 'password.txt'))
    $inputCertificate = New-Object Security.Cryptography.X509Certificates.X509Certificate2((Join-Path $PrivateCertificateDirectory 'publisher.pfx'),$inputPassword,[Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet)
} else {
    $inputCertificate = Get-ChildItem Cert:\CurrentUser\My,Cert:\LocalMachine\My -CodeSigningCert |
        Where-Object { $_.Thumbprint -eq $CertificateThumbprint -and $_.HasPrivateKey } | Select-Object -First 1
}
if (!$inputCertificate) { throw 'The selected Code Signing certificate with its private key was not found' }
if ($inputCertificate.Thumbprint -ne $CertificateThumbprint) { throw 'The private certificate does not match the selected publisher' }
function Assert-InputSigned($Signature) {
    if (!$Signature.SignerCertificate -or $Signature.SignerCertificate.Thumbprint -ne $CertificateThumbprint -or $Signature.Status -notin @('Valid','NotTrusted','UnknownError')) {
        throw ('Signing failed: ' + $Signature.Status)
    }
}
$inputSignature = Get-AuthenticodeSignature -LiteralPath $inputExecutable
if ($inputSignature.Status -ne 'Valid' -or $inputSignature.SignerCertificate.Thumbprint -ne $CertificateThumbprint) {
    $inputSignature = Set-AuthenticodeSignature -LiteralPath $inputExecutable -Certificate $inputCertificate -HashAlgorithm SHA256
}
Assert-InputSigned $inputSignature
foreach ($inputScriptName in @('Install.ps1','install_input_host.ps1')) {
    $inputScriptPath = Join-Path $inputPackage $inputScriptName
    if (Test-Path -LiteralPath $inputScriptPath) {
        $inputSignature = Set-AuthenticodeSignature -LiteralPath $inputScriptPath -Certificate $inputCertificate -HashAlgorithm SHA256
        Assert-InputSigned $inputSignature
    }
}
$inputCatalog = Join-Path $inputPackage 'NGSInput.cat'
New-FileCatalog -Path $inputPayload -CatalogFilePath $inputCatalog -CatalogVersion 2.0 | Out-Null
$inputSignature = Set-AuthenticodeSignature -LiteralPath $inputCatalog -Certificate $inputCertificate -HashAlgorithm SHA256
Assert-InputSigned $inputSignature
if ((Test-FileCatalog -Path $inputPayload -CatalogFilePath $inputCatalog) -ne 'Valid') { throw 'File catalog validation failed' }
Write-Output ('Signed publisher: ' + $inputCertificate.Subject + ' / ' + $inputCertificate.Thumbprint)
[IO.File]::WriteAllBytes((Join-Path $inputPackage 'publisher.cer'),$inputCertificate.Export([Security.Cryptography.X509Certificates.X509ContentType]::Cert))
Write-Output ('Trust status before installation: ' + $inputSignature.Status)
