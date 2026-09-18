# language: PowerShell, file: build_install.ps1
# Compile + self-sign + install + load WdiSvcMon.sys. Run as Administrator.

param(
    [string]$Src     = ".\wk_driver.c",
    [string]$OutDir  = ".\out",
    [string]$SvcName = "WdiSvcMon"
)

$ErrorActionPreference = "Stop"
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this PowerShell window as Administrator."
}

$bcd = bcdedit /enum "{current}"
if (-not ($bcd | Select-String "testsigning\s+Yes")) {
    Write-Host ""
    Write-Warning "testsigning is OFF."
    Write-Host "    Run:  bcdedit /set testsigning on"
    Write-Host "    Then: shutdown /r /t 0"
    Write-Host "    Then re-run this script."
    return
}

$kits = "C:\Program Files (x86)\Windows Kits\10"
if (-not (Test-Path $kits)) { throw "Windows Kits\10 not found. Install the WDK." }

$sdkVer = (Get-ChildItem "$kits\bin" -Directory |
           Where-Object { $_.Name -match '^\d+\.' } |
           Sort-Object Name -Descending | Select-Object -First 1).Name
if (-not $sdkVer) { throw "No SDK version folder under $kits\bin." }

$cl   = "$kits\bin\$sdkVer\x64\cl.exe"
$link = "$kits\bin\$sdkVer\x64\link.exe"
$st   = "$kits\bin\$sdkVer\x64\signtool.exe"
if (-not (Test-Path $cl))   { throw "cl.exe not found. Install VS Build Tools + C++ workload." }
if (-not (Test-Path $link)) { throw "link.exe not found." }
if (-not (Test-Path $st))   { throw "signtool.exe not found." }

$wdkInc = "$kits\Include\$sdkVer"
$wdkLib = "$kits\Lib\$sdkVer"

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

Write-Host "[1/5] Compiling $Src ..." -ForegroundColor Cyan
& $cl /nologo /c /O2 /GS- /kernel /Fo"$OutDir\wk_driver.obj" $Src `
    /I"$wdkInc\km" /I"$wdkInc\shared" /I"$wdkInc\um" `
    /D"_WIN64" /D"AMD64" /D"POOL_NX_OPTIN=1" /D"NT=1"
if ($LASTEXITCODE) { throw "compile failed" }

Write-Host "[2/5] Linking ..." -ForegroundColor Cyan
& $link /nologo /DRIVER /SUBSYSTEM:NATIVE /ENTRY:DriverEntry `
    /LIBPATH:"$wdkLib\km\x64" `
    /OUT:"$OutDir\WdiSvcMon.sys" "$OutDir\wk_driver.obj" `
    ntoskrnl.lib hal.lib wdm.lib BufferOverflowK.lib
if ($LASTEXITCODE) { throw "link failed" }

Write-Host "[3/5] Creating/loading self-signed cert ..." -ForegroundColor Cyan
$subject = "CN=WdiSvcMonLocal"
$existing = Get-ChildItem Cert:\LocalMachine\My |
            Where-Object { $_.Subject -eq $subject } |
            Select-Object -First 1
if ($existing) {
    $cert = $existing
    Write-Host "    reusing $($cert.Thumbprint)"
} else {
    $cert = New-SelfSignedCertificate -Type CodeSigningCert `
        -Subject $subject `
        -KeyUsage DigitalSignature `
        -KeyAlgorithm RSA -KeyLength 2048 `
        -CertStoreLocation "Cert:\LocalMachine\My" `
        -NotAfter (Get-Date).AddYears(5)
    Write-Host "    created $($cert.Thumbprint)"
    Export-Certificate -Cert $cert -FilePath "$OutDir\WdiSvcMonLocal.cer" | Out-Null
    Import-Certificate -FilePath "$OutDir\WdiSvcMonLocal.cer" `
        -CertStoreLocation "Cert:\LocalMachine\Root" | Out-Null
    Import-Certificate -FilePath "$OutDir\WdiSvcMonLocal.cer" `
        -CertStoreLocation "Cert:\LocalMachine\TrustedPublisher" | Out-Null
}

Write-Host "[4/5] Signing driver ..." -ForegroundColor Cyan
& $st sign /fd SHA256 /sha1 $cert.Thumbprint /v "$OutDir\WdiSvcMon.sys"
if ($LASTEXITCODE) { throw "signtool failed" }

Write-Host "[5/5] Installing + starting service ..." -ForegroundColor Cyan
$dest = "$env:SystemRoot\System32\drivers\WdiSvcMon.sys"
Copy-Item "$OutDir\WdiSvcMon.sys" $dest -Force

if (Get-Service -Name $SvcName -ErrorAction SilentlyContinue) {
    Stop-Service $SvcName -ErrorAction SilentlyContinue
    sc.exe delete $SvcName | Out-Null
    Start-Sleep -Milliseconds 500
}

sc.exe create $SvcName type= kernel start= demand binpath= $dest | Out-Null
$start = sc.exe start $SvcName 2>&1
Write-Host $start

Write-Host ""
Write-Host "=== DONE ===" -ForegroundColor Green
Write-Host "Verify:"
Write-Host "    sc query $SvcName"
Write-Host "    python pk_demo.py"