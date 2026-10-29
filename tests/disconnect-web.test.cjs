const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

for (const [name, answers, pending, expected] of [
  ['cancel ordinary disconnect', [false], false, 0],
  ['ordinary disconnect never opts into identity loss', [true], false, 1],
  ['decline identity reset and retain beta binding', [true, false], true, 1],
  ['explicitly accept identity reset for the original selected member', [true, true], true, 2],
]) test(name, async () => {
  const calls = [], prompts = [], errors = [], button = {};
  const context = vm.createContext({
    selected:7, $:()=>button, action:fn=>fn, readMember:()=>7, selectMember:async()=>{},
    memberLabel:id=>`member ${id}`, cancelAuth() {}, refresh:async()=>{},
    showError:e=>errors.push(e),
    confirm:message=>{prompts.push(message); return answers[prompts.length-1];},
    api:async(path, method, id)=>{
      calls.push({path, method, id});
      context.selected = 9; // A member selection can change during the first request.
      return pending && calls.length===1
        ? {disconnected:false, import_identity_pending:true, error:'identity unavailable'}
        : {disconnected:true, remote_revocation_confirmed:true};
    },
  });
  const code = fs.readFileSync(require.resolve('../web/app.js'), 'utf8');
  const start = code.indexOf('$("disconnect").onclick =');
  const end = code.indexOf('\nconst tabs =', start);
  assert.ok(start>=0 && end>start);
  vm.runInContext(code.slice(start,end), context);
  await button.onclick();
  assert.equal(calls.length, expected);
  assert.ok(calls.every(c=>c.id===7 && c.method==='POST'));
  if (expected===2) {
    assert.equal(calls[1].path, 'disconnect?allow_legacy_identity_loss=true');
    assert.match(prompts[1], /重新绑定后可能重复导入/);
  } else assert.ok(calls.every(c=>!c.path.includes('allow_legacy_identity_loss')));
  if (pending && expected===1) assert.deepEqual(errors, ['identity unavailable']);
});
