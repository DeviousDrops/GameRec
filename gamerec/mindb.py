"""Thin client over the generated FlatBuffers stubs.

grpcio has no FlatBuffers support, so every call goes through an unserialised unary_unary channel:
raw builder output in, raw bytes out. See clients/README.md for why that is correct.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

import flatbuffers
import grpc
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "clients", "generated"))

from mindb.GetRequest import (  # noqa: E402
    GetRequestAddIds, GetRequestEnd, GetRequestStart, GetRequestStartIdsVector,
)
from mindb.GetResponse import GetResponse  # noqa: E402
from mindb.InsertRequest import (  # noqa: E402
    InsertRequestAddVectors, InsertRequestEnd, InsertRequestStart, InsertRequestStartVectorsVector,
)
from mindb.InsertResponse import InsertResponse  # noqa: E402
from mindb.SearchRequest import (  # noqa: E402
    SearchRequestAddQueryVector, SearchRequestAddTopK, SearchRequestEnd,
    SearchRequestStart, SearchRequestStartQueryVectorVector,
)
from mindb.SearchResponse import SearchResponse  # noqa: E402
from mindb.SnapshotRequest import SnapshotRequestEnd, SnapshotRequestStart  # noqa: E402
from mindb.SnapshotResponse import SnapshotResponse  # noqa: E402
from mindb.StatsRequest import StatsRequestEnd, StatsRequestStart  # noqa: E402
from mindb.StatsResponse import StatsResponse  # noqa: E402
from mindb.Vector import (  # noqa: E402
    VectorAddId, VectorAddPayload, VectorAddValues, VectorEnd, VectorStart, VectorStartValuesVector,
)


@dataclass
class Hit:
    id: str
    score: float
    payload: bytes


@dataclass
class Stats:
    vector_count: int
    capacity: int
    dims: int
    kernel_name: str
    fast_int8: bool
    goarch: str
    wal_enabled: bool
    wal_healthy: bool


def _payload_bytes(obj) -> bytes:
    return bytes(obj.PayloadAsNumpy()) if obj.PayloadLength() else b""


class MinDBClient:
    def __init__(self, addr: str) -> None:
        self.addr = addr
        # MinDB caps a message at roughly 4 MiB by default; batch sizes are chosen to stay under it.
        self._channel = grpc.insecure_channel(addr)
        self._insert = self._channel.unary_unary("/mindb.VectorService/Insert")
        self._search = self._channel.unary_unary("/mindb.VectorService/Search")
        self._get = self._channel.unary_unary("/mindb.VectorService/Get")
        self._snapshot = self._channel.unary_unary("/mindb.VectorService/Snapshot")
        self._stats = self._channel.unary_unary("/mindb.VectorService/Stats")

    def wait_ready(self, timeout: float = 30.0) -> None:
        grpc.channel_ready_future(self._channel).result(timeout=timeout)

    def close(self) -> None:
        self._channel.close()

    def insert(self, items: list[tuple[str, np.ndarray, bytes]]) -> int:
        """Upsert. An id that already exists is overwritten in place."""
        b = flatbuffers.Builder(1024 * 64)
        offsets = []
        for vector_id, values, payload in items:
            id_off = b.CreateString(vector_id)
            payload_off = b.CreateByteVector(payload)
            VectorStartValuesVector(b, len(values))
            for v in reversed(values.tolist()):
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
        return InsertResponse.GetRootAs(self._insert(bytes(b.Output())), 0).InsertedCount()

    def search(self, query: np.ndarray, top_k: int) -> list[Hit]:
        b = flatbuffers.Builder(4096)
        SearchRequestStartQueryVectorVector(b, len(query))
        for v in reversed(query.tolist()):
            b.PrependFloat32(v)
        query_off = b.EndVector()
        SearchRequestStart(b)
        SearchRequestAddQueryVector(b, query_off)
        SearchRequestAddTopK(b, top_k)
        b.Finish(SearchRequestEnd(b))
        response = SearchResponse.GetRootAs(self._search(bytes(b.Output())), 0)
        out = []
        for i in range(response.ResultsLength()):
            r = response.Results(i)
            out.append(Hit(r.Id().decode(), r.Score(), _payload_bytes(r)))
        return out

    def get(self, ids: list[str]) -> dict[str, np.ndarray]:
        """Absent ids are omitted, so the result is keyed rather than positional."""
        b = flatbuffers.Builder(1024)
        offsets = [b.CreateString(i) for i in ids]
        GetRequestStartIdsVector(b, len(offsets))
        for off in reversed(offsets):
            b.PrependUOffsetTRelative(off)
        ids_off = b.EndVector()
        GetRequestStart(b)
        GetRequestAddIds(b, ids_off)
        b.Finish(GetRequestEnd(b))
        response = GetResponse.GetRootAs(self._get(bytes(b.Output())), 0)
        out = {}
        for i in range(response.VectorsLength()):
            v = response.Vectors(i)
            out[v.Id().decode()] = v.ValuesAsNumpy().astype(np.float32)
        return out

    def snapshot(self) -> tuple[bool, str]:
        b = flatbuffers.Builder(64)
        SnapshotRequestStart(b)
        b.Finish(SnapshotRequestEnd(b))
        response = SnapshotResponse.GetRootAs(self._snapshot(bytes(b.Output())), 0)
        message = response.Message()
        return response.Success(), message.decode() if message else ""

    def stats(self) -> Stats:
        b = flatbuffers.Builder(64)
        StatsRequestStart(b)
        b.Finish(StatsRequestEnd(b))
        s = StatsResponse.GetRootAs(self._stats(bytes(b.Output())), 0)
        kernel, arch = s.KernelName(), s.Goarch()
        return Stats(
            vector_count=s.VectorCount(),
            capacity=s.Capacity(),
            dims=s.Dims(),
            kernel_name=kernel.decode() if kernel else "",
            fast_int8=s.FastInt8(),
            goarch=arch.decode() if arch else "",
            wal_enabled=s.WalEnabled(),
            wal_healthy=s.WalHealthy(),
        )
