#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$MetadataPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'service-processes.ps1')
$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$metadataRoot = Join-Path $repositoryRoot '.local\service-starts'

try {
    $MetadataPath = [System.IO.Path]::GetFullPath($MetadataPath)
    if (-not $MetadataPath.StartsWith($metadataRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'MetadataPath must be this checkout''s .local\service-starts\<launch>\services.json.'
    }
    $relative = $MetadataPath.Substring($metadataRoot.Length).TrimStart('\')
    if ($relative -notmatch '^[^\\]+\\services\.json$') {
        throw 'MetadataPath must be this checkout''s .local\service-starts\<launch>\services.json.'
    }
    $metadata = Get-Content -LiteralPath $MetadataPath -Raw | ConvertFrom-Json
    if ($metadata.schema -ne 'openagent.dev-services@1' -or
        -not [string]::Equals($metadata.repository_root, $repositoryRoot,
            [System.StringComparison]::OrdinalIgnoreCase) -or
        -not [string]::Equals($metadata.backend_source, (Join-Path $repositoryRoot 'backend\src'),
            [System.StringComparison]::OrdinalIgnoreCase) -or
        -not [string]::Equals($metadata.frontend_root, (Join-Path $repositoryRoot 'frontend'),
            [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Launch metadata does not belong to this OpenAgent checkout.'
    }
    $identities = @($metadata.processes)
    if ($identities.Count -eq 0 -or $identities.Count -gt 64) { throw 'Launch metadata has no bounded process set.' }
    $pids = @{}
    foreach ($identity in $identities) {
        if ([int]$identity.pid -le 0 -or $pids.ContainsKey([int]$identity.pid) -or
            $identity.role -notin @('backend', 'frontend') -or
            [int]$identity.depth -lt 0 -or [int]$identity.depth -gt 16 -or
            [long]$identity.start_time_ticks -le 0 -or
            -not $identity.executable_path -or -not $identity.command_line) {
            throw 'Invalid process identity in launch metadata.'
        }
        $pids[[int]$identity.pid] = $true
        if ($identity.depth -eq 0 -and $identity.role -eq 'backend' -and
            ($identity.command_line -notmatch '-m\s+phase1_agent\.server(?:\s|$)' -or
             -not $identity.command_line.Contains([string]$metadata.database_path))) {
            throw 'Recorded backend command does not match this launch.'
        }
        if ($identity.depth -eq 0 -and $identity.role -eq 'frontend' -and
            -not $identity.command_line.Contains((Join-Path $repositoryRoot 'frontend\node_modules\vite\bin\vite.js'))) {
            throw 'Recorded frontend command does not match this checkout.'
        }
    }
    $identities = @(Get-ServiceProcessTree -Identities $identities)
    Stop-ServiceProcessTree -Identities $identities
    $metadata.state = 'stopped'
    $metadata | Add-Member -NotePropertyName stopped_at -NotePropertyValue ((Get-Date).ToUniversalTime().ToString('o')) -Force
    $metadata.processes = $identities
    $metadata | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $MetadataPath -Encoding UTF8
    [ordered]@{
        schema = 'openagent.dev-stop@1'
        metadata_path = $MetadataPath
        state = 'stopped'
        database_path = $metadata.database_path
        database_preserved = $true
    } | ConvertTo-Json -Compress
} catch {
    Write-Host "[STOP FAILED] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
