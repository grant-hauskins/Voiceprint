param(
    [ValidateSet('build', 'api', 'worker', 'mcp', 'tunnel', 'token')][string]$Mode = 'build',
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$ForwardArgs
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    # Shared secret for the public MCP endpoint. Generated once into ignored data\mcp-token.txt unless VOICEPRINT_MCP_TOKEN is set.
    $tokenFile = Join-Path $projectRoot 'data\mcp-token.txt'
    if (-not $env:VOICEPRINT_MCP_TOKEN) {
        if (-not (Test-Path -LiteralPath $tokenFile)) {
            New-Item -ItemType Directory -Force (Split-Path -Parent $tokenFile) | Out-Null
            $bytes = New-Object byte[] 24; [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
            [System.IO.File]::WriteAllText($tokenFile, ([System.BitConverter]::ToString($bytes) -replace '-', '').ToLower())
        }
        $env:VOICEPRINT_MCP_TOKEN = (Get-Content -LiteralPath $tokenFile -Raw).Trim()
    }
    if ($Mode -eq 'token') { Write-Output $env:VOICEPRINT_MCP_TOKEN; exit 0 }
    if ($Mode -eq 'tunnel') {
        $port = if ($env:VOICEPRINT_MCP_PORT) { $env:VOICEPRINT_MCP_PORT } else { '8082' }
        $found = Get-Command cloudflared.exe -ErrorAction SilentlyContinue
        $cloudflared = if ($found) { $found.Source } else { $null }
        if (-not $cloudflared) {
            foreach ($candidate in @("$env:ProgramFiles\cloudflared\cloudflared.exe", "${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe", "$env:LOCALAPPDATA\Microsoft\WinGet\Links\cloudflared.exe")) {
                if (Test-Path -LiteralPath $candidate) { $cloudflared = $candidate; break }
            }
        }
        if (-not $cloudflared) { throw 'Install cloudflared: winget install --id Cloudflare.cloudflared' }
        Write-Host "MCP bearer token: $env:VOICEPRINT_MCP_TOKEN"
        Write-Host "Exposing http://127.0.0.1:$port/mcp - use https://<name>.trycloudflare.com/mcp as the MCP server_url"
        & $cloudflared tunnel --url "http://127.0.0.1:$port" @ForwardArgs
        exit $LASTEXITCODE
    }
    if ($Mode -eq 'worker') {
        & (Join-Path $projectRoot '.venv\Scripts\python.exe') worker\worker.py @ForwardArgs
        exit $LASTEXITCODE
    }
    $taskJdk = $env:JAVA_HOME
    if (-not $taskJdk -or -not (Test-Path -LiteralPath (Join-Path $taskJdk 'bin\javac.exe'))) {
        $compiler = Get-Command javac.exe -ErrorAction SilentlyContinue
        if ($compiler) { $taskJdk = Split-Path -Parent (Split-Path -Parent $compiler.Source) }
    }
    if (-not $taskJdk -or -not (Test-Path -LiteralPath (Join-Path $taskJdk 'bin\javac.exe'))) {
        foreach ($editor in @('.cursor', '.vscode')) {
            $extensionRoot = Join-Path $env:USERPROFILE "$editor\extensions"
            foreach ($extension in (Get-ChildItem -Path "$extensionRoot\redhat.java-*" -Directory -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)) {
                $candidate = Get-ChildItem -LiteralPath (Join-Path $extension.FullName 'jre') -Directory -ErrorAction SilentlyContinue |
                    Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'bin\javac.exe') } | Select-Object -First 1
                if ($candidate) { $taskJdk = $candidate.FullName; break }
            }
            if ($taskJdk -and (Test-Path -LiteralPath (Join-Path $taskJdk 'bin\javac.exe'))) { break }
        }
    }
    if (-not $taskJdk) { throw 'Install JDK 21 and set JAVA_HOME.' }
    $env:JAVA_HOME = $taskJdk
    if ($Mode -eq 'build') {
        $taskMaven = Join-Path $projectRoot '.tools\apache-maven-3.9.11\bin\mvn.cmd'
        if (-not (Test-Path -LiteralPath $taskMaven)) {
            $installed = Get-Command mvn.cmd -ErrorAction SilentlyContinue
            if (-not $installed) { throw 'Install Maven 3.9+ and put it on PATH.' }
            $taskMaven = $installed.Source
        }
        if (-not $ForwardArgs) { $ForwardArgs = @('package') }
        & $taskMaven '-Dmaven.repo.local=.tools/m2' '-B' '-ntp' @ForwardArgs
    } else {
        $runtime = Join-Path $taskJdk 'bin\java.exe'
        if ($Mode -eq 'mcp') { & $runtime -jar target\voiceprint-0.1.0.jar mcp @ForwardArgs }
        else { & $runtime -jar target\voiceprint-0.1.0.jar @ForwardArgs }
    }
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
