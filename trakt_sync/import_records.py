"""Durable Trakt history IDs and the records still waiting for a MovieClaw target."""

from collections import defaultdict


class ImportRecords:
    def __init__(self, store, member, namespace):
        self.store = store
        self.pending_scope = f"trakt_pending:{member}"
        self.receipt_scope = f"trakt_receipts:{member}"
        self.namespace = namespace
        self.receipts = {}

    @staticmethod
    def key(record_id):
        # At most 128 records per value, well below the host's 64 KiB limit.
        return str(int(record_id) // 128)

    async def receipt_bucket(self, key):
        if key not in self.receipts:
            self.receipts[key] = await self.store.get(key, scope=self.receipt_scope, default={})
        return self.receipts[key]

    async def done(self, record_id):
        bucket = await self.receipt_bucket(self.key(record_id))
        return bucket.get(str(record_id)) == self.namespace

    async def finish(self, record_ids):
        grouped = defaultdict(list)
        for record_id in record_ids:
            grouped[self.key(record_id)].append(str(record_id))
        for key, ids in grouped.items():
            bucket = await self.receipt_bucket(key)
            updated = {**bucket, **dict.fromkeys(ids, self.namespace)}
            if updated != bucket:
                await self.store.set(key, updated, scope=self.receipt_scope, secret=True)
                self.receipts[key] = updated

    async def pending(self):
        return {
            record_id: record
            for bucket in (await self.store.items(scope=self.pending_scope)).values()
            for record_id, (namespace, record) in bucket.items()
            if namespace == self.namespace
        }

    async def save_pending(self, before, after):
        changed = {
            self.key(record_id)
            for record_id in before.keys() | after.keys()
            if before.get(record_id) != after.get(record_id)
        }
        buckets = {key: {} for key in changed}
        for record_id, record in after.items():
            if self.key(record_id) in buckets:
                buckets[self.key(record_id)][record_id] = [self.namespace, record]
        for key, bucket in buckets.items():
            if bucket:
                await self.store.set(key, bucket, scope=self.pending_scope, secret=True)
            else:
                await self.store.delete(key, scope=self.pending_scope)

    async def clear(self):
        for scope in (self.pending_scope, self.receipt_scope):
            for key in await self.store.items(scope=scope):
                await self.store.delete(key, scope=scope)
