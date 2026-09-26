# MinDB client

MinDB's data path is FlatBuffers over gRPC. The server runs
`grpc.ForceServerCodec(flatbuffers.FlatbuffersCodec{})`, which is a raw passthrough:

| | |
|---|---|
| `Marshal` | `Builder.FinishedBytes()` |
| `Unmarshal` | `GetUOffsetT(data)`, then `Init(data, off)` |

There is no envelope and no length prefix — the gRPC message body *is* the finished flatbuffer. On the
Python side that means calling `channel.unary_unary(method)` with **no** `request_serializer` or
`response_deserializer`, so grpcio passes `bytes` straight through, and parsing replies with
`GetRootAs(buf, 0)`.

Because `FlatbuffersCodec.Name()` returns `"flatbuffers"`, a Go client negotiates
`application/grpc+flatbuffers`. grpcio sends `application/grpc+proto` instead, but `ForceServerCodec`
ignores the subtype and decodes with the forced codec anyway. `tests/test_flatbuffers_codec.py` is the
proof; if that test ever fails, this assumption is what broke.

## Provenance

`mindb.fbs` is copied verbatim from the MinDB repo. Nothing here is edited by hand.

| | |
|---|---|
| Source | `github.com/DeviousDrops/mindb` — `fbs/mindb.fbs` |
| Tag | `v0.1.0` (commit `282bccd73d3338a2ca5b6b4f43653f9699f78bf1`) |
| Image | `ghcr.io/deviousdrops/mindb:v0.1.0` — multi-arch, `linux/amd64` and `linux/arm64` |
| flatc | `25.12.19` (matches MinDB's `github.com/google/flatbuffers v25.12.19` and the Python `flatbuffers` runtime) |

The pin is a released tag, and the image is built from that same tag, so the schema this client was
generated from and the server it talks to cannot drift apart. `v0.1.0`'s schema is **byte-identical** to
the commit the bindings were generated from, so moving the pin required no regeneration — checked by
diffing `fbs/mindb.fbs` at the tag against this copy.

MinDB's repo owner and Go module path both moved to `DeviousDrops` before the tag. The old
`typicallhavok` URLs still redirect, but nothing here should use them.

The schema carries `Get` and `Stats` (D8) and the `wal_enabled` / `wal_healthy` fields. GameRec uses
`Stats.kernel_name` and `goarch` for `/health`, so benchmark labels come from the server rather than an
assumption. `v0.1.0` implements the write-ahead log, so those two fields now describe a live log instead
of reading false; it also serves `grpc.health.v1.Health` on `:50052` (`-health-addr`), which is the
readiness probe Phase 3 should use.

## Regenerating

```bash
scripts/gen-mindb-client.sh
```
