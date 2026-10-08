#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$PythonPath,
    [ValidateRange(1024, 65535)]
    [int]$BackendPort = 8765,
    [ValidateRange(1024, 65535)]
    [int]$FrontendPort = 5178,
    [string]$DatabasePath,
    [string]$Mode,
    [switch]$NoBrowser,
    [switch]$CheckOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'service-processes.ps1')
$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$backendRoot = Join-Path $repositoryRoot 'backend'
$sourceRoot = Join-Path $backendRoot 'src'
$frontendRoot = Join-Path $repositoryRoot 'frontend'
$vitePath = Join-Path $frontendRoot 'node_modules\vite\bin\vite.js'
$ownedProcesses = [System.Collections.Generic.List[object]]::new()
$startedProcessHandles = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
$startedProcessCreation = @{}
$logDirectory = $null
$metadataPath = $null
$savedEnvironment = @{}
foreach ($name in @('PYTHONPATH', 'PYTHONIOENCODING', 'PYTHONDONTWRITEBYTECODE',
    'OPENAGENT_SOURCE_ROOT', 'OPENAGENT_API_PORT', 'VITE_CHAT_UI_URL')) {
    $savedEnvironment[$name] = [System.Environment]::GetEnvironmentVariable($name, 'Process')
}

function Update-OwnedProcesses {
    $tree = @(Get-ServiceProcessTree -Identities $ownedProcesses.ToArray())
    $ownedProcesses.Clear()
    foreach ($identity in $tree) { $ownedProcesses.Add($identity) }
}

function Write-ServiceMetadata {
    param([string]$State)
    Update-OwnedProcesses
    [ordered]@{
        schema = 'openagent.dev-services@1'
        repository_root = $repositoryRoot
        backend_source = $sourceRoot
        frontend_root = $frontendRoot
        database_path = $DatabasePath
        backend_port = $BackendPort
        frontend_port = $FrontendPort
        state = $State
        started_at = (Get-Date).ToUniversalTime().ToString('o')
        processes = @($ownedProcesses.ToArray())
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $metadataPath -Encoding UTF8
}

function Wait-ServiceReady {
    param(
        [System.Diagnostics.Process]$Process,
        [string]$Uri,
        [switch]$Health
    )
    $deadline = [DateTime]::UtcNow.AddSeconds(40)
    while ([DateTime]::UtcNow -lt $deadline) {
        Update-OwnedProcesses
        $Process.Refresh()
        if ($Process.HasExited) { throw "Service PID $($Process.Id) exited with code $($Process.ExitCode)." }
        try {
            if ($Health) {
                $response = Invoke-RestMethod -Uri $Uri -TimeoutSec 2
                if ($response.status -eq 'ok') { return }
            } else {
                $response = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 2
                if ($response.StatusCode -eq 200) { return }
            }
        } catch {
            # Loopback connections can be refused while the new process starts.
        }
        Start-Sleep -Milliseconds 300
    }
    throw "Service readiness timed out: $Uri"
}

try {
    if ($PSBoundParameters.ContainsKey('Mode')) {
        throw '-Mode is retired. Model calls are controlled by each workflow node; this launcher does not force offline mode.'
    }
    if ($BackendPort -eq $FrontendPort) { throw 'BackendPort and FrontendPort must differ.' }
    Get-ServiceProcessIdentity -Process (Get-Process -Id $PID) -Role 'launcher' | Out-Null
    if (-not $PythonPath) {
        $localPython = Join-Path $repositoryRoot '.venv\Scripts\python.exe'
        if (Test-Path -LiteralPath $localPython -PathType Leaf) {
            $PythonPath = $localPython
        } else {
            $PythonPath = (Get-Command python.exe -CommandType Application -ErrorAction Stop |
                Select-Object -First 1).Source
        }
    } elseif (-not [System.IO.Path]::IsPathRooted($PythonPath)) {
        $PythonPath = Join-Path $repositoryRoot $PythonPath
    }
    $PythonPath = [System.IO.Path]::GetFullPath($PythonPath)
    $nodePath = (Get-Command node.exe -CommandType Application -ErrorAction Stop |
        Select-Object -First 1).Source
    foreach ($required in @($PythonPath, $vitePath, (Join-Path $sourceRoot 'phase1_agent\server.py'),
        (Join-Path $frontendRoot 'vite.config.ts'), (Join-Path $frontendRoot 'package.json'))) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "Missing local dependency or source: $required. Install this checkout's dependencies explicitly."
        }
    }
    if (-not $DatabasePath) {
        $DatabasePath = Join-Path $repositoryRoot '.local\dev\workflow.sqlite'
    } elseif (-not [System.IO.Path]::IsPathRooted($DatabasePath)) {
        $DatabasePath = Join-Path $repositoryRoot $DatabasePath
    }
    $DatabasePath = [System.IO.Path]::GetFullPath($DatabasePath)
    if (Test-Path -LiteralPath $DatabasePath -PathType Container) {
        throw "DatabasePath must be a file, not a directory: $DatabasePath"
    }
    $listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    $busyPorts = @($listeners | Where-Object { $_.Port -in @($BackendPort, $FrontendPort) } |
        Select-Object -ExpandProperty Port -Unique)
    if ($busyPorts.Count -gt 0) {
        throw "Ports already in use: $($busyPorts -join ', '). Stop their known owners first. No existing process will be terminated."
    }

    $env:PYTHONPATH = $sourceRoot
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:OPENAGENT_SOURCE_ROOT = $sourceRoot
    $env:OPENAGENT_API_PORT = $BackendPort.ToString()
    $env:VITE_CHAT_UI_URL = "http://127.0.0.1:$BackendPort/"
    $checkCode = @'
import json, os
from pathlib import Path
import openai, httpx, jsonschema
import phase1_agent, phase1_agent.server as server
from phase1_agent import builtin_packages
from phase1_agent.capability_packages import CapabilityPackageLoader
source = Path(os.environ["OPENAGENT_SOURCE_ROOT"]).resolve()
assert Path(phase1_agent.__file__).resolve() == source / "phase1_agent" / "__init__.py", "Backend package source mismatch"
assert Path(server.__file__).resolve() == source / "phase1_agent" / "server.py", "Backend server source mismatch"
assert Path(builtin_packages.__file__).resolve() == source / "phase1_agent" / "builtin_packages.py", "Builtin package source mismatch"
loaded = CapabilityPackageLoader(builtin_packages.builtin_capability_packages()).load(builtin_packages.DEFAULT_PACKAGES)
tavern_package = {"package_id": "workflow.tavern", "version": builtin_packages.DEFAULT_PACKAGES["workflow.tavern"]}
assert tavern_package in loaded.package_lock, "Current tavern package missing"
tavern_manifest = next(manifest for manifest in loaded.package_manifests
                       if manifest["package_id"] == tavern_package["package_id"] and manifest["version"] == tavern_package["version"])
tavern_nodes = tavern_manifest["exports"]["nodes"]
assert {"lorebook.global-reference", "lorebook.global-activate", "tavern.chat.output", "tavern.chat.append",
        "tavern.chat.presentation"} <= {node["component_id"] for node in tavern_nodes}, "Current tavern node directory incomplete"
assert all(loaded.registry.get(node["component_id"], node["component_version"]) for node in tavern_nodes)
print(json.dumps({"server_module": str(Path(server.__file__).resolve()), "package_lock": list(loaded.package_lock),
                  "tavern_package": tavern_package,
                  "tavern_nodes": [{"component_id": node["component_id"], "component_version": node["component_version"]}
                                   for node in tavern_nodes]}, ensure_ascii=True))
'@
    Push-Location -LiteralPath $backendRoot
    try {
        $checkOutput = @($checkCode | & $PythonPath -B -)
        if ($LASTEXITCODE -ne 0) { throw 'Current backend source import or package registration failed. Check Python dependencies.' }
        $sourceCheck = ($checkOutput -join "`n") | ConvertFrom-Json
    } finally {
        Pop-Location
    }
    Push-Location -LiteralPath $frontendRoot
    try {
        & $nodePath --input-type=module -e "await Promise.all(['vite', '@vitejs/plugin-vue', 'vue', 'pinia'].map(name => import(name)));"
        if ($LASTEXITCODE -ne 0) { throw 'Current frontend dependencies cannot load. Check Node and local node_modules.' }
    } finally {
        Pop-Location
    }
    $result = [ordered]@{
        schema = 'openagent.dev-launcher@1'
        check_only = [bool]$CheckOnly
        repository_root = $repositoryRoot
        backend_source = $sourceRoot
        server_module = $sourceCheck.server_module
        python_path = $PythonPath
        node_path = $nodePath
        frontend_root = $frontendRoot
        database_path = $DatabasePath
        backend_port = $BackendPort
        frontend_port = $FrontendPort
        package_lock = @($sourceCheck.package_lock)
        tavern_package = $sourceCheck.tavern_package
        tavern_nodes = @($sourceCheck.tavern_nodes)
    }
    if ($CheckOnly) {
        $result | ConvertTo-Json -Depth 8 -Compress
        exit 0
    }

    $launchId = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
    $logDirectory = Join-Path $repositoryRoot ('.local\service-starts\' + $launchId)
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    New-Item -ItemType Directory -Path (Split-Path $DatabasePath -Parent) -Force | Out-Null
    $metadataPath = Join-Path $logDirectory 'services.json'
    Write-Host "Backend source: $sourceRoot"
    Write-Host "Frontend source: $frontendRoot"
    Write-Host "Database: $DatabasePath"
    Write-Host 'Workflow nodes control model calls. No offline mode is forced.'
    $backendProcess = Start-Process -FilePath $PythonPath -WorkingDirectory $backendRoot -WindowStyle Normal -PassThru `
        -ArgumentList @('-B', '-u', '-m', 'phase1_agent.server', '--port', $BackendPort.ToString(),
            '--database', ('"' + $DatabasePath + '"'))
    $startedProcessHandles.Add($backendProcess)
    $null = $backendProcess.Handle
    $startedProcessCreation[$backendProcess.Id] = $backendProcess.StartTime.ToUniversalTime().Ticks
    $identity = Get-ServiceProcessIdentity -Process $backendProcess -Role 'backend'
    if (-not $identity) { throw 'Backend exited before its process identity could be recorded.' }
    $ownedProcesses.Add($identity)
    Write-ServiceMetadata -State 'starting'
    $backendUrl = "http://127.0.0.1:$BackendPort/"
    Wait-ServiceReady -Process $backendProcess -Uri ($backendUrl + 'api/health') -Health
    $catalog = Invoke-RestMethod -Uri ($backendUrl + 'api/graph/node-types/v2') -TimeoutSec 15
    $expectedTavernVersion = $sourceCheck.tavern_package.version
    if ($catalog.schema_version -ne 2 -or
        -not (@($catalog.package_lock) | Where-Object { $_.package_id -eq 'workflow.tavern' -and $_.version -eq $expectedTavernVersion })) {
        throw "The selected database does not expose current workflow.tavern@$expectedTavernVersion. Its saved package configuration was not changed."
    }
    $actualNodes = @($catalog.node_types | ForEach-Object { $_.component_id + '@' + $_.component_version })
    foreach ($node in $sourceCheck.tavern_nodes) {
        if (($node.component_id + '@' + $node.component_version) -notin $actualNodes) {
            throw "Live tavern node catalog is incomplete: $($node.component_id)@$($node.component_version)"
        }
    }

    $frontendProcess = Start-Process -FilePath $nodePath -WorkingDirectory $frontendRoot -WindowStyle Normal -PassThru `
        -ArgumentList @(('"' + $vitePath + '"'), '--host', '127.0.0.1', '--port', $FrontendPort.ToString(), '--strictPort')
    $startedProcessHandles.Add($frontendProcess)
    $null = $frontendProcess.Handle
    $startedProcessCreation[$frontendProcess.Id] = $frontendProcess.StartTime.ToUniversalTime().Ticks
    $identity = Get-ServiceProcessIdentity -Process $frontendProcess -Role 'frontend'
    if (-not $identity) { throw 'Frontend exited before its process identity could be recorded.' }
    $ownedProcesses.Add($identity)
    Write-ServiceMetadata -State 'starting'
    $frontendUrl = "http://127.0.0.1:$FrontendPort/"
    Wait-ServiceReady -Process $frontendProcess -Uri $frontendUrl
    Wait-ServiceReady -Process $frontendProcess -Uri ($frontendUrl + 'api/health') -Health
    $proxyCatalog = Invoke-RestMethod -Uri ($frontendUrl + 'api/graph/node-types/v2') -TimeoutSec 15
    if (($proxyCatalog | ConvertTo-Json -Depth 32 -Compress) -cne ($catalog | ConvertTo-Json -Depth 32 -Compress)) {
        throw 'Frontend proxy and backend do not expose the same node catalog.'
    }
    Write-ServiceMetadata -State 'ready'
    Write-Host "Workbench: $frontendUrl"
    Write-Host "Tavern: ${backendUrl}tavern/"
    Write-Host "Launch metadata: $metadataPath"
    Write-Host 'Service logs are shown in the visible backend and frontend consoles.'
    Write-Host "Stop this launch: powershell.exe -NoProfile -File `"$PSScriptRoot\stop-services.ps1`" -MetadataPath `"$metadataPath`""
    $result['metadata_path'] = $metadataPath
    $result['backend_url'] = $backendUrl
    $result['frontend_url'] = $frontendUrl
    $result['tavern_url'] = $backendUrl + 'tavern/'
    $result | ConvertTo-Json -Depth 8 -Compress
    if (-not $NoBrowser -and -not $env:NO_BROWSER) {
        try { Start-Process $frontendUrl }
        catch { Write-Host "Could not open the browser. Open $frontendUrl manually. $($_.Exception.Message)" }
    }
} catch {
    if ($ownedProcesses.Count -gt 0) {
        try {
            Update-OwnedProcesses
            Stop-ServiceProcessTree -Identities $ownedProcesses.ToArray()
            if ($metadataPath) { Write-ServiceMetadata -State 'failed-stopped' }
        } catch {
            Write-Host "Cleanup could not prove or stop every owned process: $($_.Exception.Message)" -ForegroundColor Red
        }
    }
    # Retain original handles even if recording a just-created process failed.
    $recordedRootPids = @($ownedProcesses | Where-Object { $_.depth -eq 0 } | ForEach-Object { $_.pid })
    foreach ($process in $startedProcessHandles) {
        if ($process.Id -notin $recordedRootPids) {
            Write-Host "Initial identity was not recorded for owned PID $($process.Id); full descendant cleanup cannot be proven." -ForegroundColor Yellow
        }
        try {
            if (-not $process.HasExited) {
                if (-not $startedProcessCreation.ContainsKey($process.Id) -or
                    $process.StartTime.ToUniversalTime().Ticks -ne $startedProcessCreation[$process.Id]) {
                    throw "Creation time could not be verified for launcher-owned PID $($process.Id)."
                }
                Stop-Process -InputObject $process -Force -ErrorAction Stop
                if (-not $process.WaitForExit(5000)) { throw "Owned PID $($process.Id) did not exit." }
            }
        } catch {
            Write-Host "Could not stop a launcher-owned process handle: $($_.Exception.Message)" -ForegroundColor Red
        }
    }
    Write-Host "[START FAILED] $($_.Exception.Message)" -ForegroundColor Red
    if ($logDirectory) { Write-Host "Launch metadata directory: $logDirectory" }
    exit 1
} finally {
    foreach ($name in $savedEnvironment.Keys) {
        [System.Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
    }
}
