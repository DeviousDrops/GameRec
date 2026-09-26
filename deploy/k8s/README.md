# Kubernetes manifests

Plain manifests, applied with `kubectl`. No Helm, no operators, no kustomize overlays — the whole
deployment is readable in one sitting, which is the point.

```
                          ┌──────────────────────────┐
   kubectl port-forward   │  Service gamerec-api     │   ClusterIP only; the Ingress arrives
   ──────────────────────▶│  (2 replicas, rolling)   │   with the VM and TLS in Phase 4
                          └────────┬────────┬────────┘
                                   │        │ reads /corpus (ro)
                          gRPC :50051       │
                                   ▼        ▼
                          ┌──────────────┐  ┌────────────────────────┐
                          │ mindb        │  │ PVC corpus             │
                          │ Recreate, 1  │  │ documents.jsonl        │
                          │ PVC mindb-   │  │ names.json             │
                          │ data         │  │ checkpoint.json        │
                          └──────────────┘  └───────────▲────────────┘
                                   ▲                    │ writes
                                   │ gRPC :50051        │
                          ┌────────┴────────────────────┴────────────┐
                          │ CronJob gamerec-ingest, 03:17 UTC, Forbid│
                          └──────────────────────────────────────────┘
```

| File | What it is |
|---|---|
| `00-namespace.yaml` | everything lives in `gamerec`, so deleting the namespace is a full teardown |
| `10-config.yaml` | non-secret config, mirroring `gamerec/config.py` |
| `15-corpus.yaml` | the corpus PVC: source of truth, written by ingest, read by the API |
| `20-mindb.yaml` | MinDB PVC, Deployment (`Recreate`) and Service |
| `25-mindb-netpol.yaml` | MinDB has no auth, so reachability is the access control |
| `30-api.yaml` | the API Deployment and Service |
| `40-ingest.yaml` | the nightly ingest CronJob |
| `manual/` | one-off Jobs and the Secret example — deliberately **not** applied by `-f deploy/k8s/` |

The numeric prefixes exist because `kubectl apply -f deploy/k8s/` applies files in name order, and the
namespace has to exist before anything in it. The apply is not recursive, which is what keeps
`manual/` out of it: the initial fill should not start because someone re-applied the directory, and
the Secret example should never overwrite a real key.

## Local cluster

```bash
k3d cluster create gamerec --agents 0

docker build -t ghcr.io/deviousdrops/gamerec-api:0.1.0 -f deploy/api.Dockerfile .
k3d image import ghcr.io/deviousdrops/gamerec-api:0.1.0 -c gamerec   # no registry until Phase 4 CI

kubectl apply -f deploy/k8s/
kubectl -n gamerec rollout status deploy/mindb
kubectl -n gamerec rollout status deploy/gamerec-api

# A Groq key is optional: without it the service answers and does not narrate (D6).
kubectl -n gamerec create secret generic gamerec-secrets --from-literal=GROQ_API_KEY=<key>

# Nothing is in the corpus yet. The nightly CronJob would fill it at 03:17, so trigger one now:
kubectl -n gamerec create job ingest-now --from=cronjob/gamerec-ingest
kubectl -n gamerec logs -f job/ingest-now

kubectl -n gamerec port-forward svc/gamerec-api 8000:8000
curl 'localhost:8000/health'
curl 'localhost:8000/recommend?q=relaxing+sandbox+building&k=3&narrate=false'
```

Teardown is `k3d cluster delete gamerec`.

## What was checked on a real cluster, not just dry-run

k3d v5.9.0, k3s v1.35.5, single node:

- **gRPC probes work against the distroless image.** MinDB logs `health service on :50052`, and the
  startup, readiness and liveness probes all use it — there is no shell in that image to exec a
  script in.
- **The ingest writes the PVC as an unprivileged user.** `fsGroup: 10001` is what makes that work;
  the run ingested 10 games, snapshotted, and left a `COMPLETE` checkpoint on the volume.
- **The API picked up a name index that did not exist when it started.** `name_index_size` went from
  0 to 10 with no restart, which is the reload the nightly CronJob depends on.
- **The NetworkPolicy blocks what it says it blocks.** From an unlabelled pod, `mindb:50051` times
  out and `mindb:50052` connects — data denied, health left open for the kubelet.
- **MinDB down is a 503, not a 500, and not an API restart.** With `mindb` scaled to zero,
  `/recommend` answers `503` with `retry-after: 5`, `/readyz` fails so the Service sheds traffic,
  `/livez` still answers `200`, and both API pods stay up with zero restarts. Scaling MinDB back
  restored all 10 vectors from the snapshot on its PVC.

## Deliberately not here yet

- **An Ingress, a hostname and TLS.** Phase 4, with the VM. An Ingress for a host nobody owns would
  be decoration.
- **A published image.** Phase 4's CI builds and pushes `gamerec-api`; until then the tag is imported
  into the local cluster by hand, which is why the pull policy is `IfNotPresent`.
- **Backups and the Ingest Lease.** Both need object storage (D20), so both land with R2 in Phase 4.
  Until then `concurrencyPolicy: Forbid` covers the scheduled ingest but not a manual run beside it.
- **More than one node.** The corpus PVC is `ReadWriteOnce`, and the API and ingest share it only
  because every pod lands on the same node. A second node breaks that, and the fix is R2 rather than
  a fight with `ReadWriteMany`.
