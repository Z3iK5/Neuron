# SPDX-License-Identifier: Apache-2.0
#
# Build a Windows MSIX from the PyInstaller bundle (dist/Neuron). Run on
# windows-latest from the neuron/ directory. Produces Neuron.msix.
#
# Identity comes from repo variables when set (your Microsoft Partner Center
# reservation), else dev defaults:
#   MSIX_IDENTITY_NAME, MSIX_PUBLISHER, MSIX_PUBLISHER_DISPLAY_NAME
# With MSIX_PUBLISHER set, the package is left UNSIGNED (the Store signs it on
# submission). With the dev default, it is self-signed so it can be sideloaded for
# testing (trust the exported Neuron-dev-cert.cer first).
$ErrorActionPreference = "Stop"

$bundle = "dist/Neuron"
if (-not (Test-Path "$bundle/Neuron.exe")) {
  throw "$bundle/Neuron.exe not found - build the PyInstaller bundle first"
}

$identityName = if ($env:MSIX_IDENTITY_NAME) { $env:MSIX_IDENTITY_NAME } else { "Neuron.Desktop" }
$publisher = if ($env:MSIX_PUBLISHER) { $env:MSIX_PUBLISHER } else { "CN=Neuron" }
$publisherDisplay = if ($env:MSIX_PUBLISHER_DISPLAY_NAME) { $env:MSIX_PUBLISHER_DISPLAY_NAME } else { "Neuron" }
$selfSign = -not [bool]$env:MSIX_PUBLISHER

# MSIX needs a 4-part version; derive from pyproject (e.g. 0.0.3 -> 0.0.3.0).
$ver = (Select-String -Path "pyproject.toml" -Pattern '^version = "(.+)"').Matches[0].Groups[1].Value
$msixVer = "$ver.0"

# Stage: app files + Assets + manifest.
$stage = Join-Path $env:RUNNER_TEMP "msix"
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Path $stage | Out-Null
Copy-Item -Recurse "$bundle/*" $stage
$assets = Join-Path $stage "Assets"
python packaging/make_msix_assets.py $assets
if ($LASTEXITCODE -ne 0) { throw "make_msix_assets failed ($LASTEXITCODE)" }

$manifest = Get-Content "packaging/AppxManifest.template.xml" -Raw
$manifest = $manifest.Replace("@IDENTITY_NAME@", $identityName).
  Replace("@PUBLISHER@", $publisher).
  Replace("@PUBLISHER_DISPLAY_NAME@", $publisherDisplay).
  Replace("@VERSION@", $msixVer)
Set-Content -Path (Join-Path $stage "AppxManifest.xml") -Value $manifest -Encoding UTF8

function Find-SdkTool($name) {
  $hit = Get-ChildItem "C:/Program Files (x86)/Windows Kits/10/bin" -Recurse -Filter $name `
    -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match "\\x64\\" } |
    Sort-Object FullName -Descending | Select-Object -First 1
  if (-not $hit) { throw "$name not found in the Windows SDK" }
  return $hit.FullName
}

$makeappx = Find-SdkTool "makeappx.exe"
& $makeappx pack /d $stage /p "Neuron.msix" /o
if ($LASTEXITCODE -ne 0) { throw "makeappx failed ($LASTEXITCODE)" }

if ($selfSign) {
  $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject $publisher `
    -CertStoreLocation "Cert:\CurrentUser\My" -KeyExportPolicy Exportable `
    -NotAfter (Get-Date).AddYears(2)
  $pwd = ConvertTo-SecureString ([guid]::NewGuid().ToString()) -Force -AsPlainText
  $pfx = Join-Path $env:RUNNER_TEMP "msix-dev.pfx"
  Export-PfxCertificate -Cert $cert -FilePath $pfx -Password $pwd | Out-Null
  $signtool = Find-SdkTool "signtool.exe"
  $plain = [System.Net.NetworkCredential]::new("", $pwd).Password
  & $signtool sign /fd SHA256 /a /f $pfx /p $plain "Neuron.msix"
  if ($LASTEXITCODE -ne 0) { throw "signtool failed ($LASTEXITCODE)" }
  Export-Certificate -Cert $cert -FilePath "Neuron-dev-cert.cer" | Out-Null
  Write-Host "Built self-signed Neuron.msix (sideload: import Neuron-dev-cert.cer into Trusted People first)."
} else {
  Write-Host "Built unsigned Neuron.msix (publisher $publisher) for Microsoft Store submission."
}
