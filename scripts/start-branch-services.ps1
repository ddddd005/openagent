#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('main', 'develop')]
    [string]$Branch,
    [string]$PythonPath,
    [ValidateRange(1024, 65535)]
    [int]$BackendPort = 8765,
    [ValidateRange(1024, 65535)]
    [int]$FrontendPort = 5178,
    [string]$DatabasePath,
    [string]$Mode,
    [switch]$NoBrowser,
    [switch]$CheckOnly,
    [switch]$ResolveOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$entryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$savedOutputEncoding = [Console]::OutputEncoding
$savedPipelineEncoding = $OutputEncoding

function Read-Git {
    param([string]$Root, [string[]]$Arguments)
    $output = @(& git.exe -C $Root @Arguments)
    if ($LASTEXITCODE -ne 0) { throw "Git validation failed in $Root." }
    return $output
}

try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $OutputEncoding = [Console]::OutputEncoding
    $currentBranch = (Read-Git $entryRoot @('symbolic-ref', '--short', 'HEAD')) -join ''
    $selectedRoot = $entryRoot
    if ($Branch -eq 'main') {
        $mainCommit = (Read-Git $entryRoot @('rev-parse', 'refs/heads/main')) -join ''
        $roots = [System.Collections.Generic.List[string]]::new()
        $candidate = $null
        foreach ($line in (Read-Git $entryRoot @('-c', 'core.quotepath=false', 'worktree', 'list', '--porcelain'))) {
            if ($line.StartsWith('worktree ')) { $candidate = $line.Substring(9) }
            if ($line -eq 'branch refs/heads/main' -and $candidate) { $roots.Add($candidate) }
        }
        if ($roots.Count -ne 1) {
            throw 'Stable startup requires exactly one checked-out main worktree. Prepare it explicitly with git worktree add .local/stable-main main. No branch was switched.'
        }
        $selectedRoot = [System.IO.Path]::GetFullPath($roots[0])
        $selectedBranch = (Read-Git $selectedRoot @('symbolic-ref', '--short', 'HEAD')) -join ''
        $selectedCommit = (Read-Git $selectedRoot @('rev-parse', 'HEAD')) -join ''
        if ($selectedBranch -ne 'main' -or $selectedCommit -ne $mainCommit) {
            throw 'Stable checkout does not match local main. No fallback to develop is allowed.'
        }
        $dirty = @(Read-Git $selectedRoot @('status', '--porcelain', '--untracked-files=normal'))
        if ($dirty.Count -gt 0) { throw 'Stable main checkout has local changes. Resolve them explicitly before startup.' }
    } else {
        if ($currentBranch -ne 'develop') { throw 'Development startup requires this entry checkout to be on develop.' }
        $selectedCommit = (Read-Git $selectedRoot @('rev-parse', 'HEAD')) -join ''
        if (-not $PSBoundParameters.ContainsKey('BackendPort')) { $BackendPort = 8766 }
        if (-not $PSBoundParameters.ContainsKey('FrontendPort')) { $FrontendPort = 5179 }
        if ($BackendPort -in @(8765, 5178) -or $FrontendPort -in @(8765, 5178)) {
            throw 'Development startup cannot use stable ports 8765 or 5178; browser storage must remain isolated.'
        }
    }
    if ($BackendPort -eq $FrontendPort) { throw 'BackendPort and FrontendPort must differ.' }
    # Preserve the original user database for stable startup; development gets a new namespace.
    $stableDatabase = [System.IO.Path]::GetFullPath((Join-Path $entryRoot '.local\dev\workflow.sqlite'))
    if (-not $DatabasePath) {
        $DatabasePath = if ($Branch -eq 'main') { $stableDatabase } else {
            Join-Path $entryRoot '.local\develop\workflow.sqlite'
        }
    } elseif (-not [System.IO.Path]::IsPathRooted($DatabasePath)) {
        $DatabasePath = Join-Path $entryRoot $DatabasePath
    }
    $DatabasePath = [System.IO.Path]::GetFullPath($DatabasePath)
    if ($Branch -eq 'develop' -and $DatabasePath -eq $stableDatabase) {
        throw 'Development startup cannot open the default stable database.'
    }
    $launcher = Join-Path $selectedRoot 'scripts\start-services.ps1'
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw "Selected checkout has no service launcher: $launcher"
    }
    if ($ResolveOnly) {
        [ordered]@{
            schema = 'openagent.branch-launcher@1'
            branch = $Branch
            git_commit = $selectedCommit
            entry_root = $entryRoot
            repository_root = $selectedRoot
            database_path = $DatabasePath
            backend_port = $BackendPort
            frontend_port = $FrontendPort
        } | ConvertTo-Json -Compress
        exit 0
    }
    Write-Host "Selected branch: $Branch ($selectedCommit)"
    Write-Host "Selected checkout: $selectedRoot"
    $launchArguments = @('-BackendPort', $BackendPort.ToString(), '-FrontendPort',
        $FrontendPort.ToString(), '-DatabasePath', $DatabasePath)
    if ($PythonPath) {
        if (-not [System.IO.Path]::IsPathRooted($PythonPath)) { $PythonPath = Join-Path $selectedRoot $PythonPath }
        $launchArguments += @('-PythonPath', $PythonPath)
    }
    if ($PSBoundParameters.ContainsKey('Mode')) { $launchArguments += @('-Mode', $Mode) }
    if ($NoBrowser) { $launchArguments += '-NoBrowser' }
    if ($CheckOnly) { $launchArguments += '-CheckOnly' }
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $launcher @launchArguments
    exit $LASTEXITCODE
} catch {
    Write-Host "[START FAILED] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    [Console]::OutputEncoding = $savedOutputEncoding
    $OutputEncoding = $savedPipelineEncoding
}
