const test = require('node:test');
const assert = require('node:assert/strict');
const A = require('../web/activity-logic.js');

test('type and send-state filters compose; playback noise is hidden by default', () => {
  const rows = [{event:'started',state:'sent'}, {event:'marked_played',state:'sent'},
    {event:'marked_unplayed',state:'failed'}, {event:'favorited',state:'failed'},
    {event:'completed',state:'sent'}, {event:'progress',state:'queued'},
    {event:'stopped',category:'watched',state:'sent'}];
  assert.equal(rows.filter(r=>A.matches(r)).length,5);
  assert.equal(rows.filter(r=>A.matches(r,'failed','watched')).length,1);
  assert.equal(rows.filter(r=>A.matches(r,'failed','favorite')).length,1);
  assert.equal(rows.filter(r=>A.matches(r,'sent','watched')).length,3);
  assert.equal(rows.filter(r=>A.matches(r,'pending','playback')).length,1);
  assert.equal(rows.filter(r=>A.matches(r,'all','all')).length,7);
  assert.equal(rows.filter(r=>A.matches(r,'sent','favorite')).length,0);
});
