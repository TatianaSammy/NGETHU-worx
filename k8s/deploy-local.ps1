# =============================================================
# QAD — Local Kubernetes Deployment Script for PowerShell (Kind)
# =============================================================
# Prerequisites:
#   - Docker Desktop running
#   - kind (kind.exe) installed
#   - kubectl (kubectl.exe) installed
#
# Usage:
#   .\k8s\deploy-local.ps1
# =============================================================

$ErrorActionPreference = "Continue"

$ClusterName = "qad"
$KubeContext = "kind-$ClusterName"
$Namespace = "qad"
$KindConfig = "k8s/kind-config.yaml"
$Overlay = "k8s/overlays/local"

$BackendImage = "qad-backend:latest"
$NginxImage = "qad-nginx:stable"

function Write-Step($msg) {
    Write-Host "`n[INFO] $msg" -ForegroundColor Cyan
}

function Write-WarnMsg($msg) {
    Write-Host "[WARN] $msg" -ForegroundColor Yellow
}

function Write-Success($msg) {
    Write-Host "[OK]   $msg" -ForegroundColor Green
}

# 1. Prerequisite checks
Write-Step "Checking prerequisites..."
if (-not (Get-Command kind -ErrorAction SilentlyContinue)) {
    Write-Host "[ERROR] kind is not installed or not in PATH. Download from https://kind.sigs.k8s.io/" -ForegroundColor Red
    return
}
if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    Write-Host "[ERROR] kubectl is not installed or not in PATH." -ForegroundColor Red
    return
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "[ERROR] docker is not installed or Docker Desktop is not running." -ForegroundColor Red
    return
}
Write-Success "Prerequisites verified."

# 2. Build Docker images if missing
Write-Step "Checking Docker images..."
$backendExists = docker images -q $BackendImage
if (-not $backendExists) {
    Write-Step "Building $BackendImage..."
    & docker build -t $BackendImage .
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] Failed to build $BackendImage" -ForegroundColor Red
        return
    }
} else {
    Write-Success "$BackendImage is ready."
}

$nginxExists = docker images -q $NginxImage
if (-not $nginxExists) {
    Write-Step "Building $NginxImage..."
    & docker build -f Dockerfile.nginx -t $NginxImage .
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] Failed to build $NginxImage" -ForegroundColor Red
        return
    }
} else {
    Write-Success "$NginxImage is ready."
}

# 3. Create Kind cluster if not already running
Write-Step "Checking Kind cluster '$ClusterName'..."
$rawClusters = (& kind.exe get clusters 2>$null)
$clusterList = @($rawClusters -split "[\r\n]+" | Where-Object { $_ -ne "" })

if ($clusterList -contains $ClusterName) {
    Write-Success "Kind cluster '$ClusterName' is already running."
} else {
    Write-Step "Creating Kind cluster '$ClusterName' with ingress-ready node mappings..."
    & kind.exe create cluster --name $ClusterName --config $KindConfig
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] Failed to create Kind cluster." -ForegroundColor Red
        return
    }
}

# 4. Switch context
Write-Step "Switching kubectl context to $KubeContext..."
& kubectl.exe config use-context $KubeContext

# 5. Load images into Kind
Write-Step "Loading container images into Kind cluster '$ClusterName'..."
& docker tag $BackendImage qad-backend:stable
Write-Host "Loading $BackendImage (this may take a minute)..."
& kind.exe load docker-image $BackendImage --name $ClusterName
& kind.exe load docker-image qad-backend:stable --name $ClusterName
Write-Host "Loading $NginxImage..."
& kind.exe load docker-image $NginxImage --name $ClusterName
Write-Success "Images loaded into cluster."

# 6. Install ingress-nginx for Kind
Write-Step "Installing/verifying Ingress-NGINX controller for Kind..."
& kubectl.exe apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml

Write-Step "Waiting for ingress-nginx controller to be ready (up to 120s)..."
& kubectl.exe wait --namespace ingress-nginx `
    --for=condition=ready pod `
    --selector=app.kubernetes.io/component=controller `
    --timeout=120s

# 7. Apply namespace
Write-Step "Creating namespace '$Namespace'..."
& kubectl.exe apply -f k8s/base/namespace.yaml

# 8. Apply secrets
$secretsFile = "k8s/base/secrets.yaml"
if (Test-Path $secretsFile) {
    Write-Step "Applying secrets from $secretsFile..."
    & kubectl.exe apply -f $secretsFile
} else {
    Write-WarnMsg "$secretsFile not found. Please create it from k8s/base/secrets.example.yaml"
}

# 9. Apply local Kustomize overlay
Write-Step "Deploying QAD application manifests via Kustomize ($Overlay)..."
& kubectl.exe apply -k $Overlay

# 10. Wait for database and migration job
Write-Step "Waiting for PostgreSQL StatefulSet to be ready..."
& kubectl.exe -n $Namespace rollout status statefulset/postgres --timeout=120s
if ($LASTEXITCODE -ne 0) {
    throw "PostgreSQL did not become ready. Deployment stopped."
}

Write-Step "Waiting for database migration job (qad-migrate)..."
& kubectl.exe -n $Namespace wait --for=condition=complete job/qad-migrate --timeout=660s
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Migration job failed or timed out. Recent job details:" -ForegroundColor Red
    & kubectl.exe -n $Namespace describe job qad-migrate
    & kubectl.exe -n $Namespace logs job/qad-migrate -c wait-for-postgres --tail=80
    & kubectl.exe -n $Namespace logs job/qad-migrate -c migrate --tail=120
    throw "Database migration did not complete. Deployment stopped."
}

# 11. Wait for web and service deployments
$deployments = @("pgbouncer", "qad-web", "qad-daphne", "qad-celery", "qad-beat", "qad-nginx")
foreach ($deploy in $deployments) {
    Write-Step "Verifying deployment/$deploy..."
    & kubectl.exe -n $Namespace rollout status "deployment/$deploy" --timeout=120s
    if ($LASTEXITCODE -ne 0) {
        throw "deployment/$deploy did not roll out successfully. Deployment stopped."
    }
}

Write-Host @"

=============================================================
  QAD Kubernetes Deployment Complete!
=============================================================
  Access URLs:
    HTTP:  http://localhost:8080
    HTTPS: https://localhost:8443

  (Or add '127.0.0.1 qad.local' to C:\Windows\System32\drivers\etc\hosts
   and visit http://qad.local:8080)

  Helpful commands:
    kubectl -n $Namespace get all
    kubectl -n $Namespace logs deploy/qad-web -f
    kubectl -n $Namespace exec -it deploy/qad-web -- python manage.py createsuperuser
    kubectl -n $Namespace exec -it deploy/qad-web -- python manage.py shell

  To tear down:
    kind delete cluster --name $ClusterName
=============================================================
"@ -ForegroundColor Green
