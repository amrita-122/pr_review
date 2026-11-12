<#
.SYNOPSIS
  Copies the three external secrets from your local .env into AWS Secrets Manager.

.DESCRIPTION
  Terraform (infra/foundation) creates the secret CONTAINERS; this script puts the VALUES in, so they never touch
  git or Terraform state. Values are written to a temporary file (deleted afterwards) and never printed.
  Run it after `terraform apply` in infra/foundation and before `terraform apply` in infra/app (ECS cannot start a
  task whose secret has no value).

.EXAMPLE
  ./infra/scripts/put-secrets.ps1 -Profile AdministratorAccess-<account-id>
#>
param(
    [Parameter(Mandatory)] [string] $Profile,
    [string] $EnvFile = (Join-Path $PSScriptRoot "..\..\.env"),
    [string] $Region = "us-east-1",
    [string] $Project = "pr-review"
)

$ErrorActionPreference = "Stop"

# .env name -> Secrets Manager name
$map = [ordered]@{
    "GITHUB_WEBHOOK_SECRET"     = "$Project/github-webhook-secret"
    "GITHUB_PRIVATE_KEY_BASE64" = "$Project/github-private-key-base64"
    "ANTHROPIC_API_KEY"         = "$Project/anthropic-api-key"
}

$values = @{}
foreach ($line in Get-Content $EnvFile) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
        $values[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'")
    }
}

foreach ($name in $map.Keys) {
    if (-not $values[$name]) { throw "$name is missing or empty in $EnvFile" }
}

foreach ($name in $map.Keys) {
    $tmp = New-TemporaryFile
    try {
        # No trailing newline: the secret must be exactly the value.
        [System.IO.File]::WriteAllText($tmp.FullName, $values[$name])
        aws secretsmanager put-secret-value --secret-id $map[$name] --secret-string "file://$($tmp.FullName)" `
            --region $Region --profile $Profile --query VersionId --output text | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "put-secret-value failed for $($map[$name])" }
        Write-Host "set $($map[$name])"
    }
    finally {
        Remove-Item $tmp.FullName -Force -ErrorAction SilentlyContinue
    }
}
