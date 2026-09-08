const assert = require('node:assert/strict');
const {ConnectionFlow} = require('../spend_web/connections.js');
let id = 0;
const flow = new ConnectionFlow(() => String(++id));
const owner = flow.move('verify');
assert.equal(flow.owns(owner), true);
flow.move('back');
assert.equal(flow.owns(owner), false);
const body = {source:'codex_local', revision:0, location:'C:/B'};
const first = flow.request(body);
assert.deepEqual(flow.request(body), first, 'uncertain retry must reuse id');
flow.move('closed');
assert.deepEqual(flow.request(body), first, 'closing does not roll back a write');
assert.notEqual(flow.request({...body,location:'C:/C'}).requestId, first.requestId);
flow.saved(body);
assert.notEqual(flow.request(body).requestId, first.requestId);
const selected = [
  {row:{source:'codex_local'},check:{checked:true}},
  {row:{source:'codex_local'},check:{checked:true}},
  {row:{source:'claude_local'},check:{checked:true}},
];
flow.selectCandidate(selected, selected[1]);
assert.equal(selected[0].check.checked, false);
assert.equal(selected[1].check.checked, true);
assert.equal(selected[2].check.checked, true);
console.log('9 connection request-state assertions passed');
