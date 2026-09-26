#!/usr/bin/env bash
# Bring a fresh Ubuntu VM to a running GameRec, or bring an already-running one up to date.
#
#   sudo ./deploy/vm/bootstrap.sh
#
# Idempotent by design: every step checks first, so this is also how a deploy happens. Re-running it
# after a `git pull` re-applies the manifests and nothing else.
#
# What it deliberately does not do: create the Secret (credentials never come from a repo), obtain a
# certificate, or run the initial fill. Those are one-time and interactive, and deploy/vm/README.md
# walks through them.
set -euo pipefail

K3S_VERSION="${K3S_VERSION:-v1.34.5+k3s1}"   # pinned: an unpinned installer changes under you
NAMESPACE=gamerec
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KUBECONFIG=/etc/rancher/k3s/k3s.yaml
export KUBECONFIG

say() { printf '\n== %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
    echo "run as root: k3s installs a systemd unit and writes /etc/rancher" >&2
    exit 1
fi

say "system packages"
# curl to fetch the installer, and that is all. No docker: k3s brings containerd, and a second
# container runtime on a small VM is memory spent twice.
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl >/dev/null

say "swap"
# kubelet refuses to start with swap on by default, and a VM image may ship with it enabled.
if swapon --show | grep -q .; then
    swapoff -a
    sed -i.bak '/\sswap\s/s/^/#/' /etc/fstab
    echo "swap disabled and commented out of /etc/fstab"
else
    echo "already off"
fi

say "k3s ${K3S_VERSION}"
if ! command -v k3s >/dev/null; then
    # --write-kubeconfig-mode 600 rather than the installer's suggestion of 644: the kubeconfig holds
    # cluster-admin credentials, and this is a single-admin box, so nothing needs to read it as a
    # normal user. Traefik and the local-path provisioner stay -- see D40 and D33.
    curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="${K3S_VERSION}" \
        INSTALL_K3S_EXEC="server --write-kubeconfig-mode 600 --disable=metrics-server" sh -
else
    echo "already installed: $(k3s --version | head -1)"
fi

say "waiting for the node"
kubectl wait --for=condition=Ready node --all --timeout=180s

say "namespace and config"
kubectl apply -f "${REPO_ROOT}/deploy/k8s/00-namespace.yaml"
if ! kubectl -n "${NAMESPACE}" get secret gamerec-secrets >/dev/null 2>&1; then
    # Not created here: a script that invents an empty Secret means the API starts, answers without
    # narration and takes no backups, all of which look like success.
    cat <<'EOF'

  No gamerec-secrets yet. Nothing will start until it exists:

    kubectl -n gamerec create secret generic gamerec-secrets \
      --from-literal=GROQ_API_KEY=... \
      --from-literal=R2_ACCESS_KEY_ID=... \
      --from-literal=R2_SECRET_ACCESS_KEY=...

  Then set R2_ENDPOINT in deploy/k8s/10-config.yaml and re-run this script.
EOF
fi

say "manifests"
# Numbered so that a plain directory apply orders itself: namespace, config, volumes, then workloads.
# manual/ is excluded -- those are Jobs someone runs on purpose, not part of a deploy.
kubectl apply -f "${REPO_ROOT}/deploy/k8s/"

say "rollout"
kubectl -n "${NAMESPACE}" rollout status deploy/mindb --timeout=300s
kubectl -n "${NAMESPACE}" rollout status deploy/gamerec-api --timeout=300s

say "state"
kubectl -n "${NAMESPACE}" get pods,pvc,cronjob
cat <<EOF

Next, if this was a first install:

  kubectl -n ${NAMESPACE} apply -f deploy/k8s/manual/fill.yaml   # the initial fill, hours long
  kubectl -n ${NAMESPACE} logs -f job/gamerec-fill

The nightly ingest and the backup sidecar need no further action. To check that backups are landing:

  kubectl -n ${NAMESPACE} exec deploy/mindb -c backup -- python -m ops.restore --list
EOF
