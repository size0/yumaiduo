[CmdletBinding()]
param(
    [switch]$ResetKey,
    [switch]$NoBrowser,
    [switch]$PromptKey
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendRoot = Join-Path $projectRoot 'backend'
Set-Location $projectRoot
$env:PYTHONPATH = $backendRoot
$frontendUrl = 'http://127.0.0.1:8000'

if ($ResetKey) {
    Remove-Item Env:DASHSCOPE_API_KEY -ErrorAction SilentlyContinue
}

if ($ResetKey) { $PromptKey = $true }
if ($PromptKey) {
    do {
        $secureKey = Read-Host 'Enter a NEW DashScope API Key for this process (hidden)' -AsSecureString
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
        try {
            $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer).Trim()
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        }
        if (-not $plainKey) { Write-Host 'API Key cannot be empty. Please try again.' -ForegroundColor Yellow }
    } while (-not $plainKey)
    $env:DASHSCOPE_API_KEY = $plainKey
    $plainKey = $null
} elseif ($env:DASHSCOPE_API_KEY) {
    Write-Host 'Using DASHSCOPE_API_KEY from the current PowerShell environment as a fallback.' -ForegroundColor DarkGray
} else {
    Write-Host 'No environment API Key. Configure and persist it from the web Settings panel.' -ForegroundColor Yellow
}

if (-not $env:DASHSCOPE_BASE_URL) { $env:DASHSCOPE_BASE_URL = 'https://dashscope.aliyuncs.com/compatible-mode/v1' }
# This is the first-run default. Persisted web settings take precedence.
$env:DASHSCOPE_MODEL = 'qwen3.5-flash-2026-02-23'

Write-Host "Frontend: $frontendUrl" -ForegroundColor Cyan
Write-Host "API docs: $frontendUrl/docs" -ForegroundColor DarkGray
Write-Host 'Model, thinking mode, prompt, endpoint and persistent API Key are managed in the web Settings panel.' -ForegroundColor DarkGray
Write-Host 'Log file: .\logs\app.log' -ForegroundColor DarkGray
Write-Host 'Live logs: Get-Content .\logs\app.log -Wait' -ForegroundColor DarkGray

if (-not $NoBrowser) {
    $openBrowserCommand = "Start-Sleep -Seconds 2; Start-Process '$frontendUrl'"
    Start-Process powershell.exe -WindowStyle Hidden -ArgumentList @('-NoProfile', '-Command', $openBrowserCommand)
}

python -m uvicorn app.main:app --app-dir $backendRoot --host 127.0.0.1 --port 8000
