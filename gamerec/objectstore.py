"""Object storage, narrowed to the five operations GameRec needs (D20).

R2 is S3-compatible, so this is boto3 underneath, but the interface is deliberately smaller than
boto3's: everything that touches object storage -- the Ingest Lease, Backup Generations, the document
store once it moves off the volume -- goes through `ObjectStore`, and tests substitute an in-memory
implementation. That keeps the S3 vocabulary at one edge of the codebase instead of spread through it.

The one operation that is not obvious is `put_if_absent`. It is the atomic create-if-absent the lease
is built on, and on S3 and R2 it is `PutObject` with `If-None-Match: *`.
"""

from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger("objectstore")

# A conflicting conditional write. R2 documents 412; AWS returns 409 when two writes race, and the
# code has to treat both as "somebody else got there first" (D20).
_CONFLICT_STATUSES = {409, 412}


class ObjectStore(Protocol):
    def put_if_absent(self, key: str, body: bytes) -> bool:
        """True if this call created the object, False if it already existed. Never overwrites."""

    def put(self, key: str, body: bytes) -> None: ...

    def get(self, key: str) -> bytes | None:
        """None when the key does not exist, so callers do not have to catch a vendor exception."""

    def delete(self, key: str) -> None: ...

    def list(self, prefix: str) -> list[str]: ...


class R2Store:
    """Cloudflare R2 over the S3 API. Credentials come from the environment (requirement 6)."""

    def __init__(self, endpoint: str, bucket: str, access_key_id: str, secret_access_key: str):
        import boto3  # Imported here so that nothing but the R2 path needs boto3 installed.
        from botocore.config import Config as BotoConfig

        self.bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            # R2 has one region and rejects the usual virtual-host addressing.
            region_name="auto",
            config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 5,
                                                                 "mode": "standard"}),
        )

    def _status(self, error) -> int:
        return int(error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))

    def put_if_absent(self, key: str, body: bytes) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.put_object(Bucket=self.bucket, Key=key, Body=body, IfNoneMatch="*")
            return True
        except ClientError as error:
            if self._status(error) in _CONFLICT_STATUSES:
                return False
            raise

    def put(self, key: str, body: bytes) -> None:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=body)

    def get(self, key: str) -> bytes | None:
        from botocore.exceptions import ClientError

        try:
            return self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as error:
            if self._status(error) == 404:
                return None
            raise

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)

    def list(self, prefix: str) -> list[str]:
        keys: list[str] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(item["Key"] for item in page.get("Contents", []))
        return keys


class MemoryStore:
    """For tests, and for running the ingest with no R2 configured.

    `put_if_absent` is atomic here for the trivial reason that there is no concurrency, which is
    exactly why the lease test against real R2 still matters: this cannot tell you whether R2
    implements the header correctly (D20).
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_if_absent(self, key: str, body: bytes) -> bool:
        if key in self.objects:
            return False
        self.objects[key] = body
        return True

    def put(self, key: str, body: bytes) -> None:
        self.objects[key] = body

    def get(self, key: str) -> bytes | None:
        return self.objects.get(key)

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    def list(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))


def from_config(config) -> ObjectStore | None:
    """An R2 store when the environment describes one, None when it does not.

    None is a supported state, not an error: a developer running the ingest locally has no R2, and
    the caller decides what to do without it. The ingest, for instance, warns that it is running
    without a lease rather than refusing to run at all.
    """
    if not (config.r2_endpoint and config.r2_bucket and config.r2_access_key_id):
        return None
    return R2Store(config.r2_endpoint, config.r2_bucket,
                   config.r2_access_key_id, config.r2_secret_access_key)
