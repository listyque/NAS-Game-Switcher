param([string]$DataRoot = '')
$ErrorActionPreference = 'Stop'
$inputPublisher = '0BD64A8A06DE15913085E6D1FFB7F9DEDDF63DCF'
$inputPackage = $PSScriptRoot
$inputInteractive = !$DataRoot
if (!$DataRoot) { $DataRoot = Join-Path $inputPackage 'data' }
$DataRoot = [IO.Path]::GetFullPath($DataRoot)
$env:PSModuleAnalysisCachePath = Join-Path $DataRoot 'cache\PowerShellModuleAnalysisCache'
[IO.Directory]::CreateDirectory((Split-Path -Parent $env:PSModuleAnalysisCachePath)) | Out-Null
try {
    $inputPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (!$inputPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        $inputProcess = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -DataRoot "' + $DataRoot + '"') -Verb RunAs -WindowStyle Hidden -PassThru -Wait
        exit $inputProcess.ExitCode
    }
    $inputInstaller = Join-Path $inputPackage 'install_input_host.ps1'
    $inputSignature = Get-AuthenticodeSignature -LiteralPath $inputInstaller
    if (!$inputSignature.SignerCertificate -or $inputSignature.SignerCertificate.Thumbprint -ne $inputPublisher -or $inputSignature.Status -notin @('Valid','NotTrusted','UnknownError')) { throw 'Unexpected installer signature' }
    $inputDestination = Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'NGS Input'
    $inputCatalog = Join-Path $inputPackage 'NGSInput.cat'
    $inputAlreadyCurrent = $false
    if (Test-Path -LiteralPath $inputDestination) {
        $inputSignature = Get-AuthenticodeSignature -LiteralPath (Join-Path $inputDestination 'NGSInput.exe')
        $inputAlreadyCurrent = $inputSignature.Status -eq 'Valid' -and $inputSignature.SignerCertificate.Thumbprint -eq $inputPublisher -and (Test-FileCatalog -Path $inputDestination -CatalogFilePath $inputCatalog) -eq 'Valid'
    }
    if (!$inputAlreadyCurrent) {
        $inputExe = Join-Path $inputDestination 'NGSInput.exe'
        $inputDeadline = [DateTime]::UtcNow.AddSeconds(2)
        while (Get-Process NGSInput -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $inputExe }) {
            if ([DateTime]::UtcNow -ge $inputDeadline) {
                Get-Process NGSInput -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $inputExe } | Stop-Process -Force -ErrorAction SilentlyContinue
                Start-Sleep -Milliseconds 200
                break
            }
            Start-Sleep -Milliseconds 100
        }
        & $inputInstaller -Package $inputPackage -PublisherThumbprint $inputPublisher -TrustLocalPublisher -Update
    }
    Write-Output 'NGS Input installed successfully.'
    exit 0
} catch {
    if ($inputInteractive) {
        Add-Type -AssemblyName PresentationFramework
        [Windows.MessageBox]::Show($_.Exception.Message, 'NGS Input — installation failed', 'OK', 'Error') | Out-Null
    } else {
        [Console]::Error.WriteLine($_.Exception.Message)
    }
    exit 1
}
