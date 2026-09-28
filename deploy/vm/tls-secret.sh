#!/usr/bin/env bash
# Copy the certbot-issued certificate into the Secret the Ingress reads.
#
#   sudo ./deploy/vm/tls-secret.sh gamerec.example.com
#
# Also the certbot deploy hook, which is the point -- renewal happens every 60 days and nobody
# remembers a manual step that infrequent:
#
#   certbot certonly --standalone -d gamerec.example.com \
#     --deploy-hook '/absolute/path/to/GameRec/deploy/vm/tls-secret.sh gamerec.example.com'
#
# certbot records that path and never re-reads it, so it has to be the absolute path to this file
# in the clone that will still be there in 60 days. A wrong one fails silently: the certificate
# renews, the hook errors into /var/log/letsencrypt, and the Secret keeps the expiring cert.
set -euo pipefail

HOST="${1:?usage: tls-secret.sh <hostname>}"
LIVE="/etc/letsencrypt/live/${HOST}"
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

# Said plainly, because kubectl's version of this is "Cannot read file" followed by "no objects passed
# to apply", which reads like a broken cluster rather than a certificate that was never issued.
if [[ ! -r "${LIVE}/fullchain.pem" ]]; then
    echo "no certificate at ${LIVE} -- has certbot run for ${HOST}?" >&2
    exit 1
fi

# create --dry-run | apply, rather than delete and create: the Ingress keeps serving the old
# certificate until the new one is in place, so a failure here changes nothing.
kubectl -n gamerec create secret tls gamerec-tls \
    --cert="${LIVE}/fullchain.pem" \
    --key="${LIVE}/privkey.pem" \
    --dry-run=client -o yaml | kubectl apply -f -

echo "gamerec-tls updated from ${LIVE}"
