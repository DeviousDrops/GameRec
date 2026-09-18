"""Proves a Python client can speak to MinDB's forced FlatBuffers codec.

This is the assumption the whole data path rests on: grpcio has no FlatBuffers support, so the client
sends raw `builder.Output()` bytes through an unserialised `unary_unary` channel and trusts the Go
server's `ForceServerCodec` to decode them. See clients/README.md.

Needs a MinDB server with dims=4. Set MINDB_ADDR, or the test skips.
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
from mindb.SearchResponse import SearchResponse  # noqa: E402
from mindb.Vector import (  # noqa: E402
    VectorAddId,
    VectorAddPayload,
    VectorAddValues,
    VectorEnd,
    VectorStart,
    VectorStartValuesVector,
)

ADDR = os.environ.get("MINDB_ADDR", "127.0.0.1:50051")


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
    yield (
        channel.unary_unary("/mindb.VectorService/Insert"),
        channel.unary_unary("/mindb.VectorService/Search"),
    )
    channel.close()


def test_insert_and_search_round_trip(rpc):
    insert, search = rpc
    items = [
        ("codec-test:1", [1.0, 0.0, 0.0, 0.0], b'{"name":"Half-Life 2"}'),
        ("codec-test:2", [0.0, 1.0, 0.0, 0.0], b'{"name":"Portal"}'),
    ]
    response = InsertResponse.GetRootAs(insert(_insert_request(items)), 0)
    assert response.InsertedCount() == 2

    results = SearchResponse.GetRootAs(search(_search_request([0.9, 0.1, 0.0, 0.0], 2)), 0)
    assert results.ResultsLength() == 2
    top = results.Results(0)
    assert top.Id().decode() == "codec-test:1"
    # The payload survives the round trip intact, which is what carries game metadata.
    assert bytes(top.PayloadAsNumpy()) == b'{"name":"Half-Life 2"}'


def test_insert_is_an_upsert_keyed_by_id(rpc):
    """Idempotent ingest depends on this: re-inserting an id overwrites rather than duplicating."""
    insert, search = rpc
    before = SearchResponse.GetRootAs(search(_search_request([1.0, 0.0, 0.0, 0.0], 100)), 0)

    insert(_insert_request([("codec-test:1", [0.0, 0.0, 0.0, 1.0], b'{"name":"Half-Life 2 (v2)"}')]))

    after = SearchResponse.GetRootAs(search(_search_request([1.0, 0.0, 0.0, 0.0], 100)), 0)
    assert after.ResultsLength() == before.ResultsLength(), "upsert added a row instead of replacing"

    moved = SearchResponse.GetRootAs(search(_search_request([0.0, 0.0, 0.0, 1.0], 1)), 0)
    top = moved.Results(0)
    assert top.Id().decode() == "codec-test:1"
    assert bytes(top.PayloadAsNumpy()) == b'{"name":"Half-Life 2 (v2)"}'
