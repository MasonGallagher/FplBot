"""Persistence adapters: DynamoDB for state, S3 for raw payloads.

The split is deliberate. DynamoDB holds small, queryable, structured state that
the running system needs (snapshots, aliases, locks, last-known-good pointers).
S3 holds large, immutable, verbatim bytes that only humans and backtests read.

Putting the raw 3 MB bootstrap payload in DynamoDB would work - gzipped it fits
under the 400 KB item limit - and indeed we do keep a compressed snapshot there
for the transfer-velocity series. But the *archive* belongs in S3, where it is a
tenth of the price and where a backtest can stream a season of it without
burning read capacity.
"""

from fplbot.storage.dynamo import DynamoStore, LockAlreadyHeld
from fplbot.storage.keys import Keys
from fplbot.storage.s3 import RawArchive

__all__ = ["DynamoStore", "Keys", "LockAlreadyHeld", "RawArchive"]
