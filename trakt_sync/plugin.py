"""仅使用 MovieClaw 开放插件服务；管理接口沿用宿主管理员认证。"""

import uuid

import httpx
from fastapi import APIRouter, HTTPException, Query
from movieclaw_api.plugins.keys import PLUGIN_DATA, PLUGIN_HEALTH, PLUGIN_ROUTES
from movieclaw_kernel import DURABLE_EVENTS
from movieclaw_sdk import Context, net, plugin
from movieclaw_sdk.events import (
    PLAYBACK_COMPLETED,
    PLAYBACK_PROGRESS,
    PLAYBACK_STARTED,
    PLAYBACK_STOPPED,
)
from pydantic import BaseModel, Field, SecretStr

from .client import TraktClient, TraktError
from .engine import Engine
from .policy import iso_time

PLUGIN_ID = "trakt-sync"


class Settings(BaseModel):
    enabled: bool = Field(True, title="启用自动追踪", description="只同步已明确绑定的成员")
    client_id: str = Field(
        "",
        title="Trakt Client ID",
        max_length=256,
        description="在 Trakt Developer 创建自己的 API 应用；不使用其他 App 的密钥",
    )
    client_secret: SecretStr = Field(
        SecretStr(""),
        title="Trakt Client Secret",
        description="可选；Trakt 已弃用此字段。若填写，仅在服务器使用并由宿主加密保存",
    )


@plugin(
    PLUGIN_ID,
    title="Trakt 自动追踪",
    config=Settings,
    inject=(PLUGIN_DATA, PLUGIN_HEALTH, PLUGIN_ROUTES, DURABLE_EVENTS),
)
async def apply(ctx: Context):
    settings = ctx.config
    store = ctx.use(PLUGIN_DATA).scoped(ctx)
    health = ctx.use(PLUGIN_HEALTH).reporter(ctx)
    http = httpx.AsyncClient(
        transport=net.http_transport(PLUGIN_ID), timeout=10, follow_redirects=False
    )
    ctx.effect(http.aclose)
    client = TraktClient(
        settings.client_id.strip(), settings.client_secret.get_secret_value(), http
    )
    engine = Engine(store, client, health, ctx.logger, enabled=settings.enabled)

    def listener(kind):
        async def receive(event):
            delivery = ctx.delivery
            await engine.accept(
                kind,
                event.model_dump(mode="json"),
                delivery.event_id if delivery else uuid.uuid4().hex,
                delivery.occurred_at if delivery else iso_time(engine.clock()),
            )

        return receive

    for event, kind in (
        (PLAYBACK_STARTED, "started"),
        (PLAYBACK_STOPPED, "stopped"),
        (PLAYBACK_COMPLETED, "completed"),
    ):
        ctx.on(event, listener(kind), id=f"track-{kind}", durable=True)
    ctx.on(PLAYBACK_PROGRESS, listener("progress"))
    ctx.task(engine.run(), name="trakt-delivery")
    router = APIRouter()

    @router.get("/status", operation_id="plugins.trakt-sync.status", summary="Trakt 同步状态")
    async def status(member_id: int = Query(0, ge=0)) -> dict:
        return await engine.status(member_id)

    @router.post(
        "/auth/begin",
        operation_id="plugins.trakt-sync.auth.begin",
        summary="为指定成员开始 Trakt 设备授权",
    )
    async def begin(member_id: int = Query(0, ge=0)) -> dict:
        try:
            return await engine.begin(member_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        except TraktError as exc:
            raise HTTPException(502, str(exc)) from None

    @router.post(
        "/auth/poll", operation_id="plugins.trakt-sync.auth.poll", summary="查询 Trakt 设备授权结果"
    )
    async def poll(member_id: int = Query(0, ge=0)) -> dict:
        try:
            return await engine.poll(member_id)
        except TraktError as exc:
            raise HTTPException(502, str(exc)) from None

    @router.post(
        "/disconnect",
        operation_id="plugins.trakt-sync.disconnect",
        summary="断开 Trakt（只清除此成员的连接和待同步队列）",
        openapi_extra={"x-cli-dangerous": "confirm"},
    )
    async def disconnect(member_id: int = Query(0, ge=0)) -> dict:
        return await engine.disconnect(member_id)

    @router.post("/retry", operation_id="plugins.trakt-sync.retry", summary="重试匹配失败的记录")
    async def retry(member_id: int = Query(0, ge=0)) -> dict:
        try:
            return await engine.retry_failed(member_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    ctx.use(PLUGIN_ROUTES).mount(ctx, router)
    ctx.logger.info("Trakt 插件已加载；只处理已绑定的成员，不导入历史记录")
