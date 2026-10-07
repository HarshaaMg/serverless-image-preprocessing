# Points the LOCAL React app at the real AWS API (after terraform apply).
#
# Writes frontend/.env.aws.local (git-ignored) with the API Gateway URL from the
# Terraform outputs. Then start the app with:   cd frontend; npm run dev:aws
# and open http://localhost:5173
#
# "npm run dev" (without :aws) keeps using the local mock backend.
#
# Usage (from the project root):  .\scripts\configure_frontend.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

# Read the Terraform outputs (defined in infra/outputs.tf) as JSON.
Push-Location (Join-Path $root "infra")
try {
    $outputs = terraform output -json | ConvertFrom-Json
} finally {
    Pop-Location
}
$api = $outputs.api_endpoint.value
if (-not $api) { throw "Terraform output 'api_endpoint' is missing. Run terraform apply first." }

# Vite loads .env.aws.local only in "aws" mode (npm run dev:aws), so the normal
# "npm run dev" keeps using the local mock backend. WriteAllText writes UTF-8 without
# a BOM, which Vite can read reliably.
$envFile = Join-Path $root "frontend\.env.aws.local"
[System.IO.File]::WriteAllText($envFile, "VITE_API_BASE_URL=$api`n")

Write-Host "API endpoint      : $api"
Write-Host "Allowed origins   : $($outputs.cors_allowed_origins.value -join ', ')"
Write-Host "Wrote             : $envFile"
Write-Host ""
Write-Host "Next:  cd frontend; npm run dev:aws    then open http://localhost:5173"
