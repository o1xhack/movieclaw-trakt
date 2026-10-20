"use strict";
const $ = (id) => document.getElementById(id);
let generation = 0;
function member() {
  const value = Number($("member").value);
  if (!Number.isInteger(value) || value < 0) throw new Error("请输入有效的成员 ID");
  return value;
}
async function api(path, method = "GET", id = member()) {
  const response = await fetch(`/api/v1/plugins/trakt-sync/${path}?member_id=${id}`, {
    method, credentials: "same-origin", headers: { "Accept": "application/json" }
  });
  const result = await response.json();
  if (!response.ok || result.success === false) {
    throw new Error(response.status === 403 ? "此页面需要管理员权限" : result.message || result.detail || "请求未完成");
  }
  return result.data ?? result;
}
async function refresh() {
  const id = member();
  const s = await api("status", "GET", id);
  if (id !== member()) return;
  $("status").textContent = !s.configured ? "请先在插件设置填写 Trakt 应用凭据" :
    !s.connected ? "尚未连接" : `已连接 ${s.username || "Trakt"}；待同步 ${s.pending} 条${s.last_error ? `；${s.last_error}` : ""}`;
  $("connect").disabled = !s.configured || s.connected;
  $("disconnect").disabled = !s.connected;
  $("retry").hidden = !s.failed;
}
function action(fn) {
  return async () => { $("error").textContent = ""; try { await fn(); } catch (e) { $("error").textContent = e.message; } };
}
$("refresh").onclick = action(refresh);
$("retry").onclick = action(async () => { await api("retry", "POST"); await refresh(); });
$("member").onchange = action(async () => { generation++; $("authorization").hidden = true; await refresh(); });
$("connect").onclick = action(async () => {
  const id = member(), own = ++generation;
  const auth = await api("auth/begin", "POST", id);
  if (own !== generation) return;
  const url = new URL(auth.verification_url);
  if (url.protocol !== "https:" || !["trakt.tv", "app.trakt.tv", "auth.trakt.tv"].includes(url.hostname)) throw new Error("授权地址无效");
  $("verify").href = url.href; $("code").textContent = auth.user_code;
  $("authorization").hidden = false; $("auth-state").textContent = "等待你在 Trakt 确认授权…";
  let interval = auth.interval;
  while (own === generation && Date.now() / 1000 < auth.expires_at) {
    await new Promise(resolve => setTimeout(resolve, interval * 1000));
    if (own !== generation) return;
    const state = await api("auth/poll", "POST", id);
    if (state.state === "connected") { $("authorization").hidden = true; await refresh(); return; }
    if (["expired", "denied", "idle"].includes(state.state)) { $("auth-state").textContent = "授权已结束，请重新连接"; return; }
    interval = Math.max(5, state.interval || interval);
  }
  if (own === generation) $("auth-state").textContent = "授权已过期，请重新连接";
});
$("disconnect").onclick = action(async () => {
  if (!confirm("断开当前成员的 Trakt，并清除此成员待同步的播放事件？Trakt 已有记录不会删除。")) return;
  generation++;
  const result = await api("disconnect", "POST"); $("authorization").hidden = true; await refresh();
  if (!result.remote_revocation_confirmed) $("error").textContent = "本地已断开；请在 Trakt 已连接应用里确认撤销授权。";
});
action(refresh)();
