"""NAS-side, opt-in reconciliation of Trakt viewing facts."""

import asyncio
from collections import defaultdict
from datetime import datetime

from movieclaw_sdk.ops import OpsError

from .activity import watch_activity
from .client import TraktError
from .policy import identity, iso_time, timestamp, trakt_minute
from .transfers import Transfers, digest, unit_key

KINDS = ("movies", "episodes")
POLL = 300
CATALOG_CHECK = 1800
FULL_CHECK = 7 * 86400
TARGET_LIMIT = 12
SNAPSHOT_PAGE = 400


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
            for name in [f"{side}_{kind}" for side in ("local", "remote") for kind in KINDS]:
                key = f"background_{name}"
                meta = await self.e.get(member, key, {})
                for i in range(meta.get("allocated", meta.get("pages", 0))):
                    await self.e.store.delete(f"{key}_{i}", scope=self.e.scope(member))
                await self.e.store.delete(key, scope=self.e.scope(member))
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
            if not await self.e.get(member, "linked", False):
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
        if (
            unit.get("watches")
            and unit["updated_at"]
            and not unit.get("last_played_at")
            and not unit.get("position_ms")
            and timestamp(unit["updated_at"]) >= timestamp(when)
        ):
            # The catalog retains historical facts after a local reset, including older resets
            # from before this plugin recorded the event.
            return True
        for key in self.e.unwatch_keys(item, prefix="import_unwatch"):
            mark = await self.e.get(member, key, {})
            if mark.get("owner") == owner and timestamp(when) <= timestamp(mark["at"]):
                return True
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

    async def history(self, member, generation, path):
        page, seen_pages = 1, set()
        while True:
            rows = await self.fetch(member, generation, path, {"page": page, "limit": 100})
            if not isinstance(rows, list):
                raise TraktError(502)
            marker = digest(rows)
            if rows and marker in seen_pages:
                raise RetryScan("Trakt 返回了重复历史分页")
            seen_pages.add(marker)
            for row in rows:
                yield row
            if not rows or (getattr(rows, "page_count", 0) and page >= rows.page_count):
                return
            page += 1

    async def target_paths(self, member, generation, kind, added, changed):
        paths = {f"/sync/history/{kind}/{value}": None for value in changed}
        titles = defaultdict(set)
        for unit in added:
            item = identity({"media": unit["media"], "unit": unit})
            titles[tuple(sorted(external_ids(item).items()))].add(unit_key(unit))
        if len(paths) + len(titles) > TARGET_LIMIT:
            return {f"/sync/history/{kind}": None}
        item_type = "movie" if kind == "movies" else "show"
        for external, allowed in sorted(titles.items(), key=lambda pair: repr(pair[0])):
            ids = dict(external)
            for key in ("tmdb", "imdb"):
                if key not in ids:
                    continue
                try:
                    results = await self.fetch(
                        member, generation, f"/search/{key}/{ids[key]}", {"type": item_type}
                    )
                except TraktError as exc:
                    if exc.status not in {400, 404, 422}:
                        raise
                    continue
                if not isinstance(results, list):
                    raise TraktError(502)
                ids_found = set()
                for result in results:
                    remote = result.get(item_type, {}).get("ids", {})
                    if remote.get(key) == ids[key] and all(
                        remote[k] == ids[k] for k in remote.keys() & ids.keys()
                    ):
                        trakt_id = remote.get("trakt")
                        if type(trakt_id) is int and trakt_id > 0:
                            ids_found.add(trakt_id)
                if len(ids_found) == 1:
                    path = f"/sync/history/{'movies' if kind == 'movies' else 'shows'}/{min(ids_found)}"
                    if path not in paths:
                        paths[path] = allowed
                    elif paths[path] is not None:
                        paths[path].update(allowed)
                    break
            else:
                # Identity resolution can lag behind a valid history entry.
                return {f"/sync/history/{kind}": None}
        return paths

    async def watched_snapshot(self, member, generation, kind):
        try:
            rows = await self.fetch(
                member, generation, f"/sync/watched/{kind}", {"extended": "min"}
            )
        except TraktError as exc:
            if exc.status in {400, 404, 422}:
                return None
            raise
        # Older/changed API shapes still reconcile through the canonical history.
        if not isinstance(rows, dict) or any(
            not key.isascii()
            or not key.isdigit()
            or not isinstance(dates, list)
            or any(not isinstance(date, str) for date in dates)
            for key, dates in rows.items()
        ):
            return None
        return {key: digest(sorted(dates))[:32] for key, dates in rows.items() if dates}

    async def histories(self, member, generation, kind, paths):
        canonical = f"/sync/history/{kind}"
        for path, allowed in sorted(paths.items()):
            try:
                async for row in self.history(member, generation, path):
                    yield row, allowed
            except TraktError as exc:
                if path == canonical or exc.status not in {400, 404, 422}:
                    raise
                async for row in self.history(member, generation, canonical):
                    yield row, None
                return

    @staticmethod
    def match(index, kind, row):
        ids, season, episode = remote_unit(kind, row)
        found = {}
        for key in match_keys(kind, ids, season, episode):
            for unit in index[key]:
                local = external_ids(identity({"media": unit["media"], "unit": unit}))
                if all(local[k] == ids[k] for k in local.keys() & ids.keys()):
                    found[unit_key(unit)] = unit
        return next(iter(found.values())) if len(found) == 1 else None

    async def apply(self, member, generation, candidates):
        applied = held = 0
        versions = {}
        conflict = False
        for start in range(0, len(candidates), 100):
            async with self.e.locks[member]:
                await self.account(member, generation)
                owner = await self.verify_member(member)
                batch, activity = [], []
                for unit, source_id, when in candidates[start : start + 100]:
                    key = unit_key(unit)
                    if key in versions and versions[key] is False:
                        continue
                    item = identity({"media": unit["media"], "unit": unit})
                    if await self.protected(member, owner, unit, item, when):
                        held += 1
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
                    continue
                result = await self.ops.call(
                    "playback.sync.import",
                    {"member_id": member, "records": [row for _, _, row in batch]},
                )
                for (key, media, row), outcome in zip(batch, result["outcomes"], strict=True):
                    status = outcome["status"]
                    if status == "applied":
                        applied += 1
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
                        continue
                    elif status in {"conflict", "not_found"}:
                        versions[key] = False
                        conflict = True
                    else:
                        # A source may already be anchored by the host to a different media row.
                        # Keep it and report the mismatch, avoiding an endless full-scan retry.
                        held += 1
                        versions[key] = False
                await self.e.record_activities(member, activity, inbound=True)
        return applied, held, conflict

    async def reconcile(self, member, generation, state, inventory_due):
        await self.verify_member(member)
        stamp = await self.activities(member, generation)
        full = self.e.clock() >= state.get("full_due", 0)
        previous_stamp = state.get("stamp", {})
        changed = {kind for kind in KINDS if stamp[kind] != previous_stamp.get(kind)}
        if not (inventory_due or full or changed):
            return state
        async with self.e.locks[member]:
            await self.account(member, generation)
            namespace = await self.e.import_identity(member)
        revision = self.library_revision
        units, index = await self.catalog(member)
        snapshots = {}
        candidates = {}
        queried = False
        for kind in KINDS:
            inventory = {
                key: digest(external_ids(identity({"media": u["media"], "unit": u})))[:32]
                for key, u in units[kind].items()
            }
            before = await self.snapshot(member, f"local_{kind}", generation)
            added = [
                u for key, u in units[kind].items() if (before or {}).get(key) != inventory[key]
            ]
            if inventory != before:
                snapshots[f"local_{kind}"] = (inventory, before)
            remote = None
            old_remote = None
            if full or kind in changed:
                remote = await self.watched_snapshot(member, generation, kind)
                old_remote = await self.snapshot(member, f"remote_{kind}", generation)
                if remote is not None and remote != old_remote:
                    snapshots[f"remote_{kind}"] = (remote, old_remote)
            if not inventory:
                continue
            if (
                full
                or before is None
                or (kind in changed and (remote is None or old_remote is None))
            ):
                paths = {f"/sync/history/{kind}": None}
            else:
                delta = [
                    key for key, h in (remote or {}).items() if h != (old_remote or {}).get(key)
                ]
                # A change stamp with no visible delta can include same-date replacements.
                paths = (
                    {f"/sync/history/{kind}": None}
                    if kind in changed and remote == old_remote
                    else await self.target_paths(member, generation, kind, added, delta)
                )
            native = {}
            queried |= bool(paths)
            async for row, allowed in self.histories(member, generation, kind, paths):
                unit = self.match(index, kind, row)
                if unit is None or (allowed is not None and unit_key(unit) not in allowed):
                    continue
                when, remote_id = row.get("watched_at"), row.get("id")
                try:
                    valid_time = datetime.fromisoformat(when.replace("Z", "+00:00"))
                    if valid_time.tzinfo is None:
                        raise ValueError
                except (ValueError, AttributeError):
                    raise TraktError(502) from None
                if type(remote_id) is not int or remote_id <= 0:
                    raise TraktError(502)
                if valid_time.timestamp() > self.e.clock():
                    raise RetryScan("Trakt 观看时间尚未到达，稍后核对")
                key = unit_key(unit)
                if key not in native:
                    native[key] = {
                        trakt_minute(w["watched_at"])
                        for w in unit["watches"]
                        if w["basis"] in {"playback", "state"}
                    }
                if trakt_minute(when) in native[key]:
                    continue
                source = digest([namespace, remote_id])
                fact = (unit, source, when)
                if source in candidates and candidates[source] != fact:
                    raise RetryScan("Trakt 返回了相互冲突的观看记录")
                candidates[source] = fact
        if (queried or changed) and await self.activities(member, generation) != stamp:
            raise RetryScan("Trakt 历史在扫描期间更新，稍后重新核对")
        # Newer records are evaluated first so a local unwatch cutoff is handled consistently.
        ordered = sorted(candidates.values(), key=lambda fact: timestamp(fact[2]), reverse=True)
        applied, held, conflict = await self.apply(member, generation, ordered)
        state["imported"] = state.get("imported", 0) + applied
        if applied:
            state["last_imported"] = iso_time(self.e.clock())
        state["protected"] = held
        if conflict:
            raise RetryScan("媒体库或观看状态在同步期间更新，稍后重新核对")
        # Only publish indexes after the host has accepted the writes. Replaying after a restart
        # uses the identical source IDs, even if indexes or the final stamp weren't saved.
        for name, (snapshot, previous) in snapshots.items():
            async with self.e.locks[member]:
                await self.account(member, generation)
                await self.save_snapshot(member, name, generation, snapshot, previous)
        self.catalog_revision[member] = revision
        state.update(stamp=stamp, catalog_due=self.e.clock() + CATALOG_CHECK)
        if full:
            state["full_due"] = self.e.clock() + FULL_CHECK
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
