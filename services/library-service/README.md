# library-service

Which Steam games each user owns or has wishlisted. GameRec's recommender asks it for a user's owned
appids and leaves those games out of the results (D67). Spring Boot 3.5, Java 21, PostgreSQL 17.

```
GameRec API ── GET /library/ids?status=OWNED, 300 ms timeout ──► library-service ×2 ──► Postgres
     │                                                              (Deployment)         (StatefulSet)
     └─ on any failure: recommend unfiltered, log a warning, report library_filtered=false
```

## API

Everything is under `/api/v1/users/{userId}/library`. `userId` is 1–64 of `A-Z a-z 0-9 _ -`, which a
SteamID64 fits; `appId` is a positive Steam appid. Swagger UI is at `/swagger-ui.html`.

| Method | Path | Answers |
|---|---|---|
| `PUT` | `/{appId}` with `{"status":"OWNED"\|"WISHLIST"}` | `201` + `Location` if new, `200` if it existed |
| `GET` | `?status=&page=0&size=20` | a page of entries, newest first; `size` at most 100 |
| `GET` | `/ids?status=` | `{"appIds":[...]}`, every appid, for the recommender |
| `DELETE` | `/{appId}` | `204`, or `404` if it was not there |

PUT is idempotent: the same request twice leaves the same row, and a repeat with an unchanged status
does not even touch `updated_at`. Every error is an RFC 9457 problem detail
(`application/problem+json`), with an `errors` list naming each invalid field.

## Layout

```
controller/   HTTP: validation, status codes. No logic.
service/      the rules: upsert, list, ids, delete. Transactions start here.
repository/   Spring Data JPA; queries derived from method names.
entity/       LibraryEntry, mapped to library_entries; EntryStatus.
dto/          records for requests and responses, so entities never reach JSON.
exception/    EntryNotFoundException and the one @RestControllerAdvice.
resources/db/migration/V1__init.sql   the schema. Hibernate only validates it.
```

## Running it

```bash
docker compose up --build     # the service and Postgres, on :8080
./mvnw test                   # needs only a JDK 21; the wrapper fetches Maven
```

Outside Docker it needs `DB_URL`, `DB_USER` and `DB_PASSWORD`, and refuses to start without them.

Kubernetes is `k8s/`: create the Secret from `k8s/manual/secret.template.yaml` (it says how without
writing the password down), then `kubectl apply -f k8s/`, then set `LIBRARY_URL` in GameRec's
ConfigMap to `http://library-service:8080`. A tag `library-service-v0.1.0` publishes the image for
`linux/amd64` and `linux/arm64`.

## Things worth knowing

- **Both replicas run Flyway at boot.** Flyway takes a Postgres advisory lock, so one migrates and
  the other finds the schema up to date. Verified on a local cluster.
- **Liveness never checks the database; readiness does.** A Postgres outage takes the pods out of
  the Service and does not restart them. Verified by deleting the Postgres pod.
- **Two PUTs creating the same entry at once**: both find nothing, the unique constraint rejects
  the second insert, and it gets a `409` to retry.
- **Memory.** Each pod is ~280 MB resident at idle (measured), and Postgres ~36 MB; requests total
  ~770 Mi. On GameRec's 4 GiB VM that is a real share of what is left (D47, D67).
