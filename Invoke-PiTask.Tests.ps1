[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

Describe 'Invoke-PiTask task isolation' {
    BeforeAll {
        $sourceInvoke = Join-Path $PSScriptRoot 'Invoke-PiTask.ps1'
        $sourceOpen = Join-Path $PSScriptRoot 'Open-Pi-V3.ps1'

        function New-TaskFixture {
            param([string]$Name)

            $fixtureRoot = Join-Path $TestDrive $Name
            $repository = Join-Path $fixtureRoot 'repository'
            $bin = Join-Path $fixtureRoot 'bin'
            New-Item -ItemType Directory -Force -Path $repository, $bin | Out-Null
            Copy-Item -LiteralPath $sourceInvoke, $sourceOpen -Destination $repository

            & git -C $repository init --quiet
            & git -C $repository config user.email 'task-runner-tests@example.invalid'
            & git -C $repository config user.name 'Task Runner Tests'
            Set-Content -LiteralPath (Join-Path $repository 'tracked.txt') -Value 'base' -Encoding utf8
            & git -C $repository add --all
            & git -C $repository commit --quiet -m 'base'
            $baseCommit = (& git -C $repository rev-parse HEAD).Trim()

            $fakePiSource = @'
using System;
using System.IO;

public static class FakePi
{
    public static int Main(string[] args)
    {
        var log = Environment.GetEnvironmentVariable("FAKE_PI_ARGS");
        if (!String.IsNullOrEmpty(log))
        {
            File.WriteAllLines(log, args);
        }

        var fileToCreate = Environment.GetEnvironmentVariable("FAKE_PI_TOUCH");
        if (!String.IsNullOrEmpty(fileToCreate))
        {
            File.WriteAllText(Path.Combine(Environment.CurrentDirectory, fileToCreate), "agent change");
        }

        Console.WriteLine("UTF8-RESULT-\u4F60\u597D");
        var exitCode = Environment.GetEnvironmentVariable("FAKE_PI_EXIT");
        return String.IsNullOrEmpty(exitCode) ? 0 : Int32.Parse(exitCode);
    }
}
'@
            Add-Type -TypeDefinition $fakePiSource -OutputAssembly (Join-Path $bin 'pi.exe') -OutputType ConsoleApplication

            return [pscustomobject]@{
                Root = $fixtureRoot
                Repository = $repository
                Script = Join-Path $repository 'Invoke-PiTask.ps1'
                BaseCommit = $baseCommit
                RunRoot = Join-Path $fixtureRoot 'runs'
                WorktreeRoot = Join-Path $fixtureRoot 'worktrees'
                Bin = $bin
                ArgsLog = Join-Path $fixtureRoot 'pi-args.txt'
            }
        }

        function Invoke-TaskScript {
            param(
                [Parameter(Mandatory)]$Fixture,
                [string]$Name = 'safe-task',
                [ValidateSet('ReadOnly', 'Write')][string]$Mode = 'ReadOnly',
                [string]$BaseCommit,
                [string]$TestCommand
            )

            if (-not $BaseCommit) {
                $BaseCommit = $Fixture.BaseCommit
            }

            $oldPath = $env:PATH
            $oldArgs = $env:FAKE_PI_ARGS
            $oldTouch = $env:FAKE_PI_TOUCH
            $oldExit = $env:FAKE_PI_EXIT
            try {
                $env:PATH = "$($Fixture.Bin);$oldPath"
                $env:FAKE_PI_ARGS = $Fixture.ArgsLog
                if ($null -eq $env:FAKE_PI_EXIT) {
                    $env:FAKE_PI_EXIT = '0'
                }

                $arguments = @(
                    '-NoLogo', '-NoProfile', '-NonInteractive',
                    '-File', $Fixture.Script,
                    '-Task', 'inspect repository',
                    '-Name', $Name,
                    '-Mode', $Mode,
                    '-BaseCommit', $BaseCommit,
                    '-RunRoot', $Fixture.RunRoot,
                    '-WorktreeRoot', $Fixture.WorktreeRoot
                )
                if ($TestCommand) {
                    $arguments += @('-TestCommand', $TestCommand)
                }

                $previousErrorActionPreference = $ErrorActionPreference
                $ErrorActionPreference = 'Continue'
                try {
                    $output = (& powershell.exe @arguments 2>&1 | Out-String)
                    $childExitCode = $LASTEXITCODE
                }
                finally {
                    $ErrorActionPreference = $previousErrorActionPreference
                }
                return [pscustomobject]@{ ExitCode = $childExitCode; Output = $output }
            }
            finally {
                $env:PATH = $oldPath
                $env:FAKE_PI_ARGS = $oldArgs
                $env:FAKE_PI_TOUCH = $oldTouch
                $env:FAKE_PI_EXIT = $oldExit
            }
        }

        function Get-OnlyMetadata {
            param([Parameter(Mandatory)]$Fixture)
            $files = @(Get-ChildItem -LiteralPath $Fixture.RunRoot -Filter 'metadata.json' -File -Recurse)
            [void]($files.Count | Should Be 1)
            return (Get-Content -LiteralPath $files[0].FullName -Raw | ConvertFrom-Json)
        }
    }

    It 'rejects unsafe task names instead of rewriting them' {
        $fixture = New-TaskFixture 'unsafe-name'
        $result = Invoke-TaskScript -Fixture $fixture -Name '../escape'

        $result.ExitCode | Should Not Be 0
        (Test-Path -LiteralPath $fixture.WorktreeRoot) | Should Be $false
        (Test-Path -LiteralPath $fixture.RunRoot) | Should Be $false
    }

    It 'supports repository paths containing Unicode characters' {
        $unicodeSuffix = [string]([char]0x9c7c) + [string]([char]0x9ea6) + [string]([char]0x591a)
        $fixture = New-TaskFixture ("unicode-$unicodeSuffix")
        $result = Invoke-TaskScript -Fixture $fixture -Mode ReadOnly

        $result.ExitCode | Should Be 0
        $metadata = Get-OnlyMetadata -Fixture $fixture
        $metadata.status | Should Be 'succeeded'
        (Test-Path -LiteralPath $metadata.worktree) | Should Be $true
    }

    It 'pins the requested base commit in a dedicated branch and worktree' {
        $fixture = New-TaskFixture 'fixed-base'
        Set-Content -LiteralPath (Join-Path $fixture.Repository 'after-base.txt') -Value 'later' -Encoding utf8
        & git -C $fixture.Repository add --all
        & git -C $fixture.Repository commit --quiet -m 'later'

        $result = Invoke-TaskScript -Fixture $fixture -Mode ReadOnly -BaseCommit $fixture.BaseCommit
        $metadata = Get-OnlyMetadata -Fixture $fixture

        $result.ExitCode | Should Be 0
        $metadata.base_commit | Should Be $fixture.BaseCommit
        $metadata.head_commit | Should Be $fixture.BaseCommit
        $metadata.branch | Should Match '^pi/task/safe-task/'
        (Test-Path -LiteralPath $metadata.worktree) | Should Be $true
        (Test-Path -LiteralPath (Join-Path $metadata.worktree 'after-base.txt')) | Should Be $false
        (& git -C $metadata.worktree rev-parse --abbrev-ref HEAD).Trim() | Should Be $metadata.branch
    }

    It 'uses read-only Pi tools and isolated result and session directories by default' {
        $fixture = New-TaskFixture 'read-only'
        $first = Invoke-TaskScript -Fixture $fixture -Name 'audit-one'
        $firstMetadata = Get-OnlyMetadata -Fixture $fixture
        $firstArgs = @(Get-Content -LiteralPath $fixture.ArgsLog)

        $second = Invoke-TaskScript -Fixture $fixture -Name 'audit-two'
        $metadataFiles = @(Get-ChildItem -LiteralPath $fixture.RunRoot -Filter 'metadata.json' -File -Recurse)
        $allMetadata = @($metadataFiles | ForEach-Object { Get-Content -LiteralPath $_.FullName -Raw | ConvertFrom-Json })

        $first.ExitCode | Should Be 0
        $second.ExitCode | Should Be 0
        ($firstArgs -contains '--no-approve') | Should Be $true
        ($firstArgs -contains '--tools') | Should Be $true
        ($firstArgs -contains 'read,grep,find,ls') | Should Be $true
        $allMetadata.Count | Should Be 2
        (@($allMetadata.session_directory | Select-Object -Unique)).Count | Should Be 2
        (@($allMetadata.result_directory | Select-Object -Unique)).Count | Should Be 2
        (@($allMetadata.branch | Select-Object -Unique)).Count | Should Be 2
        (Get-ChildItem -LiteralPath $fixture.RunRoot -Filter 'latest.json' -File -Recurse).Count | Should Be 0
        $firstMetadata.test_status | Should Be 'not_run'
        $resultBytes = [System.IO.File]::ReadAllBytes($firstMetadata.result_file)
        ($resultBytes.Length -gt 2 -and -not ($resultBytes[0] -eq 0xff -and $resultBytes[1] -eq 0xfe)) | Should Be $true
        $expectedUnicodeOutput = 'UTF8-RESULT-' + [string]([char]0x4f60) + [string]([char]0x597d)
        [System.IO.File]::ReadAllText($firstMetadata.result_file, [System.Text.Encoding]::UTF8) | Should Match $expectedUnicodeOutput
    }

    It 'does not commit agent changes and reports commit diff and test status' {
        $fixture = New-TaskFixture 'write-status'
        $env:FAKE_PI_TOUCH = 'agent-change.txt'
        $result = Invoke-TaskScript -Fixture $fixture -Mode Write -TestCommand 'exit 0'
        $metadata = Get-OnlyMetadata -Fixture $fixture
        $sourceHead = (& git -C $fixture.Repository rev-parse HEAD).Trim()

        $result.ExitCode | Should Be 0
        $metadata.mode | Should Be 'Write'
        $metadata.commit_status | Should Be 'unchanged'
        $metadata.diff_status | Should Be 'dirty'
        $metadata.test_status | Should Be 'passed'
        $metadata.head_commit | Should Be $fixture.BaseCommit
        $sourceHead | Should Be $fixture.BaseCommit
        (Test-Path -LiteralPath (Join-Path $fixture.Repository 'agent-change.txt')) | Should Be $false
        $result.Output | Should Match 'Worktree:'
        $result.Output | Should Match 'Branch:'
        $result.Output | Should Match 'Base commit:'
        $result.Output | Should Match 'Diff status:'
        $result.Output | Should Match 'Test status:'
    }

    It 'preserves a dirty worktree and atomically records metadata when Pi fails' {
        $fixture = New-TaskFixture 'failure-recovery'
        $env:FAKE_PI_TOUCH = 'partial-change.txt'
        $env:FAKE_PI_EXIT = '7'
        $result = Invoke-TaskScript -Fixture $fixture -Mode Write
        $metadata = Get-OnlyMetadata -Fixture $fixture

        $result.ExitCode | Should Be 7
        $metadata.status | Should Be 'failed'
        $metadata.pi_exit_code | Should Be 7
        $metadata.diff_status | Should Be 'dirty'
        (Test-Path -LiteralPath $metadata.worktree) | Should Be $true
        (Test-Path -LiteralPath (Join-Path $metadata.worktree 'partial-change.txt')) | Should Be $true
        $metadata.recovery_command | Should Match 'Set-Location'
        @(Get-ChildItem -LiteralPath $metadata.result_directory -File | Where-Object { $_.Extension -in @('.tmp', '.bak') }).Count | Should Be 0
    }

    It 'has valid PowerShell syntax in both launch scripts' {
        foreach ($path in @($sourceInvoke, $sourceOpen)) {
            $tokens = $null
            $errors = $null
            [void][System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
            @($errors).Count | Should Be 0
        }
    }
}
