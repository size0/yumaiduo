[CmdletBinding()]
param(
    [ValidateSet('ReadOnly', 'Write')]
    [string]$Mode = 'ReadOnly',

    [string]$WorktreePath,

    [string]$SessionDirectory
)

$ErrorActionPreference = 'Stop'
$scriptRoot = Split-Path -Parent $PSCommandPath
if ([string]::IsNullOrWhiteSpace($WorktreePath)) {
    $WorktreePath = $scriptRoot
}
$WorktreePath = (Resolve-Path -LiteralPath $WorktreePath).Path

if (-not (Get-Command pi -ErrorAction SilentlyContinue)) {
    throw '未找到 pi 命令。请先确认 Pi 已安装且已加入 PATH。'
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw '未找到 git 命令。'
}

& git -C $WorktreePath rev-parse --is-inside-work-tree *> $null
if ($LASTEXITCODE -ne 0) {
    throw 'WorktreePath 必须是 Git worktree。'
}

# This launcher opens an existing worktree. New isolated tasks must be created
# with Invoke-PiTask.ps1 first.
$guardrail = if ($Mode -eq 'ReadOnly') {
    '当前会话为只读检查：不得修改文件。禁止提交、合并、推送、部署或删除 worktree。新任务请先使用 Invoke-PiTask.ps1 创建隔离 worktree。'
}
else {
    '当前会话可修改此 worktree，但默认禁止提交、合并、推送、部署或删除 worktree。保留未提交改动供人工检查。'
}

$piArguments = @('--append-system-prompt', $guardrail)
if ($Mode -eq 'ReadOnly') {
    $piArguments = @('--no-approve', '--no-extensions', '--tools', 'read,grep,find,ls') + $piArguments
}
else {
    $piArguments = @('--approve') + $piArguments
}
if (-not [string]::IsNullOrWhiteSpace($SessionDirectory)) {
    if (-not [System.IO.Path]::IsPathRooted($SessionDirectory)) {
        $SessionDirectory = [System.IO.Path]::GetFullPath((Join-Path $WorktreePath $SessionDirectory))
    }
    New-Item -ItemType Directory -Force -Path $SessionDirectory | Out-Null
    $piArguments += @('--session-dir', $SessionDirectory)
}

function ConvertTo-SingleQuotedPowerShellLiteral {
    param([Parameter(Mandatory)][string]$Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

$quotedWorktree = ConvertTo-SingleQuotedPowerShellLiteral -Value $WorktreePath
$quotedArguments = @($piArguments | ForEach-Object { ConvertTo-SingleQuotedPowerShellLiteral -Value ([string]$_) })
$command = "Set-Location -LiteralPath $quotedWorktree; & pi " + ($quotedArguments -join ' ')

Write-Host "[Pi] Worktree: $WorktreePath" -ForegroundColor Cyan
Write-Host "[Pi] Mode: $Mode" -ForegroundColor Cyan
Write-Host '[Pi] 默认不提交、不合并、不推送、不部署，也不删除 worktree。' -ForegroundColor Yellow

if (Get-Command wt -ErrorAction SilentlyContinue) {
    Start-Process wt -ArgumentList @('new-tab', 'powershell', '-NoExit', '-Command', $command)
}
else {
    Start-Process powershell -ArgumentList @('-NoExit', '-Command', $command)
}
