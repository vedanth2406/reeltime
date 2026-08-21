/* Execute the viewer's render paths against real API payloads.
 *
 * `src/reeltime/core/ui/index.html` is ~600 lines of JavaScript that the Python
 * suite cannot reach: the API tests prove the *payloads* are right and say
 * nothing about whether the code that draws them runs. A typo in a render
 * function is invisible until somebody opens the page.
 *
 * So this drives the real functions over payloads captured from a real trace,
 * with a DOM stub that is deliberately dumb -- it is not trying to be a
 * browser, only to let the functions run and let an exception escape. That
 * catches the class of bug worth catching here (undefined property, wrong
 * arity, a renamed field) without a headless-browser dependency, which the
 * design rejected.
 *
 * Usage:  node tools/ui_render_check.js <payloads.json> <ui.js>
 */
const fs = require('fs');
const vm = require('vm');

const P = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

function mkEl(tag) {
  return {
    tagName: (tag || '').toUpperCase(), className: '', style: {}, children: [],
    _text: '', attributes: {}, clientWidth: 1200, open: false, tabIndex: 0,
    set textContent(v) { this._text = String(v); this.children = []; },
    get textContent() { return this._text; },
    set innerHTML(v) { this._html = v; }, get innerHTML() { return this._text; },
    appendChild(c) { this.children.push(c); return c; },
    setAttribute(k, v) { this.attributes[k] = v; },
    getAttribute(k) { return this.attributes[k]; },
    addEventListener() {}, focus() {}, remove() {},
    classList: {
      _s: new Set(), add(x) { this._s.add(x); }, remove(x) { this._s.delete(x); },
      toggle(x, on) { on ? this._s.add(x) : this._s.delete(x); },
      contains(x) { return this._s.has(x); },
    },
  };
}

const nodes = {};
global.document = {
  createElement: mkEl,
  createTextNode: (t) => ({ nodeValue: String(t) }),
  getElementById: (id) => nodes[id] || (nodes[id] = mkEl('div')),
  addEventListener() {},
  documentElement: { setAttribute() {}, getAttribute() { return 'dark'; } },
  title: '',
};
global.window = { addEventListener() {} };
global.location = { pathname: '/run/' + P.run.summary.run_id };
global.history = { pushState() {}, replaceState() {} };
global.navigator = {};
global.localStorage = { getItem: () => null, setItem() {} };
global.fetch = function (path) {
  let body;
  if (path.indexOf('baseline=') >= 0) body = P.diff;
  else if (path.indexOf('/context/') >= 0) body = P.ctx;
  else if (path.indexOf('/api/run/') >= 0) body = P.run;
  else if (path === '/api/runs') body = P.runs;
  else if (path === '/api/tree') body = P.tree;
  else if (path === '/api/boot') body = { run_id: P.run.summary.run_id, explicit: true, tape_dir: '/t' };
  else body = { groups: [] };
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
};

let src = fs.readFileSync(process.argv[3], 'utf8');
// The boot block is driven deliberately below; the strict pragma goes so that
// `var` declarations land where this harness can call them.
src = src.replace(/\/\* Boot:[\s\S]*$/, '').replace('"use strict";', '');
vm.runInThisContext(src);

let failures = 0;
function check(name, fn) {
  try { fn(); console.log('  ok    ' + name); }
  catch (e) { failures++; console.log('  FAIL  ' + name + '  ->  ' + e.message); }
}

S.run = P.run; S.events = P.run.events; S.sel = P.run.events[0].i;
S.runs = P.runs.runs; S.tapeDir = '/t';

check('drawTrack, proportional', () => drawTrack());
check('drawStatus', () => drawStatus());
check('drawTabs', () => drawTabs());
check('renderRaw', () => renderRaw(mkEl('div')));
check('bucketLimit derives from the live width', () => {
  const n = bucketLimit();
  if (n !== Math.floor(1200 / 5)) throw new Error('expected 240, got ' + n);
});
check('drawTrack buckets past the limit', () => {
  const real = S.events;
  S.events = Array.from({ length: 900 }, (_, i) =>
    ({ i: i, kind: 'llm', dur_ms: 10, t_rel: i * 0.01, site: 'a.py:1' }));
  drawTrack();
  const drawn = document.getElementById('track').children.length;
  if (drawn > bucketLimit()) throw new Error('did not bucket: ' + drawn + ' blocks');
  S.events = real;
});
check('inlineDiff', () => {
  if (!inlineDiff('the quick brown fox', 'the slow brown fox').children.length) {
    throw new Error('no output');
  }
});
check('preview truncates', () => {
  if (preview('x'.repeat(500)).length > 121) throw new Error('too long');
});
check('currentCommand names the diff baseline', () => {
  S.view = 'diff'; S.baseline = 0;
  if (currentCommand().indexOf('--context --diff 0') < 0) {
    throw new Error(currentCommand());
  }
  S.view = 'raw';
});
check('messageCard over every recorded message', () => {
  P.ctx.context.messages.forEach(function (m) { messageCard(m); });
});
check('changeRow over every change kind', () => {
  P.diff.changes.forEach(function (c) { changeRow(c); });
});
check('the TRUNCATED treatment is reached', () => {
  const cut = P.diff.changes.filter(function (c) { return c.truncated; });
  if (!cut.length) throw new Error('the fixture produced no truncated change');
  const row = changeRow(cut[0]);
  if (row.className.indexOf('trunc') < 0) {
    throw new Error('a truncated change did not get the trunc treatment');
  }
  /* The class alone is not enough. A kept-prefix truncation must render the
   * kept and lost blocks *instead of* an inline diff -- and reading a renamed
   * or missing field just takes the else branch, silently, which is exactly
   * the regression an exception-only check sails past. So assert the output. */
  const classes = [];
  (function walk(n) {
    (n.children || []).forEach(function (c) { classes.push(c.className); walk(c); });
  })(row);
  if (classes.indexOf('kept') < 0 || classes.indexOf('lost') < 0) {
    throw new Error('kept/lost blocks missing; got [' + classes.join(', ') + ']');
  }
});
check('contextIndices', () => {
  if (!contextIndices().length) throw new Error('no context events found');
});
check('moveBaseline', () => { S.view = 'diff'; moveBaseline(-1); });
check('select, step, stepKind', () => { select(1); step(-1); step(1); stepKind(1); });

console.log(failures ? '\n' + failures + ' FAILURES' : '\nall render paths executed cleanly');
process.exit(failures ? 1 : 0);
