// Behavioural tests over code extracted verbatim from trends.html.
//
//     node --test tests/js/            (or: python -m pytest tests/test_viewer_js.py)
//
// Run with TZ=America/New_York: several of these tests are about local time across DST,
// and their expected values are written for that zone. The pytest wrapper sets it.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { compile, consts, grabSource, recorder, html } from './extract.mjs';

// ── F-03 · the overview strip must break on collector outages ────────────────────────────
// The record holds a 70-hour and a 23-hour outage. Before the fix the mini-map drew one
// unbroken path across both, on the single control whose job is answering "where is there
// data". drawSeries and the fleet band had each been given this test already; the strip
// shipped later and inherited neither.

function hourly(spec) {
  // spec: array of [startHour, countOfBuckets] runs, one hour apart, gaps between runs
  const pts = [];
  let t = 1_784_000_000;
  for (const [skipHours, n] of spec) {
    t += skipHours * 3600;
    for (let i = 0; i < n; i++, t += 3600) pts.push({ t, v: 50 + (i % 5), lo: 48, hi: 55 });
  }
  return pts;
}

function drawStrip(pts) {
  const ovx = recorder();
  const env = {
    ovx, OVW: 900, OVH: 54, OVM: { t: 13, b: 3 },
    state: { extent: { min_ts: pts[0].t, max_ts: pts[pts.length - 1].t },
             start: pts[0].t, end: pts[pts.length - 1].t },
    ovData: { id: 1, pts },
    colorOf: () => '#4aa8ff',
    $: () => null,                       // no #ovLabel in the harness
    ...consts(['OV_MIN_BOX']),           // read from the file, not retyped
  };
  const { drawOverview } = compile(
    ['drawOverview', 'ovGapThreshold', 'medianStep', 'ovFmtSpan', 'ovSpan', 'ovExtent', 'ovPx', 'ovBox'],
    env);
  drawOverview();
  // drawOverview fillRects three different things: the dimming veil outside the window
  // box, the two grip marks on its edges, and the gap markers. Only the last are amber,
  // so match on that -- and if the marker ever stops being visually distinct, this fails,
  // which is the right outcome.
  ovx.gaps = ovx.rects.filter(r => r.style.includes('245,182,66'));
  return ovx;
}

test('F-03 · continuous data draws as ONE run', () => {
  const rec = drawStrip(hourly([[0, 200]]));
  // band (1 subpath) + mean line (1 subpath)
  assert.equal(rec.subpaths, 2, `expected 2 subpaths, got ${rec.subpaths}`);
  assert.equal(rec.gaps.length, 0, 'continuous data must draw no gap markers');
});

test('F-03 · a multi-day outage splits the path and is marked', () => {
  // 40 buckets, a 70-hour hole, 60 more — the shape of the real Jul 17-20 outage
  const rec = drawStrip(hourly([[0, 40], [70, 60]]));
  assert.equal(rec.subpaths, 4,
    `two runs must give 2 band + 2 line subpaths, got ${rec.subpaths}`);
  assert.equal(rec.gaps.length, 1, 'the hole must be shaded, not silently empty');
  assert.ok(rec.gaps[0].w > 1, 'the gap marker must have real width');
});

test('F-03 · two outages give three runs', () => {
  const rec = drawStrip(hourly([[0, 30], [70, 40], [23, 50]]));
  assert.equal(rec.subpaths, 6);
  assert.equal(rec.gaps.length, 2);
});

test('F-03 · the gap threshold tracks the data, with a floor', () => {
  // medianStep is the shared gap-spacing helper the three renderers now share (M-11). compile() pulls each named function's source, so a factored-out dependency has to be named here too or it is simply not defined in the sandbox.
  const { ovGapThreshold } = compile(['ovGapThreshold', 'medianStep']);
  const h = n => Array.from({ length: n }, (_, i) => ({ t: i * 3600 }));
  assert.equal(ovGapThreshold(h(50)), 3 * 3600, 'hourly data -> 3 h');
  assert.equal(ovGapThreshold([{ t: 0 }]), Infinity, 'too few points -> never split');
});

// ── F-08 · occupancy shading must step LOCAL days, not 86400 seconds ─────────────────────

function daysShaded(startISO, endISO) {
  const start = Date.parse(startISO) / 1000, end = Date.parse(endISO) / 1000;
  const M = { l: 56, r: 14, t: 14, b: 26 }, W = 900, H = 600;
  const ctx = recorder();
  const { drawOccupancy } = compile(['drawOccupancy'], {
    ctx, OCC_HRS: { start: 5, end: 18 }, M, W, H,
    state: { start, end },
    // the real linear time->pixel map, so bands land inside the plot and get drawn
    px: t => M.l + (t - start) / (end - start) * (W - M.l - M.r),
  });
  drawOccupancy();
  // x is unique per local day, so duplicate x means a day was shaded twice
  return { bands: ctx.rects.length, starts: ctx.rects.map(r => Math.round(r.x * 100)) };
}

// ⚠️ CALIBRATION, recorded because it matters more than the fix: F-08 is a LATENT
// correctness bug, not an observed visual defect, and the audit slightly overstated it.
// The old loop did drift — with a fixed 86400 s stride it lands twice inside the DST
// transition day and its final iteration falls a day short — but neither is visible:
//   · the duplicated day is ALWAYS the transition day, which in the US is always a Sunday,
//     and weekends are skipped before anything is drawn;
//   · the day it falls short by is always the trailing over-scan day, which the loop only
//     visits to cover a partial day at the window edge and which is clipped anyway.
// Measured across four window widths spanning both 2026 transitions: old and new draw
// IDENTICAL band counts. The fix is still right — stepping local days is what the code
// means, and the over-scan should not be silently load-bearing — but these tests pin the
// invariant rather than reproduce a defect, and they pass against the old code too.

test('F-08 · no local day is shaded twice, either side of fall-back', () => {
  const { starts } = daysShaded('2026-10-28T04:00:00Z', '2026-11-12T20:00:00Z');
  assert.equal(new Set(starts).size, starts.length,
    'a local day was shaded twice — the day stride has drifted');
});

test('F-08 · every interior weekday gets exactly one band, across both transitions', () => {
  for (const [label, a, b, expect] of [
    ['fall-back   ', '2026-10-28T04:00:00Z', '2026-11-12T20:00:00Z', 12],
    ['spring-fwd  ', '2026-03-01T05:00:00Z', '2026-03-15T20:00:00Z', 10],
    ['no transition', '2026-08-03T04:00:00Z', '2026-08-17T20:00:00Z', 11],
  ]) {
    const { bands, starts } = daysShaded(a, b);
    assert.equal(new Set(starts).size, starts.length, `${label}: duplicate band`);
    assert.equal(bands, expect, `${label}: expected ${expect} weekday bands, got ${bands}`);
  }
});

// ── F-07 · the CSV must carry the quality flag and a local timestamp ─────────────────────

test('F-07 · export carries value AND quality, plus local time', () => {
  let captured = null;
  const env = {
    state: {
      sel: [1619], deltas: [],
      series: {
        1619: {
          meta: { system_code: 'AC-5', name: 'AC5_DAT|dac-3', display_name: null },
          samples: [
            { t: 1785513652, v: 55.4, q: 'good' },
            { t: 1785513772, v: 185.0, q: 'fault' },   // the AC-1-style failed sensor
          ],
        },
      },
    },
    deltaId: (a, b) => `d:${a}-${b}`,
    isDelta: id => typeof id === 'string' && id.startsWith('d:'),
    stamp: () => 'STAMP',
    Blob: class { constructor(parts) { this.text = parts.join(''); } },
    dl: (blob, name) => { captured = { text: blob.text, name }; },
  };
  const { exportCSV } = compile(['exportCSV'], env);
  exportCSV();
  assert.ok(captured, 'exportCSV produced nothing');
  const [hdr, r1, r2] = captured.text.split('\n');

  assert.ok(hdr.includes('timestamp_local'), 'header must offer a local timestamp');
  assert.ok(/quality/i.test(hdr), 'header must carry a quality column');

  // the faulted 185 degF reading must not leave the building looking ordinary
  assert.ok(r2.includes('185'), 'value missing');
  assert.ok(r2.includes('fault'),
    'the fault flag is not in the CSV — a faulted reading exports as an ordinary number');
  assert.ok(r1.includes('good'));
});

// ── M-4 · a rollup export must say it is a rollup ────────────────────────────────────
// Past ~2 days the viewer serves 5-minute or hourly buckets. The export wrote each
// bucket's AVERAGE under a column headed with the point name, left quality blank, and
// dropped min/max/n entirely -- 1,738 rows of means that read as readings. F-07 fixed the
// raw window and stopped there.

function exportEnv(samples, rollup) {
  let captured = null;
  const env = {
    state: {
      sel: [1619], deltas: [], rollup,
      series: { 1619: { meta: { system_code: 'AC-5', name: 'AC5_DAT|dac-3',
                                display_name: null }, samples } },
    },
    deltaId: (a, b) => `d:${a}-${b}`,
    isDelta: (id) => typeof id === 'string' && id.startsWith('d:'),
    stamp: () => 'STAMP',
    Blob: class { constructor(parts) { this.text = parts.join(''); } },
    dl: (blob, name) => { captured = { text: blob.text, name }; },
  };
  const { exportCSV } = compile(['exportCSV'], env);
  exportCSV();
  return captured;
}

test('M-4 - a 5m export names the resolution and carries min/max/n', () => {
  const got = exportEnv([
    { t: 1785513600, v: 55.4, lo: 54.9, hi: 56.1, n: 30, nt: 30 },
    { t: 1785513900, v: 55.9, lo: 55.0, hi: 57.0, n: 28, nt: 30 },
  ], '5m');
  assert.ok(got, 'exportCSV produced nothing');
  const [hdr, r1, r2] = got.text.split('\n');

  assert.ok(hdr.includes('resolution'), 'no resolution column - the CSV cannot say what it is');
  assert.ok(r1.includes('5-min avg'), 'rows do not state the resolution');
  assert.ok(/avg/i.test(hdr) && /min/i.test(hdr) && /max/i.test(hdr),
    'a bucket exported without its min/max still reads as a reading');
  assert.ok(/n_total/.test(hdr), 'n_total missing - a faulted bucket is indistinguishable again');
  assert.ok(r1.includes('54.9') && r1.includes('56.1'), 'the envelope was dropped');
  assert.ok(r2.includes('28') && r2.includes('30'), 'the sample counts were dropped');
});

test('M-4 - a raw export is unchanged (F-07 still holds)', () => {
  const got = exportEnv([
    { t: 1785513652, v: 55.4, q: 'good' },
    { t: 1785513772, v: 185.0, q: 'fault' },
  ], 'raw');
  const [hdr, , r2] = got.text.split('\n');
  assert.ok(/quality/i.test(hdr), 'raw export lost its quality column');
  assert.ok(r2.includes('fault'), 'raw export lost the fault flag');
  assert.ok(!/n_total/.test(hdr), 'raw rows must not pretend to be buckets');
});

// -- A3 - one "usable reading" rule, used by every consumer ---------------------------------
// After the 2026-09-22 fixes the rule "a faulted reading is not a reading" lived in 3 of the
// 9 places that consume readings. The scatter + Pearson r, the SP/first transforms, the fleet
// band and median, the smoothing window and the compare-period ghost still used frozen values
// -- the ghost divergence introduced by the very fix that taught drawSeries the rule. The
// sixth time in this file one rule was fixed in one place and not its siblings.

test('A3 - every consumer of readings goes through usable()', () => {
  // The structural guard. A new consumer, or a refactor that re-inlines the test, fails here
  // instead of shipping a seventh divergence.
  const src = html();
  for (const fn of ['statsFor', 'computeDelta', 'smoothSamples', 'fleetMedianSeries',
                    'drawSeries', 'drawGhost', 'fleetBuild', 'matchByTime', 'scatterPairs']) {
    assert.ok(/\busable\(/.test(grabSource(fn, src)),
      `${fn} decides for itself what a usable reading is - call usable() instead`);
  }
});

test('A3 - a faulted reading never enters a scatter pair, a smoothed mean or a statistic', () => {
  const G = (t, v) => ({ t, v, q: 'good' });
  const F = (t, v) => ({ t, v, q: 'fault' });
  const { matchByTime, smoothSamples, statsFor } = compile(
    ['matchByTime', 'smoothSamples', 'statsFor', 'usable', 'minMax'],
    { state: { smoothOn: true } });

  // X-Y scatter / Pearson r: no pair may carry a faulted value on either side
  const pairs = matchByTime([G(0, 1), F(120, 99), G(240, 3)],
                            [G(0, 10), G(120, 20), F(240, 30)], 180);
  assert.ok(pairs.length > 0, 'the good readings should still pair');
  assert.ok(!pairs.some(p => p.x === 99 || p.y === 30), 'a faulted value was paired');

  // smoothing: the fault passes through UNSMOOTHED (so the line still breaks at it) and never
  // bends a good neighbour toward a number nobody measured
  const sm = smoothSamples([G(0, 10), F(120, 1000), G(240, 20)], 3);
  assert.equal(sm[1].v, 1000, 'the faulted sample must pass through untouched');
  assert.ok(sm[0].v < 100 && sm[2].v < 100, 'a faulted neighbour leaked into a smoothed mean');

  // statistics
  const st = statsFor([G(0, 10), F(120, 1000), G(240, 20)]);
  assert.equal(st.n, 2);
  assert.equal(st.max, 20, 'the frozen value set the maximum');
});

test('A3 - rollup buckets (no q) and delta rows (q good) are still usable', () => {
  const { usable } = compile(['usable']);
  assert.ok(usable({ t: 0, v: 5 }), 'a rollup bucket has no q and must count');
  assert.ok(usable({ t: 0, v: 5, q: 'good' }));
  assert.ok(!usable({ t: 0, v: null, n: 0, nt: 30 }), 'a faulted-throughout bucket has no value');
  assert.ok(!usable({ t: 0, v: 5, q: 'comm-loss' }));
  assert.ok(!usable(undefined));
});

// ── a guard on the extraction itself ─────────────────────────────────────────────────────

test('the functions under test still exist in the shipped file', () => {
  const src = html();
  for (const fn of ['drawOverview', 'ovGapThreshold', 'drawOccupancy', 'exportCSV',
                    'drawSeries', 'isStep', 'renderPicker', 'scorePoint']) {
    assert.ok(grabSource(fn, src).length > 0, `${fn} vanished from trends.html`);
  }
});

// ── the weather overlay: the FOURTH renderer to need the gap rule ────────────────────────
// The real weather table has 17 gaps, the longest 96 h. drawWeather broke on a NULL value
// but never on a time gap, so over Aug 1-4 the outdoor temperature drew one confident
// straight line across a 24-hour hole, right next to AC-9's trace, which broke correctly.
// Same shape as F-03 (drawOverview), M-3 (the faulted-bucket hole) and the duplicated
// legend stats: the rule existed and the sibling never inherited it.

test('M-11 - the weather overlay breaks on a weather outage', () => {
  const wx = [];
  for (let i = 0; i < 24; i++) wx.push({ ts_utc: i * 3600, oat_f: 50 + i });
  for (let i = 48; i < 72; i++) wx.push({ ts_utc: i * 3600, oat_f: 50 + i });   // 24 h hole
  const ctx = recorder();
  const { drawWeather } = compile(['drawWeather', 'medianStep', 'minMax', 'fmtNum'], {
    ctx, W: 900, H: 400, M: { t: 10, b: 30, l: 50, r: 50 },
    px: (tt) => 50 + (tt / (72 * 3600)) * 800,
    state: { weatherCh: new Set(['oat_f']), weather: wx, start: 0, end: 72 * 3600 },
    ...consts(['WCH']),                  // read from the file, not retyped
  });
  drawWeather();
  assert.equal(ctx.subpaths, 2,
    'the weather line drew straight through a 24 h hole - F-03 all over again');
});

test('M-11 - continuous weather still draws as ONE run', () => {
  const wx = [];
  for (let i = 0; i < 72; i++) wx.push({ ts_utc: i * 3600, oat_f: 50 + (i % 10) });
  const ctx = recorder();
  const { drawWeather } = compile(['drawWeather', 'medianStep', 'minMax', 'fmtNum'], {
    ctx, W: 900, H: 400, M: { t: 10, b: 30, l: 50, r: 50 },
    px: (tt) => 50 + (tt / (72 * 3600)) * 800,
    state: { weatherCh: new Set(['oat_f']), weather: wx, start: 0, end: 72 * 3600 },
    ...consts(['WCH']),
  });
  drawWeather();
  assert.equal(ctx.subpaths, 1, 'unbroken weather must not be split into runs');
});

test('drawSeries still breaks on gaps — the rule F-03 failed to inherit', () => {
  const src = grabSource('drawSeries', html());
  assert.ok(/\(p\.t\s*-\s*prev\.t\)\s*>\s*gap/.test(src),
    'drawSeries lost its gap test; the chart would now fabricate continuity too');
});

// ── the mini-map must not invent a record it does not have ───────────────────────────────
// Reported from a screenshot: the strip read "whole record · Jun 23 → Sep 15 · 84 days" for a
// 61-day record. Dragging the box's EDGE was unclamped (panning was clamped from the start),
// so state.start went before the beginning of the record, ovSpan widened to contain it, and
// the caption kept claiming the widened span was the record.

test('GUI · the overview caption never calls a widened span "whole record"', () => {
  const pts = hourly([[0, 300]]);
  const rec = recorder();
  let labelText = '';
  const label = { set textContent(v) { labelText = v; }, get textContent() { return labelText; } };
  const env = {
    ovx: rec, OVW: 900, OVH: 48, OVM: { t: 3, b: 3 },
    // window starts three weeks BEFORE anything was recorded
    state: { extent: { min_ts: pts[0].t, max_ts: pts[pts.length - 1].t },
             start: pts[0].t - 21 * 86400, end: pts[pts.length - 1].t },
    ovData: { id: 1, pts }, colorOf: () => '#4aa8ff',
    $: () => label,
    ...consts(['OV_MIN_BOX']),
  };
  const { drawOverview } = compile(
    ['drawOverview', 'ovGapThreshold', 'medianStep', 'ovFmtSpan', 'ovSpan', 'ovExtent', 'ovPx', 'ovBox'], env);
  drawOverview();
  assert.ok(!/^whole record/.test(labelText),
    `the caption claimed a span outside the record was the whole record: "${labelText}"`);
  assert.ok(/^record /.test(labelText), `expected an honest caption, got "${labelText}"`);
});

// ── THE TEST THAT SHOULD HAVE EXISTED FIRST ──────────────────────────────────────────────
// On 2026-09-16 the viewer shipped with `const e = ovExtent()` inside a handler declared as
// `pointermove, e => {...}`. That is a SyntaxError, so the ENTIRE 171 KB script failed to
// parse: no picker, no chart, a permanent "loading…", and nothing in the server logs. It
// reached the operator's browser.
//
// Every other test here extracts ONE NAMED FUNCTION and compiles it in isolation, so none of
// them could see it — the offending code lives in an anonymous arrow passed to
// addEventListener. Parsing the whole file is the cheapest possible check and it catches the
// most expensive possible failure. It runs first for a reason.

import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const STATIC = join(dirname(fileURLToPath(import.meta.url)), '..', '..',
                    'src', 'hvactrend', 'api', 'static');

function scriptBlocks(file) {
  const h = readFileSync(join(STATIC, file), 'utf8').replace(/\r/g, '');
  const out = [];
  let i = 0;
  for (;;) {
    const a = h.indexOf('<script>', i);
    if (a < 0) break;
    const b = h.indexOf('</script>', a);
    if (b < 0) break;
    out.push(h.slice(a + 8, b));
    i = b + 9;
  }
  return out;
}

test('EVERY page\'s script parses — a syntax error takes the whole page down', () => {
  const pages = readdirSync(STATIC).filter(f => f.endsWith('.html'));
  assert.ok(pages.length >= 4, `expected the static pages, found ${pages}`);
  for (const page of pages) {
    const blocks = scriptBlocks(page);
    assert.ok(blocks.length > 0, `${page} has no <script> block — did the markup change?`);
    blocks.forEach((src, i) => {
      try {
        new Function(src);                      // parse only; nothing is executed
      } catch (err) {
        assert.fail(`${page} script #${i + 1} does not parse: ${err.message}`);
      }
    });
  }
});


// ── 2026-10-14 · a point's missing stretches are shown, not left to a 3-px ribbon ─────────
// The tower panel loaded an old database and 45 of its points answered nothing for 27 hours.
// The viewer drew that as a thin line along the bottom edge, which the operator did not read as
// "this point was gone". The server now sends each point's outages; these pin how they read.

test('outages · the words, the readout lookup and the legend total', () => {
  const { outageText, outageAt, outageInView, outageDur } =
    compile(['outageText', 'outageAt', 'outageInView', 'outageDur']);
  assert.equal(outageText('comm-loss'), 'no response');
  assert.equal(outageText('fault'), 'faulted');
  assert.equal(outageText(null), 'no good reading');
  const runs = [{ a: 100, b: 200, q: 'fault' }, { a: 1000, b: 99000, q: 'comm-loss' }];
  assert.equal(outageAt(runs, 150).q, 'fault');
  assert.equal(outageAt(runs, 200), null, 'b is exclusive');
  assert.deepEqual(outageInView(runs, 0, 50000), { secs: 100 + 49000, q: 'comm-loss' });
  assert.equal(outageDur(27.4 * 3600), '27.4 h');
  assert.equal(outageDur(600), '10 min');
});

// ── 2026-10-27 · an OVR mark changes without a reload ────────────────────────────────────
// The operator: the marks "only update when you refresh the browser". They came with /api/logged
// alone, which the auto-refresh skips on purpose (it is the heaviest query in the app).

function ovrRow(id) {
  return {
    id, badge: null, title: '', painted: 0,
    querySelector(s) {
      if (s === '.ovr') return this.badge ? { remove: () => { this.badge = null; } } : null;
      if (s === '.edit') return { insertAdjacentHTML: (_pos, h) => { this.badge = h; this.painted++; } };
      return null;
    },
  };
}

function ovrEnv(logged, reply) {
  const rows = logged.map(p => ovrRow(p.id));
  const env = {
    state: { logged },
    rows,
    calls: 0,
    document: {
      hidden: false,
      querySelectorAll: sel => rows.filter(r => sel === `.pt[data-id="${r.id}"]`),
    },
    j: async () => { env.calls++; if (reply instanceof Error) throw reply; return reply; },
    esc: s => String(s),
    ptTitle: p => (p.override ? `OVERRIDDEN: ${p.override.text}` : 'plain'),
  };
  return env;
}

const OV = (id, text, extra = {}) => ({ point_id: id, level: 8, text, ...extra });

test('overrides · a new mark appears, a cleared one goes, an unchanged one is left alone', async () => {
  const logged = [{ id: 1, override: OV(1, 'AC-1 X = ON') }, { id: 2 },
                  { id: 3, override: OV(3, 'AC-3 Y = 1.2') }];
  const env = ovrEnv(logged, { overrides: [OV(2, 'AC-2 Z = OFF'), OV(3, 'AC-3 Y = 1.2')] });
  env.rows[0].badge = 'old';
  env.rows[2].badge = 'kept';
  const { refreshOverrides } = compile(['refreshOverrides', 'paintOverride', 'ovrBadge'], env);
  await refreshOverrides();
  assert.equal(logged[0].override, undefined, 'the cleared override is dropped');
  assert.equal(env.rows[0].badge, null, 'and its mark removed');
  assert.equal(env.rows[0].title, 'plain');
  assert.equal(logged[1].override.text, 'AC-2 Z = OFF');
  assert.match(env.rows[1].badge, /OVR 8/);
  assert.equal(env.rows[1].title, 'OVERRIDDEN: AC-2 Z = OFF', 'the tooltip follows');
  assert.equal(env.rows[2].badge, 'kept', 'an unchanged row is not repainted');
});

test('overrides · held on purpose is drawn hollow, and a change of reason repaints', async () => {
  const logged = [{ id: 5, override: OV(5, 'AC-8 Fan = ON') }];
  const env = ovrEnv(logged, { overrides: [OV(5, 'AC-8 Fan = ON', { expected: 'held for the tenant' })] });
  const { refreshOverrides } = compile(['refreshOverrides', 'paintOverride', 'ovrBadge'], env);
  await refreshOverrides();
  assert.match(env.rows[0].badge, /class="ovr exp"/);
});

test('overrides · a hidden tab does not ask, and a failed ask changes nothing', async () => {
  const logged = [{ id: 1, override: OV(1, 'AC-1 X = ON') }];
  const hidden = ovrEnv(logged, { overrides: [] });
  hidden.document.hidden = true;
  await compile(['refreshOverrides', 'paintOverride', 'ovrBadge'], hidden).refreshOverrides();
  assert.equal(hidden.calls, 0);
  const failing = ovrEnv(logged, new Error('503'));
  await compile(['refreshOverrides', 'paintOverride', 'ovrBadge'], failing).refreshOverrides();
  assert.equal(failing.calls, 1);
  assert.equal(logged[0].override.text, 'AC-1 X = ON', 'a failed ask must not clear a mark');
});

test('overrides · the refresh is scheduled, and the row builder uses the shared badge', () => {
  const src = html();
  assert.match(src, /setInterval\(refreshOverrides, OVR_REFRESH_MS\)/);
  assert.match(grabSource('ptRow', src), /\$\{ovrBadge\(p\)\}/);
});

test('outages · each stretch is a full-height band with its name on it', () => {
  const ctx = recorder();
  const env = {
    ctx, W: 1000, H: 400, M: { l: 50, r: 10, t: 20, b: 30 },
    state: { series: { 7: { samples: [{ t: 0 }], meta: { display_name: 'Tower 1 Level SP' },
                            outages: [{ a: 1000, b: 9000, q: 'comm-loss' }] } } },
    px: t => 50 + t / 10, activeIds: () => [7], colorOf: () => '#b48cff',
    clipPlot: () => {}, shortName: m => m.display_name, faultedRuns: () => [],
  };
  const { drawFaulted } = compile(['drawFaulted', 'outageRuns', 'outageText'], env);
  drawFaulted(7);
  const band = ctx.rects.find(r => r.h === 400 - 30 - 20);
  assert.ok(band, 'a full-height band');
  assert.equal(band.x, 150);
  assert.equal(band.w, 800);
  assert.deepEqual(ctx.texts.map(x => x.t), ['Tower 1 Level SP: no response']);
});


// ── 2026-10-27 review · DST ends Sunday 1 November ───────────────────────────────────────
// TZ=America/New_York (the pytest wrapper sets it). 1 Nov 2026 is 25 hours long.
const local = (y, mo, d, h = 0) => Math.floor(new Date(y, mo - 1, d, h).getTime() / 1000);

test('DST · a whole day steps to the next midnight, not 24 h later', () => {
  const { addLocalDays, isLocalMidnight } = compile(['addLocalDays', 'isLocalMidnight']);
  assert.equal(addLocalDays(local(2026, 11, 1), 1), local(2026, 11, 2));
  assert.equal(local(2026, 11, 2) - local(2026, 11, 1), 90000, 'the 25-hour day');
  assert.equal(addLocalDays(local(2027, 3, 14), 1) - local(2027, 3, 14), 82800, 'the 23-hour day');
  assert.ok(isLocalMidnight(local(2026, 11, 2)) && !isLocalMidnight(local(2026, 11, 2, 1)));
});

test('DST · stepping a calendar day keeps it midnight to midnight', () => {
  const state = { start: local(2026, 10, 31), end: local(2026, 11, 1),
                  extent: { min_ts: local(2026, 7, 1) } };
  const env = { state, DAY_S: 86400, clearXPin() {}, markRange() {}, currentPreset() {},
                updateRangeLabel() {}, persist() {}, loadSeries() {},
                liveAnchor: () => local(2026, 12, 1) };
  const { stepWindow } = compile(['stepWindow', 'addLocalDays', 'isLocalMidnight'], env);
  stepWindow(1);
  assert.deepEqual([state.start, state.end], [local(2026, 11, 1), local(2026, 11, 2)]);
  stepWindow(1);
  assert.deepEqual([state.start, state.end], [local(2026, 11, 2), local(2026, 11, 3)]);
});

test('DST · last week\u2019s ghost lines up by the clock, not by 604,800 s', () => {
  const { cmpShift } = compile(['cmpShift']);
  // last Monday 06:00 EDT, shown on this Monday after the change: 06:00 EST
  assert.equal(cmpShift(local(2026, 10, 26, 6), 604800), local(2026, 11, 2, 6));
  assert.equal(cmpShift(local(2026, 10, 20, 6), 604800), local(2026, 10, 27, 6), 'no change');
  assert.equal(cmpShift(1000, 3600), 4600, 'a non-day offset is plain seconds');
});

test('the legend ages an hourly bucket from its end, not its start', () => {
  const { lastLbl } = compile(['lastLbl'], { fmtNum: v => String(v) });
  const now = Math.floor(Date.now() / 1000);
  const start = now - 57 * 60;                      // the current hour's bucket, 57 min in
  assert.equal(lastLbl({ last: 55.5, lastT: start, bucketS: 3600 }), 'now 55.5');
  assert.match(lastLbl({ last: 55.5, lastT: start, bucketS: 0 }), /class="stale"/,
               'a raw reading 57 min old IS stale');
  assert.match(lastLbl({ last: 55.5, lastT: now - 3 * 3600, bucketS: 3600 }), /class="stale"/,
               'an hourly bucket that ended 2 h ago is stale too');
});

test('Δ mode · the reference is not drawn as if it were a difference', () => {
  const series = { 1: { samples: [{ t: 0, v: 55 }] }, 2: { samples: [{ t: 0, v: 61 }] },
                   'd:1-2': { samples: [{ t: 0, v: -6 }] } };
  const env = {
    state: { transform: 'first', series, sel: [1, 2] }, XF_CACHE: {},
    xfKey: () => 'k', _proc: (k, s) => ({ proc: s }),
    refFor: id => (id === 1 ? null : series[1].samples),
    fleetMedianSeries: () => [], gapOf: () => 300,
    matchByTime: (a, b) => a.map((p, i) => ({ t: p.t, x: p.v, y: b[i].v })),
  };
  const { drawSamples } = compile(['drawSamples', 'isDelta'], env);
  assert.deepEqual(drawSamples(1), [], 'the reference');
  assert.deepEqual(drawSamples(2), [{ t: 0, v: 6, q: 'good' }]);
  assert.deepEqual(drawSamples('d:1-2'), [{ t: 0, v: -6 }], 'an A-B line is left as it is');
});

test('a burst of wheel notches fetches once', () => {
  const timers = [];
  const env = { _loadSoonT: null, loads: 0,
                setTimeout: (fn) => { timers.push(fn); return timers.length; },
                clearTimeout: (h) => { if (h) timers[h - 1] = null; } };
  env.loadSeries = () => { env.loads++; };
  const { loadSeriesSoon } = compile(['loadSeriesSoon'], env);
  loadSeriesSoon(); loadSeriesSoon(); loadSeriesSoon();
  timers.filter(Boolean).forEach(fn => fn());
  assert.equal(env.loads, 1);
  const src = html();
  const wheel = src.slice(src.indexOf("cv.addEventListener('wheel'"));
  assert.match(wheel.slice(0, wheel.indexOf('{passive:false}')), /loadSeriesSoon\(\)/);
});

test('a saved view cannot put markup into the page', () => {
  const state = {};
  const env = { state, COLOR_MODES: ['series'], METRIC_COLOR: null, syncControls() {} };
  const { applySnapshot } = compile(['applySnapshot'], env);
  applySnapshot({ sel: [1, '2', 'x'], color: { 1: 'red"><img src=x onerror=alert(1)>', 2: '#4aa8ff',
                                               3: 'hsl(120,62%,62%)' },
                  todFrom: '<b>5</b>', todTo: 17, deltas: [{ a: '1" onmouseover="x', b: 2 }, { a: 1, b: 2 }],
                  compareOffset: '604800' });
  assert.deepEqual(state.sel, [1, 2]);
  assert.deepEqual(Object.keys(state.color).sort(), ['2', '3']);
  assert.equal(state.todFrom, 5);
  assert.equal(state.todTo, 17);
  assert.deepEqual(state.deltas, [{ a: 1, b: 2 }]);
  assert.equal(state.compareOffset, 604800);
});
