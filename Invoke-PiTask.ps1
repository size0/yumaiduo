[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateNotNullOrEmpty()]
    [string]$Task,

    [string]$Name = ("task-" + (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss')),

    [ValidateSet('ReadOnly', 'Write')]
    [string]$Mode = 'ReadOnly',

    [ValidateNotNullOrEmpty()]
    [string]$BaseCommit = 'HEAD',

    [string]$RunRoot,

    [string]$WorktreeRoot,

    [string]$TestCommand
)

$ErrorActionPreference = 'Stop'
$scriptRoot = Split-Path -Parent $PSCommandPath
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Get-FullPath {
    param(
        [Parameter(Mandatory)]
        [string]$Path,

        [Parameter(Mandatory)]
        [string]$BasePath
    )

    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }

    return [System.IO.Path]::GetFullPath((Join-Path $BasePath $Path))
}

function ConvertTo-SingleQuotedPowerShellLiteral {
    param([Parameter(Mandatory)][string]$Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

function Write-JsonAtomic {
    param(
        [Parameter(Mandatory)]$Value,
        [Parameter(Mandatory)][string]$Path
    )

    $directory = Split-Path -Parent $Path
    $temporaryPath = Join-Path $directory ('.metadata-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $backupPath = Join-Path $directory ('.metadata-' + [Guid]::NewGuid().ToString('N') + '.bak')
    try {
        $json = $Value | ConvertTo-Json -Depth 8
        Set-Content -LiteralPath $temporaryPath -Value $json -Encoding utf8
        if ([System.IO.File]::Exists($Path)) {
            # Windows PowerShell 5.1 requires a non-null backup path here.
            [System.IO.File]::Replace($temporaryPath, $Path, $backupPath)
        }
        else {
            [System.IO.File]::Move($temporaryPath, $Path)
        }
    }
    finally {
        foreach ($candidate in @($temporaryPath, $backupPath)) {
            if (Test-Path -LiteralPath $candidate) {
                Remove-Item -LiteralPath $candidate -Force
            }
        }
    }
}

function Get-TaskHash {
    param([Parameter(Mandatory)][string]$Value)

    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
        return ([System.BitConverter]::ToString($sha256.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
    }
}

function Update-WorktreeStatus {
    param(
        [Parameter(Mandatory)]$Metadata,
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$PinnedCommit
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        $Metadata.worktree_status = 'not_created'
        return
    }

    $headOutput = @(& git -C $Path rev-parse HEAD 2>$null)
    if ($LASTEXITCODE -eq 0 -and $headOutput.Count -gt 0) {
        $headCommit = ([string]$headOutput[-1]).Trim()
        $Metadata.head_commit = $headCommit
        $Metadata.commit_status = if ($headCommit -eq $PinnedCommit) { 'unchanged' } else { 'changed' }
    }
    else {
        $Metadata.commit_status = 'unknown'
    }

    $statusOutput = @(& git -C $Path status --porcelain=v1 --untracked-files=all 2>$null)
    if ($LASTEXITCODE -eq 0) {
        $statusLines = @($statusOutput | ForEach-Object { ([string]$_).TrimEnd() } | Where-Object { $_ })
        $Metadata.diff_status = if ($statusLines.Count -eq 0) { 'clean' } else { 'dirty' }
        $Metadata.worktree_status = $Metadata.diff_status
        $Metadata.diff_entries = $statusLines
    }
    else {
        $Metadata.diff_status = 'unknown'
        $Metadata.worktree_status = 'unknown'
    }
}

# Task names are used in refs and paths. Reject unsafe input instead of silently
# rewriting it, so callers can reliably identify and recover a run.
if ($Name -notmatch '^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?$') {
    throw 'Name must be 1-64 ASCII letters, digits, dots, underscores, or hyphens; it must start and end with a letter or digit.'
}

if ($Mode -eq 'ReadOnly' -and -not [string]::IsNullOrWhiteSpace($TestCommand)) {
    throw 'TestCommand is only allowed in Write mode because tests may modify the worktree.'
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'Git was not found on PATH.'
}
if (-not (Get-Command pi -ErrorAction SilentlyContinue)) {
    throw 'Pi was not found on PATH.'
}

$scriptRoot = Get-FullPath -Path $scriptRoot -BasePath (Get-Location).Path
# Discover the worktree root with .NET paths instead of parsing `git rev-parse`
# output. Windows PowerShell 5 can decode native UTF-8 output with the active
# OEM code page and corrupt repository paths containing Chinese characters.
$repositoryRoot = $scriptRoot
while (-not (Test-Path -LiteralPath (Join-Path $repositoryRoot '.git'))) {
    $parent = Split-Path -Parent $repositoryRoot
    if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $repositoryRoot) {
        throw 'Invoke-PiTask.ps1 must be run from inside a Git worktree.'
    }
    $repositoryRoot = $parent
}
& git -C $repositoryRoot rev-parse --is-inside-work-tree *> $null
if ($LASTEXITCODE -ne 0) {
    throw 'Invoke-PiTask.ps1 must be run from inside a Git worktree.'
}

$baseOutput = @(& git -C $repositoryRoot rev-parse --verify --end-of-options ("$BaseCommit^{commit}") 2>&1)
if ($LASTEXITCODE -ne 0 -or $baseOutput.Count -eq 1 -and [string]::IsNullOrWhiteSpace([string]$baseOutput[0])) {
    throw "BaseCommit does not resolve to a commit: $BaseCommit"
}
$resolvedBaseCommit = ([string]$baseOutput[-1]).Trim().ToLowerInvariant()
if ($resolvedBaseCommit -notmatch '^[0-9a-f]{40,64}$') {
    throw "Git returned an invalid commit id for BaseCommit: $BaseCommit"
}

if ([string]::IsNullOrWhiteSpace($RunRoot)) {
    $canonicalParent = Split-Path -Parent $repositoryRoot
    $canonicalName = Split-Path -Leaf $repositoryRoot
    $RunRoot = Join-Path $canonicalParent "$canonicalName-pi-task-runs"
}
else {
    $RunRoot = Get-FullPath -Path $RunRoot -BasePath $repositoryRoot
}

if ([string]::IsNullOrWhiteSpace($WorktreeRoot)) {
    $worktreeParent = Split-Path -Parent $repositoryRoot
    $worktreeContainerName = (Split-Path -Leaf $repositoryRoot) + '-pi-task-worktrees'
    $WorktreeRoot = Join-Path $worktreeParent $worktreeContainerName
}
else {
    $WorktreeRoot = Get-FullPath -Path $WorktreeRoot -BasePath $repositoryRoot
}

$repositoryPrefix = $repositoryRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
if ($WorktreeRoot.Equals($repositoryRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
    $WorktreeRoot.StartsWith($repositoryPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'WorktreeRoot must be outside the dispatching worktree.'
}

$startedAt = (Get-Date).ToUniversalTime()
$runId = $startedAt.ToString('yyyyMMddTHHmmssfffZ') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 12)
$branch = "pi/task/$Name/$runId"
$worktree = Join-Path $WorktreeRoot "$Name-$runId"
$runDirectory = Join-Path $RunRoot "$Name-$runId"
$resultDirectory = Join-Path $runDirectory 'results'
$sessionDirectory = Join-Path $runDirectory 'sessions'
$resultFile = Join-Path $resultDirectory 'pi-output.txt'
$testResultFile = Join-Path $resultDirectory 'test-output.txt'
$metadataFile = Join-Path $resultDirectory 'metadata.json'

New-Item -ItemType Directory -Force -Path $WorktreeRoot, $resultDirectory, $sessionDirectory | Out-Null

$quotedWorktree = ConvertTo-SingleQuotedPowerShellLiteral -Value $worktree
$quotedSession = ConvertTo-SingleQuotedPowerShellLiteral -Value $sessionDirectory
$recoveryPiOptions = if ($Mode -eq 'ReadOnly') {
    '--no-approve --no-extensions --tools read,grep,find,ls'
}
else {
    '--approve'
}

$metadata = [ordered]@{
    schema_version = 1
    run_id = $runId
    name = $Name
    task_sha256 = Get-TaskHash -Value $Task
    mode = $Mode
    status = 'preparing'
    started_at = $startedAt.ToString('o')
    finished_at = $null
    base_commit = $resolvedBaseCommit
    branch = $branch
    worktree = $worktree
    worktree_status = 'not_created'
    head_commit = $null
    commit_status = 'not_checked'
    diff_status = 'not_checked'
    diff_entries = @()
    test_command_sha256 = if ([string]::IsNullOrWhiteSpace($TestCommand)) { $null } else { Get-TaskHash -Value $TestCommand }
    test_status = 'not_run'
    test_exit_code = $null
    pi_exit_code = $null
    exit_code = $null
    result_directory = $resultDirectory
    result_file = $resultFile
    test_result_file = if ([string]::IsNullOrWhiteSpace($TestCommand)) { $null } else { $testResultFile }
    session_directory = $sessionDirectory
    metadata_file = $metadataFile
    recovery_command = "Set-Location -LiteralPath $quotedWorktree; pi $recoveryPiOptions --session-dir $quotedSession --continue"
    recovery_note = 'The dispatcher never removes task worktrees. Inspect and recover this worktree manually; never remove it while it has uncommitted changes.'
    error = $null
}
Write-JsonAtomic -Value $metadata -Path $metadataFile

$exitCode = 1
try {
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $worktreeOutput = @(& git -C $repositoryRoot worktree add -b $branch -- $worktree $resolvedBaseCommit 2>&1)
        $worktreeExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($worktreeExitCode -ne 0) {
        throw ('git worktree add failed: ' + (($worktreeOutput | ForEach-Object { [string]$_ }) -join [Environment]::NewLine))
    }

    $metadata.status = 'running'
    $metadata.worktree_status = 'clean'
    $metadata.head_commit = $resolvedBaseCommit
    $metadata.commit_status = 'unchanged'
    $metadata.diff_status = 'clean'
    Write-JsonAtomic -Value $metadata -Path $metadataFile

    $accessInstruction = if ($Mode -eq 'ReadOnly') {
        '这是只读任务。只允许检查和分析，不得修改文件或运行可能产生文件的命令。'
    }
    else {
        '这是可写任务。只修改任务必要的文件，并运行相关验证。'
    }

    $prompt = @"
在此独立 Git worktree 中完成以下任务：

$Task

固定上下文：
- Base commit: $resolvedBaseCommit
- Branch: $branch
- Worktree: $worktree
- Mode: $Mode

$accessInstruction
禁止提交、合并、推送或部署；不得删除 worktree。失败时保留现场供恢复。
先检查现有代码和测试，只做必要工作。完成后用中文简洁说明：完成内容、修改文件、验证命令及结果、未解决风险。不要泄露密钥、令牌或个人数据。
"@

    $piArguments = @('--session-dir', $sessionDirectory, '--name', "$Name-$runId", '--print')
    if ($Mode -eq 'ReadOnly') {
        $piArguments = @('--no-approve', '--no-extensions', '--tools', 'read,grep,find,ls') + $piArguments
    }
    else {
        $piArguments = @('--approve') + $piArguments
    }
    $piArguments += $prompt

    Write-Host "[Pi] Run ID: $runId" -ForegroundColor Cyan
    Write-Host "[Pi] Worktree: $worktree" -ForegroundColor Cyan
    Write-Host "[Pi] Branch: $branch" -ForegroundColor Cyan
    Write-Host "[Pi] Base commit: $resolvedBaseCommit" -ForegroundColor Cyan
    Write-Host "[Pi] Mode: $Mode" -ForegroundColor Cyan

    Push-Location -LiteralPath $worktree
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if (Test-Path -LiteralPath $resultFile) { Remove-Item -LiteralPath $resultFile -Force }
        & pi @piArguments 2>&1 | ForEach-Object {
            $line = [string]$_
            Write-Host $line
            [System.IO.File]::AppendAllText($resultFile, $line + [Environment]::NewLine, $utf8NoBom)
        }
        $piExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
        Pop-Location
    }
    if ($null -eq $piExitCode) {
        $piExitCode = 1
    }
    $metadata.pi_exit_code = [int]$piExitCode

    if (-not [string]::IsNullOrWhiteSpace($TestCommand)) {
        $metadata.test_status = 'running'
        Write-JsonAtomic -Value $metadata -Path $metadataFile
        $powerShellExecutable = (Get-Process -Id $PID).Path
        Push-Location -LiteralPath $worktree
        $previousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            if (Test-Path -LiteralPath $testResultFile) { Remove-Item -LiteralPath $testResultFile -Force }
            & $powerShellExecutable -NoLogo -NoProfile -NonInteractive -Command $TestCommand 2>&1 |
                ForEach-Object {
                    $line = [string]$_
                    Write-Host $line
                    [System.IO.File]::AppendAllText($testResultFile, $line + [Environment]::NewLine, $utf8NoBom)
                }
            $testExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
            Pop-Location
        }
        if ($null -eq $testExitCode) {
            $testExitCode = 1
        }
        $metadata.test_exit_code = [int]$testExitCode
        $metadata.test_status = if ($testExitCode -eq 0) { 'passed' } else { 'failed' }
    }

    Update-WorktreeStatus -Metadata $metadata -Path $worktree -PinnedCommit $resolvedBaseCommit

    if ($piExitCode -ne 0) {
        $metadata.status = 'failed'
        $exitCode = [int]$piExitCode
    }
    elseif ($metadata.test_status -eq 'failed') {
        $metadata.status = 'failed'
        $exitCode = if ($metadata.test_exit_code -gt 0 -and $metadata.test_exit_code -le 255) { [int]$metadata.test_exit_code } else { 1 }
    }
    elseif ($metadata.commit_status -eq 'changed' -or ($Mode -eq 'ReadOnly' -and $metadata.diff_status -eq 'dirty')) {
        $metadata.status = 'policy_violation'
        $exitCode = 2
    }
    else {
        $metadata.status = 'succeeded'
        $exitCode = 0
    }
}
catch {
    $metadata.status = 'failed'
    $metadata.error = $_.Exception.Message
    if ($metadata.pi_exit_code -is [int] -and $metadata.pi_exit_code -ne 0) {
        $exitCode = [int]$metadata.pi_exit_code
    }
    Update-WorktreeStatus -Metadata $metadata -Path $worktree -PinnedCommit $resolvedBaseCommit
}
finally {
    $metadata.exit_code = $exitCode
    $metadata.finished_at = (Get-Date).ToUniversalTime().ToString('o')
    Write-JsonAtomic -Value $metadata -Path $metadataFile
}

Write-Host "`n[Pi] Status: $($metadata.status)" -ForegroundColor $(if ($exitCode -eq 0) { 'Green' } else { 'Red' })
Write-Host "[Pi] Worktree: $worktree"
Write-Host "[Pi] Worktree status: $($metadata.worktree_status)"
Write-Host "[Pi] Branch: $branch"
Write-Host "[Pi] Base commit: $resolvedBaseCommit"
Write-Host "[Pi] HEAD commit: $($metadata.head_commit)"
Write-Host "[Pi] Commit status: $($metadata.commit_status)"
Write-Host "[Pi] Diff status: $($metadata.diff_status)"
Write-Host "[Pi] Test status: $($metadata.test_status)"
Write-Host "[Pi] Result: $resultFile"
Write-Host "[Pi] Metadata: $metadataFile"
if ($exitCode -ne 0) {
    Write-Host "[Pi] Recovery: $($metadata.recovery_command)" -ForegroundColor Yellow
}

exit $exitCode
