#!/usr/bin/env bash
# Copy the certbot-issued certificate into the Secret the Ingress reads.
#
#   sudo ./deploy/vm/tls-secret.sh gamerec.example.com
#
# Also the certbot deploy hook, which is the point -- renewal happens every 60 days and nobody
# remembers a manual step that infrequent:
#
#   certbot certonly --standalone -d gamerec.example.com \
#     --deploy-hook '/root/GameRec/deploy/vm/tls-secret.sh gamerec.example.com'
set -euo pipefail

HOST="${1:?usage: tls-secret.sh <hostname>}"
LIVE="/etc/letsencrypt/live/${HOST}"
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

# create --dry-run | apply, rather than delete and create: the Ingress keeps serving the old
# certificate until the new one is in place, so a failure here changes nothing.
kubectl -n gamerec create secret tls gamerec-tls \
    --cert="${LIVE}/fullchain.pem" \
    --key="${LIVE}/privkey.pem" \
    --dry-run=client -o yaml | kubectl apply -f -

echo "gamerec-tls updated from ${LIVE}"
