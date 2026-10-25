# Creates files only. Trust is granted separately, during the explicitly requested installation.
param([Parameter(Mandatory=$true)][string]$Directory)
$ErrorActionPreference = 'Stop'
$inputCertificateDirectory = [IO.Path]::GetFullPath($Directory)
if (Test-Path -LiteralPath $inputCertificateDirectory) { throw 'Use a fresh private certificate directory' }
New-Item -ItemType Directory -Path $inputCertificateDirectory | Out-Null
$inputAcl = New-Object Security.AccessControl.DirectorySecurity
$inputAcl.SetAccessRuleProtection($true,$false)
$inputOwner = [Security.Principal.WindowsIdentity]::GetCurrent().User
$inputAcl.SetOwner($inputOwner)
foreach ($inputSid in @($inputOwner,(New-Object Security.Principal.SecurityIdentifier('S-1-5-18')),(New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')))) {
    $inputRule = New-Object Security.AccessControl.FileSystemAccessRule($inputSid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
    $inputAcl.AddAccessRule($inputRule)
}
Set-Acl -LiteralPath $inputCertificateDirectory -AclObject $inputAcl
$inputRsa = [Security.Cryptography.RSACng]::new(3072)
$inputRequest = New-Object Security.Cryptography.X509Certificates.CertificateRequest('CN=NGS Input Local Publisher',$inputRsa,[Security.Cryptography.HashAlgorithmName]::SHA256,[Security.Cryptography.RSASignaturePadding]::Pkcs1)
$inputRequest.CertificateExtensions.Add((New-Object Security.Cryptography.X509Certificates.X509BasicConstraintsExtension($false,$false,0,$true)))
$inputRequest.CertificateExtensions.Add((New-Object Security.Cryptography.X509Certificates.X509KeyUsageExtension([Security.Cryptography.X509Certificates.X509KeyUsageFlags]::DigitalSignature,$true)))
$inputUsage = New-Object Security.Cryptography.OidCollection
$inputUsage.Add((New-Object Security.Cryptography.Oid('1.3.6.1.5.5.7.3.3'))) | Out-Null
$inputRequest.CertificateExtensions.Add((New-Object Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension($inputUsage,$true)))
$inputCertificate = $inputRequest.CreateSelfSigned([DateTimeOffset]::Now.AddDays(-1),[DateTimeOffset]::Now.AddYears(2))
$inputRandom = New-Object byte[] 32
$inputGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
$inputGenerator.GetBytes($inputRandom)
$inputPassword = [Convert]::ToBase64String($inputRandom)
try {
    [IO.File]::WriteAllBytes((Join-Path $inputCertificateDirectory 'publisher.pfx'),$inputCertificate.Export([Security.Cryptography.X509Certificates.X509ContentType]::Pfx,$inputPassword))
    [IO.File]::WriteAllBytes((Join-Path $inputCertificateDirectory 'publisher.cer'),$inputCertificate.Export([Security.Cryptography.X509Certificates.X509ContentType]::Cert))
    [IO.File]::WriteAllText((Join-Path $inputCertificateDirectory 'password.txt'),$inputPassword)
    $inputInformation = @{subject=$inputCertificate.Subject;thumbprint=$inputCertificate.Thumbprint;expires=$inputCertificate.NotAfter.ToUniversalTime().ToString('o');purpose='Code Signing only';trusted=$false}
    $inputInformation | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $inputCertificateDirectory 'publisher.json') -Encoding UTF8
    $inputInformation | ConvertTo-Json
} finally { $inputCertificate.Dispose(); $inputRsa.Dispose(); $inputGenerator.Dispose() }
