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
| Source | `github.com/typicallhavok/mindb` — `fbs/mindb.fbs` |
| Commit | `69a0c9c705c81b565f6d5b77fc7e16ca8a5789a3` (`v0.0.0-20260918114935-69a0c9c705c8`) |
| flatc | `25.12.19` (matches MinDB's `github.com/google/flatbuffers v25.12.19` and the Python `flatbuffers` runtime) |

**This pin is provisional.** MinDB has no tagged release yet, so the schema is pinned to a commit rather
than a tag. Per D19 it must move to a released tag before deployment, alongside the GHCR image.

This commit adds `Get` and `Stats` (D8) and the first `wal_enabled` / `wal_healthy` fields. GameRec uses
`Stats.kernel_name` and `goarch` for `/health`, so benchmark labels come from the server rather than an
assumption. The same commit also runs a standard `grpc.health.v1.Health` service on `:50052`, which is
the readiness probe Phase 3 should use.

## Regenerating

```bash
scripts/gen-mindb-client.sh
```
