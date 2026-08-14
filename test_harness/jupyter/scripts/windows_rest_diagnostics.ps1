<#
.SYNOPSIS
Validates every Jupyter REST endpoint required by the Executor Worker.

.DESCRIPTION
Runs a non-destructive diagnostic against one or more native Windows Jupyter servers. The script
checks standard Jupyter APIs, Executor extension APIs, kernel lifecycle, notebook persistence, and
Artifact metadata. Each server receives a unique diagnostics workspace. The workspace is removed
after a successful or failed run unless -KeepWorkspace is supplied.

.PARAMETER Endpoints
Jupyter base URLs. Defaults to the three local native-test ports 8888, 8889, and 8890.

.PARAMETER Token
Jupyter token shared by the supplied endpoints. Defaults to JUPYTER_TOKEN. Run the script once per
endpoint when servers use different tokens.

.PARAMETER Profile
Kernel profile used for the lifecycle check. Defaults to basic.

.PARAMETER KeepWorkspace
Preserves the unique diagnostics workspace on each Jupyter server for manual inspection.

.EXAMPLE
$env:JUPYTER_TOKEN = "local-test-token"
.\test_harness\jupyter\scripts\windows_rest_diagnostics.ps1

.EXAMPLE
.\test_harness\jupyter\scripts\windows_rest_diagnostics.ps1 `
  -Endpoints "http://127.0.0.1:8888" `
  -Token "server-specific-token" `
  -KeepWorkspace
#>

[CmdletBinding()]
param(
    [string[]]$Endpoints = @(
        "http://127.0.0.1:8888",
        "http://127.0.0.1:8889",
        "http://127.0.0.1:8890"
    ),
    [string]$Token = $env:JUPYTER_TOKEN,
    [ValidateSet("basic", "ml")]
    [string]$Profile = "basic",
    [switch]$KeepWorkspace
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($Token)) {
    throw "Jupyter token is required. Set JUPYTER_TOKEN or pass -Token."
}
if ($Endpoints.Count -eq 0) {
    throw "At least one Jupyter endpoint is required."
}

function Get-HttpStatusCode {
    param([System.Exception]$Exception)

    $responseProperty = $Exception.PSObject.Properties["Response"]
    if ($null -eq $responseProperty -or $null -eq $responseProperty.Value) {
        return "unavailable"
    }
    try {
        return [int]$responseProperty.Value.StatusCode
    }
    catch {
        return "unknown"
    }
}

function Invoke-JupyterRequest {
    param(
        [Parameter(Mandatory = $true)][string]$Endpoint,
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][string]$Path,
        [object]$Body
    )

    $uri = "$($Endpoint.TrimEnd('/'))$Path"
    $arguments = @{
        Method = $Method
        Uri = $uri
        Headers = @{ Authorization = "token $Token" }
        ErrorAction = "Stop"
    }
    if ($null -ne $Body) {
        $arguments["ContentType"] = "application/json"
        $arguments["Body"] = $Body | ConvertTo-Json -Depth 20 -Compress
    }

    try {
        $result = Invoke-RestMethod @arguments
        Write-Host "  [PASS] $Method $Path" -ForegroundColor Green
        return $result
    }
    catch {
        $status = Get-HttpStatusCode -Exception $_.Exception
        throw "[FAIL] $Method $Path (status=$status): $($_.Exception.Message)"
    }
}

function Assert-Condition {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Message
    )

    if (-not $Condition) {
        throw "[FAIL] Response validation: $Message"
    }
}

$failures = New-Object System.Collections.Generic.List[string]

foreach ($rawEndpoint in $Endpoints) {
    $endpoint = $rawEndpoint.TrimEnd('/')
    $diagnosticId = [Guid]::NewGuid().ToString("N")
    # Match the production Execution hierarchy so Windows path-length problems are reproducible.
    $workspace = (
        "users/diagnostic-user/projects/diagnostic-project/" +
        "sessions/tool-session-$($diagnosticId.Substring(0, 24))/executions/$diagnosticId"
    )
    $notebookPath = "$workspace/notebooks/execution.ipynb"
    $checkpointPath = "$workspace/notebooks/.ipynb_checkpoints/execution-checkpoint.ipynb"
    $artifactPath = "$workspace/artifacts/reports/rest-diagnostic.txt"
    $kernelId = $null
    $workspacePrepared = $false

    Write-Host ""
    Write-Host "=== Jupyter REST diagnostics: $endpoint ===" -ForegroundColor Cyan
    Write-Host "  Workspace relative path length: $($workspace.Length)"
    Write-Host "  Checkpoint relative path length: $($checkpointPath.Length)"
    Write-Host "  Checkpoint relative path: $checkpointPath"

    try {
        $status = Invoke-JupyterRequest -Endpoint $endpoint -Method "GET" -Path "/api/status"
        Assert-Condition -Condition ($null -ne $status) -Message "Status response is empty."

        $kernelspecs = Invoke-JupyterRequest `
            -Endpoint $endpoint `
            -Method "GET" `
            -Path "/api/kernelspecs"
        $profiles = @($kernelspecs.kernelspecs.PSObject.Properties.Name)
        Assert-Condition `
            -Condition ($profiles -contains $Profile) `
            -Message "Kernel profile '$Profile' is missing. Available: $($profiles -join ', ')."

        $resources = Invoke-JupyterRequest `
            -Endpoint $endpoint `
            -Method "GET" `
            -Path "/executor/resource-status"
        Assert-Condition `
            -Condition ($resources.schema_version -eq "1.0") `
            -Message "Resource schema_version must be 1.0."

        $prepared = Invoke-JupyterRequest `
            -Endpoint $endpoint `
            -Method "POST" `
            -Path "/executor/storage/workspaces/prepare" `
            -Body @{ workspace_path = $workspace }
        $workspacePrepared = $true
        Assert-Condition `
            -Condition ($prepared.workspace_path -eq $workspace) `
            -Message "Prepared workspace path does not match the request."
        Assert-Condition `
            -Condition ($prepared.notebook_path -eq $notebookPath) `
            -Message "Prepared notebook path does not match the Executor convention."

        $snapshotBefore = Invoke-JupyterRequest `
            -Endpoint $endpoint `
            -Method "POST" `
            -Path "/executor/storage/artifacts/snapshot" `
            -Body @{ workspace_path = $workspace }
        Assert-Condition `
            -Condition ($null -ne $snapshotBefore.files) `
            -Message "Artifact snapshot has no files collection."

        $kernel = Invoke-JupyterRequest `
            -Endpoint $endpoint `
            -Method "POST" `
            -Path "/api/kernels" `
            -Body @{ name = $Profile; path = $workspace }
        $kernelId = [string]$kernel.id
        Assert-Condition `
            -Condition (-not [string]::IsNullOrWhiteSpace($kernelId)) `
            -Message "Kernel creation returned no id."
        $null = Invoke-JupyterRequest `
            -Endpoint $endpoint `
            -Method "GET" `
            -Path "/api/kernels/$kernelId"
        $null = Invoke-JupyterRequest `
            -Endpoint $endpoint `
            -Method "POST" `
            -Path "/api/kernels/$kernelId/interrupt"

        $notebook = @{
            type = "notebook"
            format = "json"
            content = @{
                cells = @(
                    @{
                        cell_type = "code"
                        execution_count = $null
                        metadata = @{}
                        outputs = @()
                        source = @("print('Executor REST diagnostics')")
                    }
                )
                metadata = @{
                    kernelspec = @{
                        name = $Profile
                        display_name = "Executor REST diagnostics"
                    }
                }
                nbformat = 4
                nbformat_minor = 5
            }
        }
        $null = Invoke-JupyterRequest `
            -Endpoint $endpoint `
            -Method "PUT" `
            -Path "/api/contents/$notebookPath" `
            -Body $notebook
        # A second save exercises Jupyter's checkpoint creation path for an existing notebook.
        $null = Invoke-JupyterRequest `
            -Endpoint $endpoint `
            -Method "PUT" `
            -Path "/api/contents/$notebookPath" `
            -Body $notebook
        $checkpoints = Invoke-JupyterRequest `
            -Endpoint $endpoint `
            -Method "GET" `
            -Path "/api/contents/$notebookPath/checkpoints"
        Assert-Condition `
            -Condition (@($checkpoints).Count -gt 0) `
            -Message "Jupyter did not create a notebook checkpoint after the second save."
        $storedNotebook = Invoke-JupyterRequest `
            -Endpoint $endpoint `
            -Method "GET" `
            -Path "/api/contents/$($notebookPath)?content=1"
        Assert-Condition `
            -Condition ($storedNotebook.type -eq "notebook") `
            -Message "Stored contents item is not a notebook."

        $artifact = @{
            type = "file"
            format = "text"
            content = "Executor REST diagnostics $diagnosticId"
        }
        $null = Invoke-JupyterRequest `
            -Endpoint $endpoint `
            -Method "PUT" `
            -Path "/api/contents/$artifactPath" `
            -Body $artifact

        $snapshotAfter = Invoke-JupyterRequest `
            -Endpoint $endpoint `
            -Method "POST" `
            -Path "/executor/storage/artifacts/snapshot" `
            -Body @{ workspace_path = $workspace }
        $artifactFiles = @($snapshotAfter.files | ForEach-Object { $_.path })
        Assert-Condition `
            -Condition ($artifactFiles -contains $artifactPath) `
            -Message "Artifact snapshot did not discover $artifactPath."

        $notebookMetadata = Invoke-JupyterRequest `
            -Endpoint $endpoint `
            -Method "POST" `
            -Path "/executor/storage/files/metadata" `
            -Body @{ path = $notebookPath }
        Assert-Condition `
            -Condition (([string]$notebookMetadata.checksum_sha256).Length -eq 64) `
            -Message "Notebook checksum is invalid."

        $artifactMetadata = Invoke-JupyterRequest `
            -Endpoint $endpoint `
            -Method "POST" `
            -Path "/executor/storage/files/metadata" `
            -Body @{ path = $artifactPath }
        Assert-Condition `
            -Condition (([string]$artifactMetadata.checksum_sha256).Length -eq 64) `
            -Message "Artifact checksum is invalid."

        $manifest = Invoke-JupyterRequest `
            -Endpoint $endpoint `
            -Method "POST" `
            -Path "/executor/storage/manifests/read" `
            -Body @{ workspace_path = $workspace; start = 0 }
        Assert-Condition `
            -Condition ($null -ne $manifest.content) `
            -Message "Manifest response has no content field."

        Write-Host "[PASS] All required Jupyter REST APIs succeeded: $endpoint" `
            -ForegroundColor Green
    }
    catch {
        $message = "$endpoint - $($_.Exception.Message)"
        $failures.Add($message)
        Write-Host $message -ForegroundColor Red
    }
    finally {
        if (-not [string]::IsNullOrWhiteSpace([string]$kernelId)) {
            try {
                $null = Invoke-JupyterRequest `
                    -Endpoint $endpoint `
                    -Method "DELETE" `
                    -Path "/api/kernels/$kernelId"
            }
            catch {
                Write-Warning "Kernel cleanup failed on $endpoint`: $($_.Exception.Message)"
            }
        }
        if ($workspacePrepared -and -not $KeepWorkspace) {
            try {
                $null = Invoke-JupyterRequest `
                    -Endpoint $endpoint `
                    -Method "DELETE" `
                    -Path "/api/contents/$workspace"
            }
            catch {
                Write-Warning "Workspace cleanup failed on $endpoint`: $($_.Exception.Message)"
            }
        }
        elseif ($workspacePrepared) {
            Write-Host "  Diagnostics workspace retained: $workspace" -ForegroundColor Yellow
        }
    }
}

Write-Host ""
if ($failures.Count -gt 0) {
    Write-Host "Jupyter REST diagnostics failed for $($failures.Count) endpoint(s):" `
        -ForegroundColor Red
    foreach ($failure in $failures) {
        Write-Host "  - $failure" -ForegroundColor Red
    }
    exit 1
}

Write-Host "Jupyter REST diagnostics passed for all $($Endpoints.Count) endpoint(s)." `
    -ForegroundColor Green
exit 0
