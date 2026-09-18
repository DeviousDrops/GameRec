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
| Commit | `0716f7835d5f7cb2c50d1172fe6f55cccd5514a7` |
| flatc | `25.12.19` (matches MinDB's `github.com/google/flatbuffers v25.12.19` and the Python `flatbuffers` runtime) |

**This pin is provisional.** MinDB has no tagged release yet, so the schema is pinned to a commit rather
than a tag. Per D19 it must move to a released tag before deployment, alongside the GHCR image.

## Regenerating

```bash
scripts/gen-mindb-client.sh
```
