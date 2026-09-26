# The VM

One Oracle Cloud Ampere A1 (arm64), k3s, no load balancer and no managed anything (ADR-0006). The
whole deploy is `sudo ./deploy/vm/bootstrap.sh`, which is idempotent — it is both the install and the
way an update lands.

```
                  Internet
                      │  443
              ┌───────▼────────┐
              │ Traefik (k3s)  │  Ingress, TLS from the gamerec-tls Secret
              └───────┬────────┘
                      │
   ┌──────────────────▼─────────────────────────────────┐
   │ k3s, one node                                      │
   │                                                    │
   │  gamerec-api ×2 ──────► mindb :50051               │
   │       │                   │   └─ backup sidecar ──┐ │
   │       │ /corpus (ro)      │ /data                 │ │
   │       ▼                   ▼                       │ │
   │  corpus PVC          mindb-data PVC               │ │
   │       ▲                                           │ │
   │  ingest CronJob 03:17 UTC ────────────────────────┼─┤
   └───────────────────────────────────────────────────┼─┘
                                                       │
                                            Cloudflare R2
                                    corpus/ + gen-<ts>/ (7 kept)
```

## Sizing

The A1 always-free shape is 4 OCPU / 24 GB, which is far more than this needs; the numbers matter
anyway because they say what a *smaller* box would break first.

| | requests | limits |
|---|---|---|
| mindb | 700Mi | 1200Mi |
| backup sidecar | 450Mi | 700Mi |
| gamerec-api ×2 | 1000Mi | 1800Mi |
| ingest CronJob (nightly) | 600Mi | 1Gi |
| k3s itself (traefik, coredns, metrics-server, local-path) | ~500Mi | — |

So ~3.3 GB of requests during the nightly ingest window. 4 GB is not enough once the ingest overlaps
a snapshot; 8 GB is comfortable. MinDB's request is what it genuinely reserves at boot — 200,000 ×
384 × 4 B for the float32 store plus the int8 cascade copy — not a guess, so it is the one number that
cannot be trimmed without lowering `-capacity`.

On arm64 MinDB falls back to pure Go for the int8 cascade, since the AVX2 kernel is x86-only. Every
benchmark this project quotes is labelled with the architecture for exactly that reason (ADR-0006).

## Before the first bootstrap

**Open 80 and 443 in the VCN.** OCI's default security list allows only 22, and nothing about the
symptom says so — the pods are Ready, `curl localhost` works, the outside world times out.

**Oracle's Ubuntu image ships iptables rules that break k3s.** The image installs a REJECT rule in
`INPUT` and persists it with netfilter-persistent, which drops pod-to-pod and pod-to-service traffic
in ways that look like DNS flakiness. Clear it before installing k3s:

```
sudo iptables -L INPUT --line-numbers | grep REJECT      # find the rejects
sudo iptables -D INPUT <n>                               # highest line number first
sudo netfilter-persistent save
```

**Point DNS at the VM** and fill the hostname in, or the Ingress matches a Host header that never
arrives:

```
sed -i 's/gamerec.example.com/<your host>/' deploy/k8s/50-ingress.yaml
```

## Install

```
sudo apt-get install -y git
git clone https://github.com/DeviousDrops/GameRec /root/GameRec
cd /root/GameRec

kubectl -n gamerec create secret generic gamerec-secrets \
  --from-literal=GROQ_API_KEY=... \
  --from-literal=R2_ACCESS_KEY_ID=... \
  --from-literal=R2_SECRET_ACCESS_KEY=...          # after the first bootstrap creates the namespace

sudo ./deploy/vm/bootstrap.sh
```

`R2_ENDPOINT` goes in `deploy/k8s/10-config.yaml` — it is not a secret, and leaving it empty is a
supported state that means "no lease and no backups", which the sidecar says loudly by crash-looping.

Then the initial fill, which takes hours at 35 requests a minute (D17) and is resumable, so a
disconnected ssh session costs nothing:

```
kubectl -n gamerec apply -f deploy/k8s/manual/fill.yaml
kubectl -n gamerec logs -f job/gamerec-fill
```

## TLS

certbot in standalone mode, with a deploy hook that writes the certificate into the Secret the Ingress
reads. No cert-manager: it is an operator and a CRD set to keep updated, for one certificate on one
host (D40).

```
sudo apt-get install -y certbot
sudo systemctl stop k3s              # standalone mode needs port 80, which Traefik is holding
sudo certbot certonly --standalone -d <your host> \
  --deploy-hook '/root/GameRec/deploy/vm/tls-secret.sh <your host>'
sudo systemctl start k3s
```

certbot's own systemd timer handles renewal from then on, and the hook keeps the Secret in step.
Without the Secret, Traefik serves its self-signed default — a browser warning, not an outage.

## Checking that backups are real

```
kubectl -n gamerec exec deploy/mindb -c backup -- python -m ops.restore --list
```

Seven generations, the newest within a snapshot interval of now. The failure worth watching for is not
an error in a log — it is this list quietly stopping at an old timestamp, so it is worth looking at
after any change to the sidecar or its credentials.

A restore is a deliberate, disruptive operation and reads its own instructions:
[deploy/k8s/manual/restore.yaml](../k8s/manual/restore.yaml).

## What has not been done on a real VM

Everything above is written from the k3d verification in [../k8s/README.md](../k8s/README.md) plus the
documented behaviour of k3s, certbot and OCI. The manifests, the backup sidecar's code path and the
restore have all been run; **this script has not yet been run on an Oracle A1**, so the OCI-specific
steps — the security list, the iptables rules, arm64 image pulls — are the parts most likely to need a
correction on first contact. The script is idempotent so that correcting it is cheap: fix, `git pull`,
run it again.
