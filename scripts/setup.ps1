param([switch]$NoLaunch, [int]$Jobs = 2)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$root = Split-Path -Parent $PSScriptRoot
$tool_root = Join-Path $root '.build-tools'

function installTool($_name, $_spec)
{
    $executable = Join-Path $tool_root $_spec.executable
    $marker = Join-Path $tool_root ($_name + '.ready')
    if ((Test-Path -LiteralPath $executable) -and (Test-Path -LiteralPath $marker) -and
        ([IO.File]::ReadAllText($marker) -eq $_spec.sha256))
    {
        return $executable
    }
    New-Item -ItemType Directory -Path $tool_root -Force | Out-Null
    $archive = Join-Path $tool_root ($_name + '.zip')
    if (-not (Test-Path -LiteralPath $archive))
    {
        Write-Host "Downloading $_name from its official release..."
        Invoke-WebRequest -UseBasicParsing -Uri $_spec.url -OutFile ($archive + '.pending')
        Move-Item -LiteralPath ($archive + '.pending') -Destination $archive -Force
    }
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $_spec.sha256)
    {
        Remove-Item -LiteralPath $archive
        throw "Checksum mismatch for $_name. Run the launcher again to retry."
    }
    Write-Host "Extracting $_name..."
    $destination = Join-Path $tool_root $_name
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    if (Get-Command tar.exe -ErrorAction SilentlyContinue)
    {
        & tar.exe -xf $archive -C $destination
        if ($LASTEXITCODE -ne 0) { throw "Could not extract $_name" }
    }
    else
    {
        Expand-Archive -LiteralPath $archive -DestinationPath $destination -Force
    }
    if (-not (Test-Path -LiteralPath $executable)) { throw "Missing executable after extracting $_name" }
    [IO.File]::WriteAllText($marker, $_spec.sha256)
    Remove-Item -LiteralPath $archive
    return $executable
}

try
{
    if (-not [Environment]::Is64BitProcess -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64')
    {
        throw 'The Windows launcher requires 64-bit Windows on an x64 processor.'
    }
    if ($Jobs -lt 1) { throw 'Jobs must be at least 1.' }
    $manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'windows_tools.json') -Raw | ConvertFrom-Json
    $uv = installTool 'uv' $manifest.uv
    $compiler = installTool 'llvm' $manifest.llvm
    $compiler_bin = Split-Path -Parent $compiler
    $env:CC = Join-Path $compiler_bin 'clang.exe'
    $env:CXX = $compiler
    $env:RC = Join-Path $compiler_bin 'windres.exe'
    $env:PATH = $compiler_bin + ';' + $env:PATH
    $env:UV_CACHE_DIR = Join-Path $tool_root 'uv-cache'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $tool_root 'python'
    $python = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python))
    {
        Write-Host 'Preparing local Python 3.12...'
        & $uv venv --python 3.12 --managed-python --seed (Join-Path $root '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Could not prepare local Python.' }
    }
    $setup_arguments = @((Join-Path $PSScriptRoot 'setup.py'), '--jobs', "$Jobs")
    if ($NoLaunch) { $setup_arguments += '--no-launch' }
    & $python @setup_arguments
    if ($LASTEXITCODE -ne 0) { throw 'Build or runtime validation failed.' }
}
catch
{
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
