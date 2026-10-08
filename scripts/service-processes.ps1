#requires -Version 5.1

function Get-ServiceProcessIdentity {
    param(
        [System.Diagnostics.Process]$Process,
        [string]$Role,
        [int]$Depth = 0
    )
    $Process.Refresh()
    if ($Process.HasExited) { return $null }
    $native = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId=$($Process.Id)"
    if (-not $native -or -not $native.ExecutablePath -or -not $native.CommandLine) {
        throw "Cannot prove service process identity for PID $($Process.Id)."
    }
    return [PSCustomObject]@{
        pid = $Process.Id
        role = $Role
        depth = $Depth
        parent_pid = [int]$native.ParentProcessId
        start_time_ticks = $Process.StartTime.ToUniversalTime().Ticks.ToString()
        executable_path = [System.IO.Path]::GetFullPath($native.ExecutablePath)
        command_line = [string]$native.CommandLine
    }
}

function Get-MatchingServiceProcess {
    param([object]$Identity)
    $process = Get-Process -Id $Identity.pid -ErrorAction SilentlyContinue
    if (-not $process) { return $null }
    $actual = Get-ServiceProcessIdentity -Process $process -Role $Identity.role -Depth $Identity.depth
    if (-not $actual) { return $null }
    if ($actual.start_time_ticks -ne $Identity.start_time_ticks -or
        -not [string]::Equals($actual.executable_path, $Identity.executable_path,
            [System.StringComparison]::OrdinalIgnoreCase) -or
        $actual.command_line -cne $Identity.command_line) {
        throw "PID $($Identity.pid) no longer matches this launch. No unrelated process will be stopped."
    }
    return $process
}

function Get-ServiceProcessTree {
    param([object[]]$Identities)
    $result = [System.Collections.Generic.List[object]]::new()
    $queue = [System.Collections.Generic.Queue[object]]::new()
    $seen = @{}
    foreach ($identity in $Identities) {
        if (-not $seen.ContainsKey([int]$identity.pid)) {
            $queue.Enqueue($identity)
            $seen[[int]$identity.pid] = $true
        }
    }
    while ($queue.Count -gt 0) {
        $identity = $queue.Dequeue()
        $result.Add($identity)
        $process = Get-MatchingServiceProcess -Identity $identity
        if (-not $process) { continue }
        foreach ($child in @(Get-CimInstance -ClassName Win32_Process -Filter "ParentProcessId=$($identity.pid)")) {
            if ($seen.ContainsKey([int]$child.ProcessId)) { continue }
            $childProcess = Get-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
            if (-not $childProcess) { continue }
            $childIdentity = Get-ServiceProcessIdentity -Process $childProcess -Role $identity.role `
                -Depth ($identity.depth + 1)
            if (-not $childIdentity -or
                [long]$childIdentity.start_time_ticks -lt [long]$identity.start_time_ticks) { continue }
            $seen[[int]$childIdentity.pid] = $true
            $queue.Enqueue($childIdentity)
        }
    }
    return $result.ToArray()
}

function Stop-ServiceProcessTree {
    param([object[]]$Identities)
    # Validate the complete set before acting, then recheck each open process handle.
    foreach ($identity in $Identities) {
        Get-MatchingServiceProcess -Identity $identity | Out-Null
    }
    foreach ($identity in @($Identities | Sort-Object -Property depth -Descending)) {
        $process = Get-MatchingServiceProcess -Identity $identity
        if (-not $process) { continue }
        Stop-Process -InputObject $process -Force -ErrorAction Stop
        if (-not $process.WaitForExit(5000)) {
            throw "Service PID $($identity.pid) did not exit."
        }
    }
}
