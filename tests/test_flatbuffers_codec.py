"""Proves a Python client can speak to MinDB's forced FlatBuffers codec.

This is the assumption the whole data path rests on: grpcio has no FlatBuffers support, so the client
sends raw `builder.Output()` bytes through an unserialised `unary_unary` channel and trusts the Go
server's `ForceServerCodec` to decode them. See clients/README.md.

Needs any running MinDB. Vector width is read from Stats rather than hardcoded, so the test works
against the dev stack (384) as well as a throwaway server. Set MINDB_ADDR, or the test skips.
"""
import os
import sys

import flatbuffers
import grpc
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "clients", "generated"))

from mindb.InsertRequest import (  # noqa: E402
    InsertRequestAddVectors,
    InsertRequestEnd,
    InsertRequestStart,
    InsertRequestStartVectorsVector,
)
from mindb.InsertResponse import InsertResponse  # noqa: E402
from mindb.SearchRequest import (  # noqa: E402
    SearchRequestAddQueryVector,
    SearchRequestAddTopK,
    SearchRequestEnd,
    SearchRequestStart,
    SearchRequestStartQueryVectorVector,
)
from mindb.DeleteRequest import (  # noqa: E402
    DeleteRequestAddIds, DeleteRequestEnd, DeleteRequestStart, DeleteRequestStartIdsVector,
)
from mindb.SearchResponse import SearchResponse  # noqa: E402
from mindb.StatsRequest import StatsRequestEnd, StatsRequestStart  # noqa: E402
from mindb.StatsResponse import StatsResponse  # noqa: E402
from mindb.Vector import (  # noqa: E402
    VectorAddId,
    VectorAddPayload,
    VectorAddValues,
    VectorEnd,
    VectorStart,
    VectorStartValuesVector,
)

ADDR = os.environ.get("MINDB_ADDR", "127.0.0.1:50051")
TEST_IDS = ["codec-test:1", "codec-test:2"]


def _insert_request(items):
    b = flatbuffers.Builder(1024)
    offsets = []
    for vector_id, values, payload in items:
        id_off = b.CreateString(vector_id)
        payload_off = b.CreateByteVector(payload)
        VectorStartValuesVector(b, len(values))
        for v in reversed(values):
            b.PrependFloat32(v)
        values_off = b.EndVector()
        VectorStart(b)
        VectorAddId(b, id_off)
        VectorAddValues(b, values_off)
        VectorAddPayload(b, payload_off)
        offsets.append(VectorEnd(b))
    InsertRequestStartVectorsVector(b, len(offsets))
    for off in reversed(offsets):
        b.PrependUOffsetTRelative(off)
    vectors_off = b.EndVector()
    InsertRequestStart(b)
    InsertRequestAddVectors(b, vectors_off)
    b.Finish(InsertRequestEnd(b))
    return bytes(b.Output())


def _delete_request(ids):
    b = flatbuffers.Builder(256)
    offsets = [b.CreateString(i) for i in ids]
    DeleteRequestStartIdsVector(b, len(offsets))
    for off in reversed(offsets):
        b.PrependUOffsetTRelative(off)
    ids_off = b.EndVector()
    DeleteRequestStart(b)
    DeleteRequestAddIds(b, ids_off)
    b.Finish(DeleteRequestEnd(b))
    return bytes(b.Output())


def _stats_request():
    b = flatbuffers.Builder(64)
    StatsRequestStart(b)
    b.Finish(StatsRequestEnd(b))
    return bytes(b.Output())


def _one_hot(dims, index):
    values = [0.0] * dims
    values[index] = 1.0
    return values


def _search_request(query, top_k):
    b = flatbuffers.Builder(256)
    SearchRequestStartQueryVectorVector(b, len(query))
    for v in reversed(query):
        b.PrependFloat32(v)
    query_off = b.EndVector()
    SearchRequestStart(b)
    SearchRequestAddQueryVector(b, query_off)
    SearchRequestAddTopK(b, top_k)
    b.Finish(SearchRequestEnd(b))
    return bytes(b.Output())


@pytest.fixture(scope="module")
def rpc():
    channel = grpc.insecure_channel(ADDR)
    try:
        grpc.channel_ready_future(channel).result(timeout=5)
    except grpc.FutureTimeoutError:
        pytest.skip(f"no MinDB at {ADDR}; set MINDB_ADDR")
    stats = StatsResponse.GetRootAs(
        channel.unary_unary("/mindb.VectorService/Stats")(_stats_request()), 0
    )
    yield (
        channel.unary_unary("/mindb.VectorService/Insert"),
        channel.unary_unary("/mindb.VectorService/Search"),
        stats.Dims(),
    )
    # These run against whatever MinDB is to hand, including the dev corpus, so clean up after.
    channel.unary_unary("/mindb.VectorService/Delete")(_delete_request(TEST_IDS))
    channel.close()


def test_insert_and_search_round_trip(rpc):
    insert, search, dims = rpc
    items = [
        ("codec-test:1", _one_hot(dims, 0), b'{"name":"Half-Life 2"}'),
        ("codec-test:2", _one_hot(dims, 1), b'{"name":"Portal"}'),
    ]
    response = InsertResponse.GetRootAs(insert(_insert_request(items)), 0)
    assert response.InsertedCount() == 2

    query = _one_hot(dims, 0)
    query[1] = 0.1
    results = SearchResponse.GetRootAs(search(_search_request(query, 2)), 0)
    assert results.ResultsLength() == 2
    top = results.Results(0)
    assert top.Id().decode() == "codec-test:1"
    # The payload survives the round trip intact, which is what carries game metadata.
    assert bytes(top.PayloadAsNumpy()) == b'{"name":"Half-Life 2"}'


def test_insert_is_an_upsert_keyed_by_id(rpc):
    """Idempotent ingest depends on this: re-inserting an id overwrites rather than duplicating."""
    insert, search, dims = rpc
    probe, moved_to = _one_hot(dims, 0), _one_hot(dims, dims - 1)
    before = SearchResponse.GetRootAs(search(_search_request(probe, 1000)), 0)

    insert(_insert_request([("codec-test:1", moved_to, b'{"name":"Half-Life 2 (v2)"}')]))

    after = SearchResponse.GetRootAs(search(_search_request(probe, 1000)), 0)
    assert after.ResultsLength() == before.ResultsLength(), "upsert added a row instead of replacing"

    moved = SearchResponse.GetRootAs(search(_search_request(moved_to, 1)), 0)
    top = moved.Results(0)
    assert top.Id().decode() == "codec-test:1"
    assert bytes(top.PayloadAsNumpy()) == b'{"name":"Half-Life 2 (v2)"}'
