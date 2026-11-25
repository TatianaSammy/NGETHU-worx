# ==============================================================================
# QAD — Quick Start & Launch Script
# ==============================================================================
# Usage:
#   .\start.ps1
# ==============================================================================

Write-Host "=============================================================" -ForegroundColor Cyan
Write-Host "  Launching QAD (Quality Assurance Department) System" -ForegroundColor Cyan
Write-Host "=============================================================" -ForegroundColor Cyan

# 1. Check if Kind cluster is running
$clusterList = (& kind.exe get clusters 2>$null)
if ($clusterList -notcontains "qad") {
    Write-Host "`n[INFO] Kind cluster 'qad' not found. Starting deployment..." -ForegroundColor Yellow
    & .\k8s\deploy-local.ps1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "`n[ERROR] Deployment failed." -ForegroundColor Red
        return
    }
} else {
    Write-Host "`n[OK] Kubernetes cluster 'qad' is active." -ForegroundColor Green
}

# 2. Ensure kubectl context is set
kubectl.exe config use-context kind-qad | Out-Null

# 3. Wait for the user-facing deployments before opening the app
foreach ($deployment in @("qad-web", "qad-nginx")) {
    Write-Host "[INFO] Waiting for deployment/$deployment..." -ForegroundColor Cyan
    kubectl.exe -n qad rollout status "deployment/$deployment" --timeout=180s
    if ($LASTEXITCODE -ne 0) {
        Write-Host "`n[ERROR] deployment/$deployment is not ready; the app was not opened." -ForegroundColor Red
        kubectl.exe -n qad get pods
        return
    }
}

# 4. Verify pods
Write-Host "[INFO] Checking pod statuses..." -ForegroundColor Cyan
kubectl.exe -n qad get pods

Write-Host @"

=============================================================
  App is LIVE and Ready!
=============================================================
  Opening port-forward on: http://localhost:8000
  Django Admin URL:       http://localhost:8000/admin/

  Login Credentials:
    Username: NCWSC00001
    Email:    admin@nairobiwater.co.ke
    Password: Tatiana2024*

  Press [Ctrl+C] in this terminal to stop port-forwarding.
=============================================================
"@ -ForegroundColor Green

# 5. Open default browser to admin login
Start-Process "http://localhost:8000/admin/"

# 6. Start port-forwarding
kubectl.exe -n qad port-forward svc/qad-nginx 8000:80

