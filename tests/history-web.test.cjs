const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('export preview and lazy record details disclose minute precision before confirmation', async () => {
  const nodes = new Map(), requests = [];
  const element = () => ({children: [], hidden: false, textContent: '',
    append(...items) { this.children.push(...items); },
    replaceChildren() { this.children = []; }, removeAttribute() {},
    showModal() { this.open = true; }});
  const get = id => { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); };
  const task = {id:'fixture', direction:'export', state:'ready', total:2, movies:1,
    done:0, skipped:0, failed:0, unmatched:0, group_pages:1, minute_collisions:1};
  const context = vm.createContext({URL, console, setTimeout, clearTimeout,
    location:{href:'http://localhost/apps/trakt-sync/history.html', origin:'http://localhost'},
    document:{getElementById:get, createElement:element, addEventListener() {}, hidden:false},
    window:{addEventListener() {}},
    fetch:async (url, options) => {
      requests.push({url:String(url), method:options.method});
      const data = String(url).includes('/fixture?') ? {task, pages:1, items:[{
        media:{kind:'movie',title:'Fixture'}, watched_at:'2020-01-01T12:34:59.999Z',
        trakt_watched_at:'2020-01-01T12:34:00Z', minute_collision:true, state:'pending',
      }]} : {tasks:[],total:0};
      return {ok:true, json:async () => ({success:true,data})};
    },
  });
  vm.runInContext(fs.readFileSync(require.resolve('../web/history.js'), 'utf8'), context);
  vm.runInContext(`task = ${JSON.stringify(task)}; render();`, context);
  assert.equal(get('confirm').hidden, false);
  assert.equal(get('precision-notice').hidden, false);
  assert.match(get('precision-notice').textContent, /1 条同一分钟.*无法在 Trakt 分别保存/);
  await vm.runInContext("details('fixture', 0, true)", context);
  assert.match(get('detail-summary').textContent, /无法在 Trakt 分别保存/);
  const paragraphs = get('detail-items').children[0].children.map(c => c.textContent).join(' ');
  assert.match(paragraphs, /Trakt 时间：.*同分钟额外观看.*原始时间不变/);
  assert.ok(requests.every(r => r.method === 'GET')); // Preview cannot write.
  const complete = vm.runInContext("description({...task, state:'complete', done:1})", context);
  assert.match(complete, /按 Trakt 分钟精度对齐/);
  assert.doesNotMatch(complete, /全部同步/);
});
