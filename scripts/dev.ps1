param(
    [ValidateSet('build', 'api', 'worker', 'mcp')][string]$Mode = 'build',
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$ForwardArgs
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
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
