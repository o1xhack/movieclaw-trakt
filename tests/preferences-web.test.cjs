const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

test("switching members blocks stale preferences and unrelated updates omit import consent", async () => {
  const prefs = ["favorites", "automatic_import"].map(name => ({
    dataset: {preference:name}, checked:true, disabled:false
  }));
  const calls = [], buttons = new Map();
  let resolveRefresh;
  const context = {
    selected:1, profileMember:1, savingProfile:false, cancelAuth() {},
    document:{querySelectorAll:()=>prefs},
    $:id=>{ if (!buttons.has(id)) buttons.set(id, {}); return buttons.get(id); },
    refresh:()=>new Promise(resolve=>{resolveRefresh = resolve;}),
    action:fn=>fn,
    confirm:()=>assert.fail("unrelated changes do not ask for import consent"),
    api:async (...args)=>calls.push(JSON.parse(JSON.stringify(args))),
  };
  vm.createContext(context);
  const source = fs.readFileSync(require.resolve("../web/app.js"), "utf8");
  for (const [start, end] of [
    ["const preferenceInputs", "async function api"],
    ["async function selectMember", "function renderAccounts"],
    ["for (const input of preferenceInputs()) input.onchange", "action(async () => {\n  invalidateProfile();"],
  ]) vm.runInContext(source.slice(source.indexOf(start), source.indexOf(end)), context);

  const change = context.selectMember(2);
  assert.ok(prefs.every(input=>input.disabled));
  await prefs[0].onchange();
  assert.deepEqual(calls, []);
  resolveRefresh();
  await change;

  context.profileMember = 2;
  prefs[0].checked = false;
  const save = prefs[0].onchange();
  await new Promise(setImmediate);
  assert.deepEqual(calls, [["profile","PUT",2,{favorites:false}]]);
  resolveRefresh();
  await save;
});
