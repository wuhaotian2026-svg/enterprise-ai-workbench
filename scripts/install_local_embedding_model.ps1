param(
    [Parameter(Mandatory = $true)]
    [string]$DownloadRoot,

    [Parameter(Mandatory = $true)]
    [string]$InstallRoot
)

$ErrorActionPreference = 'Stop'
$modelId = 'intfloat/multilingual-e5-small'
$expected = [ordered]@{
    'config.json' = 'BBB7C1333FC4B3E27FBC9CD5D2070AABCC1D4DFB99917C3633E772F97545A6B6'
    'model.onnx' = 'CA456C06B3A9505DDFD9131408916DD79290368331E7D76BB621F1CBA6BC8665'
    'sentencepiece.bpe.model' = 'CFC8146ABE2A0488E9E2A0C56DE7952F7C11AB059ECA145A0A727AFCE0DB2865'
    'special_tokens_map.json' = 'D05497F1DA52C5E09554C0CD874037A083E1DC1B9CFD48034D1C717F1AFC07A7'
    'tokenizer.json' = '0B44A9D7B51C3C62626640CDA0E2C2F70FDACDC25BBBD68038369D14EBDF4C39'
    'tokenizer_config.json' = 'A1D6BC8734A6F635DC158508BEF000F8E2E5A759C7D92F984B2C86E5FF53425B'
}

New-Item -ItemType Directory -Force -Path $DownloadRoot, $InstallRoot | Out-Null
foreach ($name in $expected.Keys) {
    $download = Join-Path $DownloadRoot $name
    $install = Join-Path $InstallRoot $name
    if (-not (Test-Path -LiteralPath $download)) {
        $partial = "$download.partial"
        if (Test-Path -LiteralPath $partial) { throw "Refusing to overwrite incomplete download: $partial" }
        $url = "https://huggingface.co/$modelId/resolve/main/onnx/$name`?download=true"
        & curl.exe -L --fail --retry 3 --retry-delay 2 --output $partial $url
        if ($LASTEXITCODE -ne 0) { throw "Download failed: $name" }
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $partial).Hash -ne $expected[$name]) {
            throw "SHA-256 mismatch for downloaded file: $name"
        }
        Move-Item -LiteralPath $partial -Destination $download
    }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $download).Hash -ne $expected[$name]) {
        throw "SHA-256 mismatch in download cache: $download"
    }
    if (Test-Path -LiteralPath $install) {
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $install).Hash -ne $expected[$name]) {
            throw "Refusing to overwrite a different installed file: $install"
        }
    } else {
        Copy-Item -LiteralPath $download -Destination $install
    }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $install).Hash -ne $expected[$name]) {
        throw "Installed-file verification failed: $install"
    }
}

$bytes = (Get-ChildItem -File -LiteralPath $InstallRoot | Measure-Object Length -Sum).Sum
Write-Output "Local embedding model verified: $modelId"
Write-Output "Install path: $InstallRoot"
Write-Output "Files: $($expected.Count); bytes: $bytes; dimension: 384"
