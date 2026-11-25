"""NAS-side, opt-in reconciliation of Trakt viewing facts."""

import asyncio
from collections import defaultdict
from datetime import datetime

from movieclaw_sdk.ops import OpsError

from .activity import watch_activity
from .client import TraktError
from .import_records import ImportRecords
from .policy import identity, iso_time, matches, timestamp, trakt_minute
from .transfers import Transfers, digest, unit_key

KINDS = ("movies", "episodes")
POLL = 300
CATALOG_CHECK = 1800
SNAPSHOT_PAGE = 400
TARGET_LIMIT = 12


class Superseded(Exception):
    """The member turned off import or changed their account while a scan was running."""


class RetryScan(Exception):
    """A snapshot changed during reconciliation."""


def external_ids(item):
    return item.get("movie", item.get("show", {})).get("ids", {})


def match_keys(kind, ids, season, episode):
    return [
        (kind, name, str(ids[name]), season, episode) for name in ("tmdb", "imdb") if ids.get(name)
    ]


def remote_unit(kind, row):
    title = row.get("movie" if kind == "movies" else "show", {})
    episode = row.get("episode", {})
    return (
        title.get("ids", {}),
        episode.get("season") if kind == "episodes" else 0,
        episode.get("number") if kind == "episodes" else 0,
    )


class BackgroundImport:
    def __init__(self, engine, ops):
        self.e, self.ops = engine, ops
        self.wake = asyncio.Event()
        self.active = {}
        self.due = {}
        self.retry = {}
        self.requested = set()
        self.library_revision = 0
        self.catalog_revision = {}

    def inventory_changed(self):
        self.library_revision += 1
        self.wake.set()

    def request(self, member):
        self.requested.add(member)
        self.wake.set()

    async def status(self, member):
        state = await self.e.get(member, "background_import", {})
        return {
            "running": member in self.active,
            "last_checked": state.get("last_checked"),
            "last_imported": state.get("last_imported"),
            "imported": state.get("imported", 0),
            "protected": state.get("protected", 0),
            "error": state.get("error"),
            "retry_at": state.get("retry_at", 0),
        }

    async def forget(self, member):
        async with self.e.locks[member]:
            if await self.e.get(member, "automatic_import_binding"):
                return
            for name in [
                "inventory",
                *(f"{side}_{kind}" for side in ("local", "remote") for kind in KINDS),
            ]:
                key = f"background_{name}"
                meta = await self.e.get(member, key, {})
                for i in range(meta.get("allocated", meta.get("pages", 0))):
                    await self.e.store.delete(f"{key}_{i}", scope=self.e.scope(member))
                await self.e.store.delete(key, scope=self.e.scope(member))
            await ImportRecords(self.e.store, member, "").clear()
            await self.e.store.delete("background_import", scope=self.e.scope(member))
            self.requested.discard(member)
            self.due.pop(member, None)
            self.retry.pop(member, None)
            self.catalog_revision.pop(member, None)

    async def account(self, member, generation):
        account = await self.e.get(member, "account", {})
        binding = await self.e.get(member, "automatic_import_binding", {})
        if (
            not self.e.enabled
            or account.get("generation") != generation
            or not account.get("access_token")
            or account.get("blocked")
            or account.get("client_id") != self.e.client.client_id
            or binding.get("generation") != generation
        ):
            raise Superseded
        return account

    async def member_identity(self, member):
        if member == 0:
            return "admin"
        profile = await self.ops.call("members.show", {"member_id": member})
        if profile.get("id") != member or profile.get("status") != "active":
            raise RetryScan("MovieClaw 成员当前不可用")
        created = profile.get("created_at")
        if not isinstance(created, str) or not created:
            raise RetryScan("无法确认 MovieClaw 成员身份")
        return digest([member, created])

    async def verify_member(self, member):
        binding = await self.e.get(member, "automatic_import_binding", {})
        owner = await self.member_identity(member)
        if owner != binding.get("member"):
            raise RetryScan("MovieClaw 成员已更换，请关闭后台导入并核对 Trakt 绑定")
        return owner

    async def fetch(self, member, generation, path, params=None):
        # OAuth rotation is shared with outgoing delivery. Release the lock between requests.
        async with self.e.locks[member]:
            account = await self.account(member, generation)
            return await self.e.authorized(member, account, "GET", path, params=params)

    async def remember_unwatch(self, event, when):
        member = event["member_id"]
        async with self.e.locks[member]:
            if not self.e.enabled:
                return
            owner = await self.member_identity(member)
            media = event["media"]
            for unit in event.get("units") or [None]:
                item = identity({"media": media, "unit": unit})
                if item is None:
                    continue
                for key in self.e.unwatch_keys(item, prefix="import_unwatch"):
                    old = await self.e.get(member, key, {})
                    if old.get("owner") != owner or timestamp(old["at"]) < timestamp(when):
                        await self.e.set(member, key, {"owner": owner, "at": when})

    async def protected(self, member, owner, unit, item, when):
        if unit["played"]:
            return False
        marks = []
        for key in self.e.unwatch_keys(item, prefix="import_unwatch"):
            mark = await self.e.get(member, key, {})
            if mark.get("owner") == owner:
                marks.append(timestamp(mark["at"]))
        if marks:
            return timestamp(when) <= max(marks)
        return False

    async def snapshot(self, member, name, generation):
        meta = await self.e.get(member, f"background_{name}", {})
        if meta.get("generation") != generation:
            return None
        result = {}
        for i in range(meta["pages"]):
            page = await self.e.get(member, f"background_{name}_{i}")
            if not isinstance(page, dict):
                return None
            result.update(page)
        return result if meta.get("checksum") == digest(result) else None

    async def save_snapshot(self, member, name, generation, values, previous):
        # PluginData caps each JSON value at 64 KiB. Each entry contains only a key and hash.
        prefix = f"background_{name}"
        meta = await self.e.get(member, prefix, {})
        allocated = meta.get("allocated", meta.get("pages", 0))
        entries = sorted(values.items())
        old_entries = sorted((previous or {}).items())
        pages = (len(entries) + SNAPSHOT_PAGE - 1) // SNAPSHOT_PAGE
        if pages > allocated:
            # Make even an interrupted expansion discoverable for cleanup on disconnect.
            await self.e.set(member, prefix, {**meta, "allocated": pages})
        for i in range(pages):
            start, end = i * SNAPSHOT_PAGE, (i + 1) * SNAPSHOT_PAGE
            if entries[start:end] != old_entries[start:end]:
                await self.e.set(member, f"{prefix}_{i}", dict(entries[start:end]), secret=True)
        for i in range(pages, allocated):
            await self.e.store.delete(f"{prefix}_{i}", scope=self.e.scope(member))
        await self.e.set(
            member, prefix, {"generation": generation, "pages": pages, "checksum": digest(values)}
        )

    async def catalog(self, member):
        cursor = None
        index = defaultdict(list)
        units = {kind: {} for kind in KINDS}
        while True:
            page = await self.ops.call(
                "playback.sync.catalog", {"member_id": member, "cursor": cursor, "limit": 100}
            )
            for unit in page["items"]:
                item = identity({"media": unit["media"], "unit": unit})
                if not item:
                    continue
                kind = "movies" if "movie" in item else "episodes"
                units[kind][unit_key(unit)] = unit
                for key in match_keys(kind, external_ids(item), unit["season"], unit["episode"]):
                    index[key].append(unit)
            next_cursor = page["next_cursor"]
            if next_cursor is None:
                return units, index
            if cursor and tuple(map(int, next_cursor.split(":"))) <= tuple(
                map(int, cursor.split(":"))
            ):
                raise RetryScan("MovieClaw 媒体库分页未前进")
            cursor = next_cursor

    async def paged_rows(self, member, generation, path, params=None, *, until_empty=False):
        page, seen_pages = 1, set()
        while True:
            rows = await self.fetch(
                member, generation, path, {**(params or {}), "page": page, "limit": 100}
            )
            if not isinstance(rows, list):
                raise TraktError(502)
            marker = digest(rows)
            if rows and marker in seen_pages:
                raise RetryScan("Trakt 返回了重复分页")
            seen_pages.add(marker)
            for row in rows:
                yield row
            pages = getattr(rows, "page_count", 0)
            if not rows or (pages and page >= pages) or (not pages and not until_empty):
                return
            page += 1

    async def watched_snapshot(self, member, generation, kind):
        # Trakt exposes last_updated_at by title, independent of the viewing date.
        item_type = "movie" if kind == "movies" else "show"
        result = {}
        try:
            async for row in self.paged_rows(
                member,
                generation,
                f"/sync/watched/{item_type}s",
                {"extended": "noseasons"} if item_type == "show" else None,
            ):
                trakt_id = row.get(item_type, {}).get("ids", {}).get("trakt")
                updated = row.get("last_updated_at")
                if type(trakt_id) is not int or trakt_id <= 0 or not isinstance(updated, str):
                    return None
                result[str(trakt_id)] = updated
        except TraktError as exc:
            if exc.status in {400, 404, 422, 502}:
                return None
            raise
        return result

    @staticmethod
    def match(index, kind, ids, season, episode):
        found = {}
        for key in match_keys(kind, ids, season, episode):
            for unit in index[key]:
                local = external_ids(identity({"media": unit["media"], "unit": unit}))
                if all(local[k] == ids[k] for k in local.keys() & ids.keys()):
                    found[unit_key(unit)] = unit
        return next(iter(found.values())) if len(found) == 1 else None

    async def fetch_history(self, member, generation, store, kind, targets):
        canonical = f"/sync/history/{kind}"
        pending = {}
        try:
            for target in sorted(targets or {""}):
                title_kind = "movies" if kind == "movies" else "shows"
                path = f"/sync/history/{title_kind}/{target}" if target else canonical
                async for row in self.paged_rows(member, generation, path, until_empty=True):
                    record_id = row.get("id")
                    if type(record_id) is not int or not 0 < record_id < 2**64:
                        raise TraktError(502)
                    if await store.done(record_id):
                        continue
                    when = row.get("watched_at")
                    try:
                        valid_time = datetime.fromisoformat(when.replace("Z", "+00:00"))
                        if valid_time.tzinfo is None or len(when) > 40:
                            raise ValueError
                    except (ValueError, AttributeError):
                        raise TraktError(502) from None
                    ids, season, episode = remote_unit(kind, row)
                    ids = {
                        name: value
                        for name, value in ids.items()
                        if name in {"imdb", "tmdb"}
                        and (
                            (type(value) is int and 0 < value < 2**64)
                            or (
                                isinstance(value, str)
                                and value.isascii()
                                and value.isalnum()
                                and len(value) <= 32
                            )
                        )
                    }
                    if not all(
                        type(number) is int and 0 <= number <= 2**31 - 1
                        for number in (season, episode)
                    ):
                        raise TraktError(502)
                    item = row.get("movie" if kind == "movies" else "show", {})
                    remote_id = item.get("ids", {}).get("trakt")
                    if type(remote_id) is not int or not 0 < remote_id < 2**64:
                        remote_id = None
                    record = [
                        kind,
                        ids,
                        season,
                        episode,
                        when,
                        remote_id,
                        "scrobble" if row.get("action") == "scrobble" else None,
                    ]
                    record_id = str(record_id)
                    if record_id in pending and pending[record_id] != record:
                        raise RetryScan("Trakt 返回了相互冲突的观看记录")
                    pending[record_id] = record
        except TraktError as exc:
            if targets is None or exc.status not in {400, 404, 422}:
                raise
            return await self.fetch_history(member, generation, store, kind, None)
        return pending, targets

    async def apply(self, member, generation, records, candidates):
        applied = held = 0
        versions = {}
        conflict = False
        finished = set()
        for start in range(0, len(candidates), 100):
            async with self.e.locks[member]:
                account = await self.account(member, generation)
                owner = await self.verify_member(member)
                pending_scrobbles = [
                    job["scrobble_window"]
                    for job in (await self.e.ledger(member))["queue"]
                    if job["generation"] == generation and job.get("scrobble_window")
                ]
                batch, activity, done = [], [], set()
                for unit, source_id, when, remote_id, action in candidates[start : start + 100]:
                    key = unit_key(unit)
                    if key in versions and versions[key] is False:
                        continue
                    item = identity({"media": unit["media"], "unit": unit})
                    if await self.e.outbound_scrobble(member, account, remote_id, item):
                        done.add(remote_id)
                        continue
                    if action == "scrobble" and any(
                        matches(item, pending["item"])
                        and trakt_minute(pending["start"])
                        <= trakt_minute(when)
                        <= trakt_minute(pending["end"])
                        for pending in pending_scrobbles
                    ):
                        # Wait for the outbound receipt before advancing the import checkpoint.
                        conflict = True
                        continue
                    if any(
                        w["basis"] in {"playback", "state"}
                        and trakt_minute(when) == trakt_minute(w["watched_at"])
                        for w in unit["watches"]
                    ):
                        done.add(remote_id)
                        continue
                    if await self.protected(member, owner, unit, item, when):
                        held += 1
                        done.add(remote_id)
                        activity.append(
                            watch_activity(
                                "held:" + source_id,
                                unit["media"],
                                unit["season"],
                                unit["episode"],
                                when,
                                iso_time(self.e.clock()),
                                event="automatic_import",
                                state="skipped",
                                reason="本地取消已看的时间较新",
                            )
                        )
                        continue
                    media = unit["media"]
                    batch.append(
                        (
                            key,
                            media,
                            remote_id,
                            {
                                "media_item_id": media["id"],
                                "kind": media["kind"],
                                "tmdb_id": media.get("tmdb_id"),
                                "imdb_id": media.get("imdb_id"),
                                "season": unit["season"],
                                "episode": unit["episode"],
                                "source_id": source_id,
                                "watched_at": when,
                                "expected_updated_at": versions.get(key, unit["updated_at"]),
                            },
                        )
                    )
                if not batch:
                    await self.e.record_activities(member, activity, inbound=True)
                    await records.finish(done)
                    finished.update(done)
                    continue
                result = await self.ops.call(
                    "playback.sync.import",
                    {"member_id": member, "records": [row for _, _, _, row in batch]},
                )
                for (key, media, remote_id, row), outcome in zip(
                    batch, result["outcomes"], strict=True
                ):
                    status = outcome["status"]
                    if status == "applied":
                        applied += 1
                        done.add(remote_id)
                        activity.append(
                            watch_activity(
                                "import:" + row["source_id"],
                                media,
                                row["season"],
                                row["episode"],
                                row["watched_at"],
                                outcome["state_version"],
                                event="automatic_import",
                            )
                        )
                        if versions.get(key) is not False:
                            versions[key] = outcome["state_version"]
                    elif status == "already":
                        done.add(remote_id)
                    elif status in {"conflict", "not_found"}:
                        versions[key] = False
                        conflict = True
                    elif status == "mismatch":
                        # This also covers metadata changing after the catalog was read.
                        # Keep it pending for the next change to the catalog or this title.
                        versions[key] = False
                    else:
                        raise RetryScan("MovieClaw 返回了未知的导入结果")
                await self.e.record_activities(member, activity, inbound=True)
                await records.finish(done)
                finished.update(done)
        return applied, held, conflict, finished

    async def reconcile(self, member, generation, state, inventory_due):
        owner = await self.verify_member(member)
        stamp = await self.activities(member, generation)
        initial = not state.get("record_ledger")
        previous_stamp = state.get("stamp", {})
        changed = {kind for kind in KINDS if initial or stamp[kind] != previous_stamp.get(kind)}
        if not (inventory_due or changed or state.get("attempt") or state.get("pending_retry")):
            return state
        async with self.e.locks[member]:
            await self.account(member, generation)
            namespace = await self.e.import_identity(member)
        records = ImportRecords(self.e.store, member, digest([owner, namespace]))
        revision = self.library_revision
        before = await records.pending()
        if not (changed or before or state.get("pending_retry")):
            self.catalog_revision[member] = revision
            state["catalog_due"] = self.e.clock() + CATALOG_CHECK
            return state
        units, index = await self.catalog(member)
        inventory = {
            unit_key(unit): digest(external_ids(identity({"media": unit["media"], "unit": unit})))[
                :32
            ]
            for group in units.values()
            for unit in group.values()
        }
        previous_inventory = await self.snapshot(member, "inventory", generation)
        inventory_changed = inventory != previous_inventory
        if changed or inventory_changed or state.get("attempt") or state.get("pending_retry"):
            pending = dict(before)
            snapshots = {}
            changed_records = set()
            for kind in sorted(changed):
                remote = await self.watched_snapshot(member, generation, kind)
                previous = await self.snapshot(member, f"remote_{kind}", generation)
                if remote is not None:
                    snapshots[kind] = (remote, previous)
                targets = None
                if not initial and remote is not None and previous is not None:
                    delta = {
                        key
                        for key in remote.keys() | previous.keys()
                        if remote.get(key) != previous.get(key)
                    }
                    if 0 < len(delta) <= TARGET_LIMIT:
                        targets = delta
                # A changed activity stamp without a usable delta can be a same-date
                # replacement or a server without the minimal watched snapshot.
                if initial and not remote and not stamp[kind]:
                    continue
                if initial and not remote and stamp[kind] == "1970-01-01T00:00:00Z":
                    continue
                found, targets = await self.fetch_history(
                    member, generation, records, kind, targets
                )
                pending = {
                    key: row
                    for key, row in pending.items()
                    if row[0] != kind or (targets is not None and str(row[5]) not in targets)
                }
                changed_records.update(found)
                pending.update(found)
            if changed and await self.activities(member, generation) != stamp:
                raise RetryScan("Trakt 历史在扫描期间更新，稍后重新核对")
            candidates = []
            reconsider = (
                pending.keys()
                if (inventory_changed or state.get("pending_retry") or state.get("attempt"))
                else changed_records
            )
            for record_id in tuple(reconsider):
                kind, ids, season, episode, when, _, action = pending[record_id]
                if await records.done(record_id):
                    del pending[record_id]
                    continue
                unit = self.match(index, kind, ids, season, episode)
                if unit:
                    candidates.append(
                        (unit, digest([namespace, int(record_id)]), when, int(record_id), action)
                    )
            candidates.sort(key=lambda fact: timestamp(fact[2]), reverse=True)
            if any(timestamp(fact[2]) > self.e.clock() for fact in candidates):
                raise RetryScan("Trakt 观看时间尚未到达，稍后核对")
            async with self.e.locks[member]:
                await self.account(member, generation)
                await self.verify_member(member)
                # If interrupted while persisting these indexes, reconsider all staged
                # records on restart, even if the inventory fingerprint is unchanged.
                state["pending_retry"] = True
                await self.e.set(member, "background_import", state)
                await records.save_pending(before, pending)
                for kind, (remote, previous) in snapshots.items():
                    await self.save_snapshot(member, f"remote_{kind}", generation, remote, previous)
                if inventory_changed:
                    await self.save_snapshot(
                        member, "inventory", generation, inventory, previous_inventory
                    )
                # Incoming records and the checkpoint become durable before any remote write.
                state.update(stamp=stamp, record_ledger=True)
                await self.e.set(member, "background_import", state)
            applied, held, conflict, finished = await self.apply(
                member, generation, records, candidates
            )
            remaining = {key: row for key, row in pending.items() if int(key) not in finished}
            async with self.e.locks[member]:
                await self.account(member, generation)
                await records.save_pending(pending, remaining)
            state["imported"] = state.get("imported", 0) + applied
            state["protected"] = state.get("protected", 0) + held
            if applied:
                state["last_imported"] = iso_time(self.e.clock())
            if conflict:
                raise RetryScan("媒体库或观看状态在同步期间更新，稍后重新核对")
            state["pending_retry"] = False
        self.catalog_revision[member] = revision
        state["catalog_due"] = self.e.clock() + CATALOG_CHECK
        return state

    async def activities(self, member, generation):
        payload = await self.fetch(member, generation, "/sync/last_activities")
        if not isinstance(payload, dict):
            raise TraktError(502)
        values = dict(zip(KINDS, Transfers.watched_stamp(payload), strict=True))
        if not any(values.values()):
            raise TraktError(502)
        return values

    async def process(self, member, requested=False):
        try:
            account = await self.e.get(member, "account", {})
            generation = account.get("generation")
            await self.account(member, generation)
        except Superseded:
            self.due[member] = self.e.clock() + POLL
            self.retry[member] = self.due[member]
            return
        state = await self.e.get(member, "background_import", {})
        if state.get("generation") != generation:
            state = {"generation": generation}
        now = self.e.clock()
        if now < state.get("retry_at", 0):
            self.due[member] = state["retry_at"]
            self.retry[member] = self.due[member]
            return
        try:
            await self.reconcile(
                member,
                generation,
                state,
                requested
                or now >= state.get("catalog_due", 0)
                or self.catalog_revision.get(member, -1) != self.library_revision,
            )
            state.update(last_checked=iso_time(self.e.clock()), error=None, attempt=0, retry_at=0)
        except Superseded:
            self.retry[member] = self.e.clock() + POLL
            return
        except (TraktError, OpsError, OSError, RetryScan, ValueError) as exc:
            state["attempt"] = state.get("attempt", 0) + 1
            delay = min(3600, 15 * 2 ** min(state["attempt"], 8))
            if isinstance(exc, TraktError) and exc.auth_error:
                async with self.e.locks[member]:
                    try:
                        account = await self.account(member, generation)
                    except Superseded:
                        return
                    await self.e.block_auth(member, account, await self.e.ledger(member))
                    state.update(error="Trakt 授权失效，请重新连接", retry_at=0)
                    await self.e.set(member, "background_import", state)
                    self.retry[member] = self.e.clock() + POLL
                    return
            message = (
                str(exc)
                if isinstance(exc, (RetryScan, TraktError))
                else "MovieClaw 接口暂不可用，请检查版本和导入权限"
                if isinstance(exc, OpsError)
                else "后台导入未完成，稍后自动重试"
            )
            state.update(
                error=message, retry_at=self.e.clock() + max(delay, getattr(exc, "retry_after", 0))
            )
        async with self.e.locks[member]:
            try:
                await self.account(member, generation)
            except Superseded:
                return
            await self.e.set(member, "background_import", state)
        self.due[member] = state["retry_at"] or self.e.clock() + POLL
        self.retry[member] = state["retry_at"]

    async def _run_member(self, member, requested):
        try:
            await self.process(member, requested)
        except Exception:
            # A storage failure for one account must not stop the other accounts.
            self.e.log.warning("Trakt 后台导入暂未完成，稍后自动重试")
            self.due[member] = self.e.clock() + 60
            self.retry[member] = self.due[member]
        finally:
            self.active.pop(member, None)
            self.wake.set()

    async def run(self):
        while True:
            try:
                self.wake.clear()
                bindings = (
                    await self.e.store.scopes("automatic_import_binding", entity="member")
                    if self.e.enabled
                    else {}
                )
                for scope in bindings:
                    if len(self.active) >= 4:
                        continue
                    member = int(scope.split(":")[1])
                    if member in self.active:
                        continue
                    requested = member in self.requested
                    if not requested and self.e.clock() < self.retry.get(member, 0):
                        continue
                    if (
                        not requested
                        and self.e.clock() < self.due.get(member, 0)
                        and self.catalog_revision.get(member, -1) == self.library_revision
                    ):
                        continue
                    self.requested.discard(member)
                    self.active[member] = asyncio.create_task(self._run_member(member, requested))
                await asyncio.wait_for(self.wake.wait(), timeout=15)
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                jobs = list(self.active.values())
                for job in jobs:
                    job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
                raise
            except Exception:
                self.e.log.warning("Trakt 后台导入调度暂不可用")
                await asyncio.sleep(15)
