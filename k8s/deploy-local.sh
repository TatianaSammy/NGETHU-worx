#!/usr/bin/env bash
# =============================================================
# QAD — Local Kubernetes Deployment Script (Kind)
# =============================================================
# Prerequisites:
#   - Docker Desktop running
#   - kind installed  (kind version)
#   - kubectl installed (kubectl version --client)
#   - qad-backend:latest and qad-nginx:stable built in Docker
#
# Usage:
#   chmod +x k8s/deploy-local.sh
#   ./k8s/deploy-local.sh
# =============================================================

set -euo pipefail

CLUSTER_NAME="qad"
KUBECONFIG_CTX="kind-${CLUSTER_NAME}"
NAMESPACE="qad"
KIND_CONFIG="k8s/kind-config.yaml"
OVERLAY="k8s/overlays/local"

BACKEND_IMAGE="qad-backend:latest"
NGINX_IMAGE="qad-nginx:stable"

# ── Color helpers ──────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── 0. Prerequisite checks ─────────────────────────────────
command -v kind    >/dev/null 2>&1 || error "kind is not installed. Download from https://kind.sigs.k8s.io/"
command -v kubectl >/dev/null 2>&1 || error "kubectl is not installed."
command -v docker  >/dev/null 2>&1 || error "docker is not installed."

info "Docker images present:"
docker image inspect "${BACKEND_IMAGE}" --format "  ✔ ${BACKEND_IMAGE}" 2>/dev/null \
  || warn "${BACKEND_IMAGE} not found — build it first with: docker build -t ${BACKEND_IMAGE} ."
docker image inspect "${NGINX_IMAGE}" --format "  ✔ ${NGINX_IMAGE}" 2>/dev/null \
  || warn "${NGINX_IMAGE} not found — build it first with: docker build -f Dockerfile.nginx -t ${NGINX_IMAGE} ."

# ── 1. Create Kind cluster (idempotent) ────────────────────
if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
  warn "Kind cluster '${CLUSTER_NAME}' already exists — skipping creation."
else
  info "Creating Kind cluster '${CLUSTER_NAME}'..."
  kind create cluster --name "${CLUSTER_NAME}" --config "${KIND_CONFIG}"
fi

# ── 2. Switch kubectl context ─────────────────────────────
info "Switching to kubectl context: ${KUBECONFIG_CTX}"
kubectl config use-context "${KUBECONFIG_CTX}"

# ── 3. Install ingress-nginx (for local routing) ──────────
info "Installing ingress-nginx controller..."
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml

info "Waiting for ingress-nginx to be ready..."
kubectl wait --namespace ingress-nginx \
  --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller \
  --timeout=120s

# ── 4. Load images into Kind ──────────────────────────────
info "Loading ${BACKEND_IMAGE} into Kind cluster..."
kind load docker-image "${BACKEND_IMAGE}" --name "${CLUSTER_NAME}"

info "Loading ${NGINX_IMAGE} into Kind cluster..."
kind load docker-image "${NGINX_IMAGE}" --name "${CLUSTER_NAME}"

# ── 5. Create the namespace (if not already) ─────────────
kubectl apply -f k8s/base/namespace.yaml

# ── 6. Apply secrets (prompt if not present) ─────────────
SECRETS_FILE="k8s/base/secrets.yaml"
if [[ -f "${SECRETS_FILE}" ]]; then
  info "Applying secrets from ${SECRETS_FILE}..."
  kubectl apply -f "${SECRETS_FILE}"
else
  warn "No secrets.yaml found at ${SECRETS_FILE}."
  warn "Copy k8s/base/secrets.example.yaml → k8s/base/secrets.yaml, fill in values, then re-run."
  warn "Skipping secret apply — existing secrets in cluster (if any) will be used."
fi

# ── 7. Apply Kustomize overlay ────────────────────────────
info "Applying Kustomize overlay: ${OVERLAY}"
kubectl apply -k "${OVERLAY}"

# ── 8. Wait for migrate job ───────────────────────────────
info "Waiting for migrate job to complete (up to 5 minutes)..."
kubectl -n "${NAMESPACE}" wait \
  --for=condition=complete \
  job/qad-migrate \
  --timeout=300s \
  || warn "Migrate job timed out or failed — check: kubectl -n ${NAMESPACE} logs job/qad-migrate"

# ── 9. Wait for deployments ───────────────────────────────
for deploy in qad-web qad-daphne qad-celery qad-beat qad-nginx; do
  info "Waiting for deployment/${deploy}..."
  kubectl -n "${NAMESPACE}" rollout status "deployment/${deploy}" --timeout=120s \
    || warn "${deploy} rollout not complete — check: kubectl -n ${NAMESPACE} describe deployment/${deploy}"
done

# ── 10. Summary ───────────────────────────────────────────
echo ""
info "===== Deployment complete! ====="
echo ""
echo "  Web (HTTP):        http://localhost:8080"
echo "  Web (HTTPS):       https://localhost:8443"
echo "  Or add to /etc/hosts: 127.0.0.1 qad.local"
echo "  Then visit:        http://qad.local:8080"
echo ""
echo "Useful commands:"
echo "  kubectl -n ${NAMESPACE} get all"
echo "  kubectl -n ${NAMESPACE} logs deploy/qad-web -f"
echo "  kubectl -n ${NAMESPACE} logs deploy/qad-celery -f"
echo "  kubectl -n ${NAMESPACE} exec -it deploy/qad-web -- python manage.py shell"
echo "  kubectl -n ${NAMESPACE} exec -it deploy/qad-web -- python manage.py createsuperuser"
echo ""
echo "To tear down:  kind delete cluster --name ${CLUSTER_NAME}"

