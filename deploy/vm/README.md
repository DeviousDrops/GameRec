# The VM

One Azure `Standard_B2als_v2` — x86_64, 2 vCPU, 4 GiB, burstable — running Ubuntu 24.04 and k3s, with
no load balancer and no managed anything (D46; ADR-0006 has the revision). The whole deploy is
`sudo ./deploy/vm/bootstrap.sh`, which is idempotent — it is both the install and the way an update
lands.

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
   │  gamerec-api ─────────► mindb :50051               │
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

4 GiB and 2 burstable vCPU, which is tight rather than comfortable. These numbers are why the API runs
one replica and not two (D47):

| | requests | limits |
|---|---|---|
| mindb | 700Mi | 1200Mi |
| backup sidecar | 450Mi | 700Mi |
| gamerec-api ×1 | 500Mi | 900Mi |
| ingest CronJob (nightly) | 600Mi | 1Gi |
| k3s itself (traefik, coredns, local-path) | ~250Mi | — |

~1.9 GiB of requests at rest and ~2.5 GiB while the nightly ingest runs, against 4 GiB with the OS and
k3s inside it. Headroom, but not much: the two things that can spike together are the sidecar holding a
whole snapshot in memory to upload it and an ingest embedding a batch. metrics-server is disabled in
`bootstrap.sh` for the same reason — nothing here autoscales, so it would be ~100Mi spent on a graph.

MinDB's request is what it genuinely reserves at boot — 200,000 × 384 × 4 B for the float32 store plus
the int8 cascade copy — not a guess, so it is the one number that cannot be trimmed without lowering
`-capacity`. Worth knowing before that trade comes up: the corpus after the Scope Filter is far smaller
than 200,000, so capacity could come down to ~120,000 and save ~150 MB, at the cost of a restart the
day the catalogue outgrows it.

CPU is a credit balance on a B-series, not a constant. The paced ingest barely touches it — 35 Steam
requests a minute leaves both vCPU idle between fetches — but `ingest.reindex` embeds ~176k documents
back to back, and once credits are gone it runs at the baseline share. Plan a reindex as hours, not
the ~20 minutes the benchmark CPU would suggest.

This host is x86_64, so MinDB selects its AVX2 int8 kernel rather than the pure-Go fallback — `/health`
reports which, and every benchmark this project quotes carries that label (ADR-0006, D46).

## Before the first bootstrap

**Open 80 and 443 in the network security group.** Azure's default NSG allows 22 and nothing else, and
no symptom on the VM says so — the pods are Ready, `curl localhost` works, the outside world times out.
Prove it from somewhere else before blaming k3s:

```
mkdir -p /tmp/probe && cd /tmp/probe      # never $HOME: this serves whatever directory it runs in
sudo python3 -m http.server 80            # then curl http://<public ip>/ from another machine
```

A dropped SYN and a refused connection look the same to `curl` at a glance; the tell is the timing. A
refusal comes back in one round trip, a filtered port takes seconds and returns nothing.

**Point DNS at the VM.** The name is `game-rec.duckdns.org`, already in
[50-ingress.yaml](../k8s/50-ingress.yaml). DuckDNS's web form prefills the IP of the browser talking to
it, which quietly points the name at your laptop; update it *from the VM* instead, with no `ip=`
parameter, so DuckDNS records the address the request came from:

```
read -rs TOKEN                                                 # nothing is echoed
curl -s "https://www.duckdns.org/update?domains=game-rec&token=$TOKEN"; echo
unset TOKEN
```

History keeps the literal `$TOKEN`, not its value. Check it with a resolver that is not your own:
`nslookup game-rec.duckdns.org 1.1.1.1`. If the hostname ever changes, it lives in exactly one place:

```
sed -i 's/game-rec.duckdns.org/<your host>/' deploy/k8s/50-ingress.yaml
```

## Install

```
sudo apt-get install -y git
sudo git clone https://github.com/DeviousDrops/GameRec /root/GameRec
cd /root/GameRec

# the namespace first: the Secret goes in it, and bootstrap.sh will not invent a Secret
kubectl apply -f deploy/k8s/00-namespace.yaml

read -rsp "Groq API key: " GROQ_KEY; echo
read -rsp "R2 access key id: " R2_ID; echo
read -rsp "R2 secret access key: " R2_SECRET; echo
printf '%s\n' "GROQ_API_KEY=$GROQ_KEY" "R2_ACCESS_KEY_ID=$R2_ID" \
  "R2_SECRET_ACCESS_KEY=$R2_SECRET" \
  "R2_ENDPOINT=https://<account>.r2.cloudflarestorage.com" \
  | kubectl -n gamerec create secret generic gamerec-secrets --from-env-file=/dev/stdin
unset GROQ_KEY R2_ID R2_SECRET

sudo ./deploy/vm/bootstrap.sh
```

Nothing is echoed as you type and history keeps the variable names, not their values. `R2_ENDPOINT`
is in the Secret rather than the ConfigMap (D48). Leaving it unset anywhere is a supported state
that means "no lease and no backups", which the sidecar says loudly by crash-looping. Check the four
keys landed without printing any of them:

```
kubectl -n gamerec get secret gamerec-secrets \
  -o go-template='{{range $k,$v := .data}}{{$k}}{{"\t"}}{{len $v}}{{"\n"}}{{end}}'
```

Then the initial fill, which takes hours at 35 requests a minute (D17) and is resumable, so a
disconnected ssh session costs nothing:

```
kubectl -n gamerec apply -f deploy/k8s/manual/fill.yaml
kubectl -n gamerec logs -f job/gamerec-fill
```

## Checking that it works

```
./deploy/vm/smoke.sh https://<your host>
```

Four things fail independently, so the script checks them separately: `/livez` (the process),
`/readyz` (MinDB behind it, retried for half a minute because a rollout is downtime measured in
seconds), `/health` (dimensions, stamp, the kernel MinDB chose, and whether the index is empty) and
one `/recommend?narrate=false`. It is read-only and safe against production — one embedding, no
writes. An empty index is a failure rather than a pass: it answers every query with nothing while
looking perfectly healthy.

Run it after a bootstrap, after a rollout and after a restore. Before TLS exists, point it at a
`kubectl port-forward` instead of the hostname.

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
documented behaviour of k3s, certbot and Azure. The manifests, the backup sidecar's code path and the
restore have all been run; **this script has not yet been run end to end on the Azure VM**, so the
host-specific steps — the NSG rules, DuckDNS, certbot against a real name — are the parts most likely
to need a correction on first contact. The script is idempotent so that correcting it is cheap: fix,
`git pull`, run it again.
