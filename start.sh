#!/usr/bin/env bash
set -e

echo "============================================================="
echo "  Launching QAD (Quality Assurance Department) System"
echo "============================================================="

if ! kind get clusters 2>/dev/null | grep -q "^qad$"; then
    echo "[INFO] Kind cluster 'qad' not found. Starting deployment..."
    ./k8s/deploy-local.sh
else
    echo "[OK] Kubernetes cluster 'qad' is active."
fi

kubectl config use-context kind-qad >/dev/null 2>&1 || true

echo "[INFO] Checking pod statuses..."
kubectl -n qad get pods

echo ""
echo "============================================================="
echo "  App is LIVE and Ready!"
echo "============================================================="
echo "  Opening port-forward on: http://localhost:8000"
echo "  Django Admin URL:       http://localhost:8000/admin/"
echo ""
echo "  Login Credentials:"
echo "    Username: NCWSC00001"
echo "    Email:    admin@nairobiwater.co.ke"
echo "    Password: Tatiana2024*"
echo ""
echo "  Press [Ctrl+C] to stop port-forwarding."
echo "============================================================="

kubectl -n qad port-forward svc/qad-nginx 8000:80

