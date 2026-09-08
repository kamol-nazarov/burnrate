// Pure formatter regression: evaluate only production numeric/percentage functions.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const text = fs.readFileSync('frontend_src/spend.js', 'utf8');
const finite = text.match(/const finite = value => \{[\s\S]*?\n\};/)[0];
const pct = text.match(/function pct\(value\) \{[\s\S]*?\n\}/)[0];
const context = vm.createContext({});
vm.runInContext(finite + '\nconst unknown = "—";\n' + pct, context);
for (const [input, expected] of [[null, '—'], [0, '0.0%'], [0.1, '0.1%'], [37, '37.0%']]) {
  context.input = input;
  assert.equal(vm.runInContext('pct(input)', context), expected);
}
console.log('4 production quota formatter assertions passed');
