// Pull a function out of the SHIPPED trends.html and hand it back as a callable.
//
// Why this exists: trends.html is 3,000+ lines of canvas JavaScript with no build step and,
// until now, no tests — 4,017 lines of JS against 10,483 of Python that had 199. An external
// audit on 2026-09-15 found three real bugs in it, one of which (the mini-map drawing a
// straight line through a 70-hour collector outage) had already been found and fixed in two
// sibling renderers in the same file.
//
// Nothing here retypes the code. Each test extracts the exact source text at runtime, so a
// test can never drift away from what actually ships: if the function is renamed or removed
// the extraction throws, and if its body changes the test runs the changed body.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
// HVACTREND_VIEWER_HTML points the harness at a different copy of the file. Used to prove a
// test actually discriminates: mutate a copy back to the pre-fix behaviour and confirm the
// test fails against it. A test that passes both ways is not a test.
export const VIEWER = process.env.HVACTREND_VIEWER_HTML
  || join(HERE, '..', '..', 'src', 'hvactrend', 'api', 'static', 'trends.html');

export function html() {
  return readFileSync(VIEWER, 'utf8').replace(/\r/g, '');
}

/** Source text of `function <name>(...){...}`, brace-balanced (not regex-terminated). */
export function grabSource(name, src = html()) {
  const head = `function ${name}(`;
  let start = src.indexOf(head);
  if (start < 0) throw new Error(`trends.html no longer defines ${name}()`);
  // keep `async`: without it an extracted body that awaits does not even compile
  if (src.slice(start - 6, start) === 'async ') start -= 6;
  const open = src.indexOf('{', start);
  let depth = 0, i = open, inStr = null, inTpl = 0;
  for (; i < src.length; i++) {
    const c = src[i], prev = src[i - 1];
    if (inStr) { if (c === inStr && prev !== '\\') inStr = null; continue; }
    if (inTpl) {
      if (c === '`' && prev !== '\\') inTpl--;
      continue;
    }
    if (c === '"' || c === "'") { inStr = c; continue; }
    if (c === '`') { inTpl++; continue; }
    if (c === '/' && src[i + 1] === '/') { i = src.indexOf('\n', i); continue; }
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) return src.slice(start, i + 1); }
  }
  throw new Error(`unbalanced braces extracting ${name}()`);
}

/**
 * Compile one or more extracted functions with an explicit environment.
 * `env` becomes the set of globals the code closes over; the returned object exposes the
 * named functions plus the env, so a test can inspect what the code did to it.
 */
export function compile(names, env = {}) {
  const src = html();
  const bodies = names.map(n => grabSource(n, src)).join('\n');
  const keys = Object.keys(env);
  const factory = new Function(...keys, `${bodies}\nreturn {${names.join(',')}};`);
  const fns = factory(...keys.map(k => env[k]));
  return { ...fns, env };
}

/** A 2D context that records what was drawn instead of drawing it. */
export function recorder() {
  const rec = {
    ops: [], subpaths: 0, lines: 0, rects: [],
    moveTo() { rec.subpaths++; rec.ops.push('M'); },
    lineTo() { rec.lines++; rec.ops.push('L'); },
    beginPath() { rec.ops.push('B'); }, closePath() {}, fill() { rec.ops.push('F'); },
    stroke() { rec.ops.push('S'); }, save() {}, restore() {}, clip() {},
    // style is captured with the rect: drawOverview paints both the dimming veil
    // and the gap markers with fillRect, and a test needs to tell them apart.
    fillRect(x, y, w, h) { rec.rects.push({ x, y, w, h, style: String(rec.fillStyle) }); },
    strokeRect() {}, arc() {}, setLineDash() {},
    texts: [], fillText(t, x, y) { rec.texts.push({ t, x, y }); },
    clearRect() { rec.ops.push('C'); }, rect() {}, quadraticCurveTo() {},
    measureText: () => ({ width: 10 }),
  };
  for (const p of ['fillStyle', 'strokeStyle', 'lineWidth', 'globalAlpha', 'font',
                   'textAlign', 'textBaseline']) {
    let v; Object.defineProperty(rec, p, { get: () => v, set: (x) => { v = x; } });
  }
  return rec;
}

/**
 * Values of top-level `const NAME = <literal>;` declarations, read from the shipped file
 * rather than retyped — so a tuning constant that changes in trends.html changes here too.
 */
export function consts(names, src = html()) {
  const out = {};
  for (const n of names) {
    // Plain string search, not a template-literal regex: `\s` inside a template literal is
    // just `s`, which silently turns the pattern into something that never matches.
    const head = `\nconst ${n} = `;
    const at = src.indexOf(head);
    if (at < 0) throw new Error(`trends.html no longer defines const ${n}`);
    const from = at + head.length;
    const end = src.indexOf(';', from);
    out[n] = new Function(`return (${src.slice(from, end)});`)();
  }
  return out;
}
