// Behavioural tests over code extracted verbatim from realtime.html (the Realtime page).
//
//     node --test tests/js/realtime.test.mjs      (or: python -m pytest tests/test_viewer_js.py)

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { compile, realtimeHtml } from './extract.mjs';

const SRC = realtimeHtml();
const env = {
  UNIT_SHORT: { 'degrees-fahrenheit': '°F', 'inches-of-water': 'in. w.c.', percent: '%' },
  TSTEPS: [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400],
};
const F = compile(['unitKey', 'fmtVal', 'fmtLeft', 'niceTicks', 'timeTicks', 'nearest',
                   'seriesPath', 'yRange', 'mergeRows'], env, SRC);

test('the page still holds the tables these tests stand in for', () => {
  assert.ok(SRC.includes("'degrees-fahrenheit':'°F'") && SRC.includes("'inches-of-water':'in. w.c.'"));
  assert.ok(SRC.includes('const TSTEPS=[5,10,15,30,60,'));
});

test('a line breaks where readings stop, and a column keeps its spike', () => {
  const X = t => t, Y = v => v;                      // one pixel per second, value as is
  const rows = [[0, 10], [5, 11], [10, 12], [40, 13], [45, null], [50, 14], [50.2, 99], [50.4, 15]];
  const out = F.seriesPath(rows, 0, 60, X, Y, 17, false);
  const runs = [];
  let cur = [];
  for (const p of out) { if (p === null) { runs.push(cur); cur = []; } else cur.push(p); }
  runs.push(cur);
  // 10 -> 40 is 30 s with nothing (gap 17): broken; the null at 45 breaks again
  assert.equal(runs.length, 3);
  assert.deepEqual(runs[0].map(p => p[0]), [0, 5, 10]);
  assert.deepEqual(runs[1].map(p => p[1]), [13]);
  // 50, 50.2 and 50.4 share a pixel: first, low, high, last
  assert.deepEqual(runs[2], [[50, 14], [50, 14], [50, 99], [50, 15]]);
});

test('an on/off point is drawn as steps', () => {
  const out = F.seriesPath([[0, 0], [10, 1], [20, 0]], 0, 30, t => t, v => v, 20, true);
  assert.deepEqual(out, [[0, 0], [10, 0], [10, 1], [20, 1], [20, 0]]);
});

test('the hover finds the nearest reading', () => {
  const rows = [[0, 1], [5, 2], [10, 3]];
  assert.equal(F.nearest(rows, 6), 1);
  assert.equal(F.nearest(rows, 8), 2);
  assert.equal(F.nearest(rows, -50), 0);
  assert.equal(F.nearest(rows, 2.5), 0);               // a tie goes to the earlier one
  assert.equal(F.nearest([], 3), -1);
});

test('time ticks are seconds for a minute and sit on the clock', () => {
  const t0 = 1_790_000_003;
  const { step, ticks } = F.timeTicks(t0, t0 + 60, 12);
  assert.equal(step, 5);
  assert.ok(ticks.every(t => t % 5 === 0) && ticks[0] === t0 + 2);
  assert.equal(F.timeTicks(t0, t0 + 4 * 3600, 8).step, 1800);
});

test('a flat reading gets a narrow scale, not a wide one', () => {
  const [lo, hi] = F.yRange('inches-of-water', [0.45, 0.45, 0.45]);
  assert.ok(hi - lo < 0.05 && lo < 0.45 && hi > 0.45);
  assert.deepEqual(F.yRange('onoff', [0, 1]), [-0.15, 1.15]);
  assert.deepEqual(F.yRange('state', [1, 3]), [0.5, 3.5]);
  assert.deepEqual(F.yRange('', []), [0, 1]);
  const t = F.niceTicks(55.2, 71.9, 5);
  assert.equal(t.step, 5);
  assert.deepEqual(t.ticks, [60, 65, 70]);
});

test('values and times read as the operator would say them', () => {
  assert.equal(F.unitKey({ object_type: 'binary-value' }), 'onoff');
  assert.equal(F.unitKey({ object_type: 'multi-state-input' }), 'state');
  assert.equal(F.unitKey({ object_type: 'analog-input', units: 'percent' }), 'percent');
  assert.equal(F.fmtVal(1, 'onoff'), 'ON');
  assert.equal(F.fmtVal(72.34, 'degrees-fahrenheit'), '72.3 °F');
  assert.equal(F.fmtVal(0.456, 'inches-of-water'), '0.46 in. w.c.');
  assert.equal(F.fmtVal(null, 'percent'), '—');
  assert.equal(F.fmtLeft(42), '42 s');
  assert.equal(F.fmtLeft(3599), '59 min');
  assert.equal(F.fmtLeft(3600 + 300), '1 h 05 min');
  assert.equal(F.fmtLeft(-5), '0 s');
});

test('a live slice adds only what is new', () => {
  const have = [[0, 1], [5, 2]];
  F.mergeRows(have, [[0, 1], [5, 2], [10, 3]]);
  assert.deepEqual(have, [[0, 1], [5, 2], [10, 3]]);
});
