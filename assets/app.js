/* ==========================================================================
   Turmeric Study Dashboard — application logic
   Research Solutions (M&A Research Solutions LLC)

   The payload in data/dashboard_data.js is record-level, not pre-aggregated.
   Everything on screen is computed here, which is what lets the filter bar
   re-cut all eight panels without a round trip to the server.
   ========================================================================== */

/* ============================ ACCESS GATE ============================ */
/* The payload is not merely hidden behind this screen — it is AES-256-GCM
   encrypted in data/dashboard_data.js and cannot be read without the
   password, which is never stored in the page. These parameters must match
   encrypt_payload() in scripts/update_dashboard.py. */
const PBKDF2_ITERATIONS = 250000;
const SESSION_KEY = 'tq_payload';

const b64bytes = b64 => Uint8Array.from(atob(b64), c => c.charCodeAt(0));

async function decryptPayload(password, blob) {
  const raw = b64bytes(blob);
  const salt = raw.slice(0, 16), iv = raw.slice(16, 28), ct = raw.slice(28);
  const material = await crypto.subtle.importKey(
    'raw', new TextEncoder().encode(password), 'PBKDF2', false, ['deriveKey']);
  const key = await crypto.subtle.deriveKey(
    { name: 'PBKDF2', salt, iterations: PBKDF2_ITERATIONS, hash: 'SHA-256' },
    material, { name: 'AES-GCM', length: 256 }, false, ['decrypt']);
  // A wrong password fails the GCM tag check and throws — that *is* the auth.
  const packed = await crypto.subtle.decrypt({ name: 'AES-GCM', iv }, key, ct);
  const stream = new Blob([packed]).stream().pipeThrough(new DecompressionStream('deflate'));
  return JSON.parse(await new Response(stream).text());
}

function unlock(payload) {
  window.DASHBOARD_DATA = payload;
  document.getElementById('auth-gate').style.display = 'none';
  document.getElementById('site-content').style.display = 'block';
  if (!window.__booted) { boot(payload); window.__booted = true; }
}

async function checkAuth() {
  const input = document.getElementById('auth-pw');
  const err = document.getElementById('auth-error');
  const btn = document.querySelector('.auth-btn');
  const pw = input.value;
  if (!pw) return;

  if (!window.DASHBOARD_ENC) {
    // Plaintext build (local debugging) — nothing to decrypt.
    if (window.DASHBOARD_DATA) { unlock(window.DASHBOARD_DATA); return; }
    err.textContent = 'Data file not loaded. Run scripts/update_dashboard.py.';
    return;
  }

  err.style.color = ''; err.textContent = 'Decrypting…';
  btn.disabled = true;
  try {
    const payload = await decryptPayload(pw, window.DASHBOARD_ENC);
    try { sessionStorage.setItem(SESSION_KEY, JSON.stringify(payload)); } catch (e) {}
    err.textContent = '';
    input.classList.remove('error');
    unlock(payload);
  } catch (e) {
    err.textContent = 'Incorrect password — the data could not be decrypted.';
    input.classList.add('error'); input.value = ''; input.focus();
    setTimeout(() => input.classList.remove('error'), 400);
  } finally {
    btn.disabled = false;
  }
}

/* Re-open without re-typing within the same tab. The decrypted payload lives
   in sessionStorage, which dies with the tab; the password is never kept. */
window.addEventListener('DOMContentLoaded', () => {
  if (!window.isSecureContext && window.DASHBOARD_ENC) {
    const err = document.getElementById('auth-error');
    if (err) err.textContent = 'This page must be served over HTTPS (or localhost) to decrypt.';
  }
  try {
    const cached = sessionStorage.getItem(SESSION_KEY);
    if (cached) unlock(JSON.parse(cached));
  } catch (e) {}
});

/* ============================== THEME =============================== */
function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  const dark = t === 'dark';
  const i = document.getElementById('themeIcon'), x = document.getElementById('themeTxt');
  if (i) i.textContent = dark ? '☀️' : '🌙';
  if (x) x.textContent = dark ? 'Light' : 'Dark';
  try { localStorage.setItem('tq_theme', t); } catch (e) {}
}
function toggleTheme() {
  const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  applyTheme(next);
  if (window.__booted) renderAll();
}
(function initTheme() {
  let t = null;
  try { t = localStorage.getItem('tq_theme'); } catch (e) {}
  if (!t) t = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  applyTheme(t);
})();

/* ============================ DATA LAYER ============================ */
/* Assigned by boot() once the payload has been decrypted — it cannot be read
   at script-load time any more. */
let D = null;
let AW, TS, SP, LB;

function mkTable(t) {
  const f = {};
  (t.fields || []).forEach((n, i) => { f[n] = i; });
  return { f, fields: t.fields || [], rows: t.rows || [] };
}

/* --- labels -------------------------------------------------------- */
function meta(ds, field) { return (LB[ds] || {})[field] || null; }
function lab(ds, field, code) {
  if (code === null || code === undefined || code === '') return null;
  const m = meta(ds, field);
  if (m && m.c && m.c[code] !== undefined) return m.c[code];
  return String(code);
}
function ord(ds, field) {
  const m = meta(ds, field);
  return (m && m.o) ? m.o.slice() : [];
}
function title(ds, field, fb) {
  const m = meta(ds, field);
  return (m && m.t) || fb || field;
}

/* Survey text carries coding artefacts ("-999. Don't know", "99 Other
   (specify)"). Strip them once, here, so no chart has to. */
function pretty(s) {
  if (s === null || s === undefined) return '—';
  let x = String(s).trim();
  x = x.replace(/^-?\d{2,3}\.?\s*/, '');
  x = x.replace(/\s*\(\s*(please\s+)?specify\s*\)\s*$/i, '');
  x = x.replace(/\s*\(specify\)\s*$/i, '');
  x = x.replace(/\s+/g, ' ').trim();
  if (/^refused/i.test(x)) x = 'Refused';
  if (/^don.?t know/i.test(x)) x = "Don't know";
  return x || '—';
}
function short(s, n) {
  const x = pretty(s);
  n = n || 40;
  return x.length > n ? x.slice(0, n - 1).trim() + '…' : x;
}
const NONSUBSTANTIVE = /^(don't know|refused|none|n\/a|not recorded|no response)$/i;

/* --- unified dimensions -------------------------------------------- */
/* The two instruments use different city and enumerator code frames but
   overlapping real-world names, so every filter works on the name. */
let CITY_LIST = [], ENUM_LIST = [], MKT_TYPE_LIST = [], MKT_NAME_LIST = [], MKT_LOC_LIST = [];
let TS_BY_KEY = new Map();

function cityOf(ds, code) { return ds === 'aw' ? lab('aw', 'city', code) : lab('ts', 'sample_city', code); }
function enumOf(ds, code) { return ds === 'aw' ? lab('aw', 'Data_Collector', code) : lab('ts', 'enum_name', code); }

/* Every Sampling Survey vendor visit is either a retail-locality visit or a
   wholesale-market visit, never both — market_name ('1'=wholesale,
   '2'=retail) picks which of the two named fields is populated. This key
   ("field:code") is what the Market filter and locOk() both match on. */
function samplingLocKey(tsRow) {
  const isW = tsRow[TS.f.market_name] === '1';
  const fld = isW ? 'wholesale_market' : 'locality_retail';
  const v = tsRow[TS.f[fld]];
  if (v === null || v === undefined || v === '') return null;
  return fld + ':' + v;
}

function buildDimensions() {
  const cities = new Set(), enums = new Set();
  AW.rows.forEach(r => {
    const c = cityOf('aw', r[AW.f.city]); if (c) cities.add(c);
    const e = enumOf('aw', r[AW.f.Data_Collector]); if (e) enums.add(e.trim());
  });
  TS.rows.forEach(r => {
    const c = cityOf('ts', r[TS.f.sample_city]); if (c) cities.add(c);
    const e = enumOf('ts', r[TS.f.enum_name]); if (e) enums.add(e.trim());
  });
  CITY_LIST = [...cities].sort();
  ENUM_LIST = [...enums].sort();
  MKT_TYPE_LIST = ord('ts', 'market_name').map(v => ({ v, l: lab('ts', 'market_name', v) }));
  const seen = new Set();
  MKT_NAME_LIST = [];
  AW.rows.forEach(r => {
    const v = r[AW.f.market_name];
    if (v && !seen.has(v)) { seen.add(v); MKT_NAME_LIST.push({ v, l: lab('aw', 'market_name', v) }); }
  });
  MKT_NAME_LIST.sort((a, b) => a.l.localeCompare(b.l));

  TS_BY_KEY = new Map(TS.rows.map(r => [r[TS.f.key], r]));
  const locSeen = new Set();
  MKT_LOC_LIST = [];
  TS.rows.forEach(r => {
    const k = samplingLocKey(r);
    if (!k || locSeen.has(k)) return;
    locSeen.add(k);
    const isW = k.startsWith('wholesale_market:');
    MKT_LOC_LIST.push({ v: k, l: pretty(lab('ts', isW ? 'wholesale_market' : 'locality_retail', r[TS.f[isW ? 'wholesale_market' : 'locality_retail']])), isW });
  });
  MKT_LOC_LIST.sort((a, b) => (a.isW === b.isW) ? a.l.localeCompare(b.l) : (a.isW ? -1 : 1));
}

/* --- filter state --------------------------------------------------- */
const F = { city: new Set(), enum: new Set(), resp: new Set(), mktType: new Set(), mktName: new Set(), mktLoc: new Set() };

const setOk = (set, v) => set.size === 0 || (v !== null && set.has(v));

/* Awareness Survey respondents are filtered on their real-world type, not
   the raw instrument code: the Retailer_survey instrument's type_of_vendor
   splits Retailer vs Wholesaler, and the Consumer_survey instrument's Q_1
   (bought for HH or business use) splits Household vs Business — a "both"
   answer belongs to both consumer categories at once. */
function respCategoriesOf(awRow) {
  const t = awRow[AW.f.Type_of_survey];
  if (t === 'RS') return [vendorTypeOf(awRow)];
  if (t === 'CS') {
    const v = pretty(lab('aw', 'Q_1', awRow[AW.f.Q_1]));
    const cats = [];
    if (/HH|both/i.test(v)) cats.push('hh');
    if (/business|both/i.test(v)) cats.push('biz');
    return cats;
  }
  return [];
}
const respOk = r => F.resp.size === 0 || respCategoriesOf(r).some(c => F.resp.has(c));
const locOk = tsRow => F.mktLoc.size === 0 || F.mktLoc.has(samplingLocKey(tsRow));

function awFiltered() {
  return AW.rows.filter(r =>
    setOk(F.city, cityOf('aw', r[AW.f.city])) &&
    setOk(F.enum, (enumOf('aw', r[AW.f.Data_Collector]) || '').trim()) &&
    respOk(r) &&
    setOk(F.mktName, r[AW.f.market_name])
  );
}
function tsFiltered() {
  return TS.rows.filter(r =>
    setOk(F.city, cityOf('ts', r[TS.f.sample_city])) &&
    setOk(F.enum, (enumOf('ts', r[TS.f.enum_name]) || '').trim()) &&
    setOk(F.mktType, r[TS.f.market_name]) &&
    locOk(r)
  );
}
function spFiltered() {
  return SP.rows.filter(r =>
    setOk(F.city, cityOf('ts', r[SP.f.city])) &&
    setOk(F.enum, (enumOf('ts', r[SP.f.enum]) || '').trim()) &&
    setOk(F.mktType, r[SP.f.market]) &&
    (F.mktLoc.size === 0 || (TS_BY_KEY.has(r[SP.f.vkey]) && locOk(TS_BY_KEY.get(r[SP.f.vkey]))))
  );
}
const filtersActive = () =>
  F.city.size || F.enum.size || F.resp.size || F.mktType.size || F.mktName.size || F.mktLoc.size;

/* Working sets, recomputed once per render pass. */
let Q = {};
function recompute() {
  const aw = awFiltered();
  Q.aw = aw;
  Q.con = aw.filter(r => r[AW.f.Consent] === '1');
  Q.rs = Q.con.filter(r => r[AW.f.Type_of_survey] === 'RS');
  Q.cs = Q.con.filter(r => r[AW.f.Type_of_survey] === 'CS');
  Q.ts = tsFiltered();
  Q.sp = spFiltered();
}

/* ========================= AGGREGATION HELPERS ====================== */
/* Distribution of a select_one, in the instrument's own choice order. */
function distOf(rows, ds, tbl, field, opt) {
  opt = opt || {};
  const i = tbl.f[field];
  if (i === undefined) return [];
  const counts = new Map();
  rows.forEach(r => {
    const v = r[i];
    if (v === null || v === undefined || v === '') return;
    counts.set(v, (counts.get(v) || 0) + 1);
  });
  let order = ord(ds, field);
  if (!order.length) order = [...counts.keys()];
  let out = order
    .filter(v => counts.has(v))
    .map(v => ({ code: v, label: pretty(lab(ds, field, v)), value: counts.get(v) }));
  [...counts.keys()].forEach(v => {
    if (!order.includes(v)) out.push({ code: v, label: pretty(lab(ds, field, v)), value: counts.get(v) });
  });
  if (opt.dropSpecial) out = out.filter(x => !NONSUBSTANTIVE.test(x.label));
  if (opt.sort) out.sort((a, b) => b.value - a.value);
  if (opt.top) out = out.slice(0, opt.top);
  out.forEach(x => { x.full = x.label; });
  if (opt.maxLabel) out.forEach(x => { x.label = short(x.label, opt.maxLabel); });
  return out;
}

/* Distribution of a select_multiple. Base is respondents who were asked. */
function multiOf(rows, ds, tbl, field, opt) {
  opt = opt || {};
  const i = tbl.f[field];
  if (i === undefined) return { rows: [], base: 0 };
  const counts = new Map();
  let base = 0;
  rows.forEach(r => {
    const v = r[i];
    if (!v || !v.length) return;
    base++;
    (Array.isArray(v) ? v : [v]).forEach(c => counts.set(c, (counts.get(c) || 0) + 1));
  });
  let order = ord(ds, field);
  if (!order.length) order = [...counts.keys()];
  let out = order.filter(v => counts.has(v))
    .map(v => ({ code: v, label: pretty(lab(ds, field, v)), value: counts.get(v) }));
  if (opt.dropSpecial) out = out.filter(x => !NONSUBSTANTIVE.test(x.label));
  if (opt.sort !== false) out.sort((a, b) => b.value - a.value);
  if (opt.top) out = out.slice(0, opt.top);
  if (opt.pct) out.forEach(x => { x.value = base ? Math.round(1000 * x.value / base) / 10 : 0; });
  out.forEach(x => { x.full = x.label; });
  if (opt.maxLabel) out.forEach(x => { x.label = short(x.label, opt.maxLabel); });
  return { rows: out, base };
}

/* Share of rows whose coded answer falls in `codes`, over rows that were asked. */
/* select_multiple answers arrive as an array of codes, select_one as a bare
   string, and the same question can be either depending on the instrument
   revision — so both helpers read "is any of these codes selected". */
function hasCode(v, codes) {
  return Array.isArray(v) ? v.some(c => codes.includes(c)) : codes.includes(v);
}
function shareIn(rows, tbl, field, codes) {
  const i = tbl.f[field];
  if (i === undefined) return { pct: 0, n: 0, base: 0 };
  let base = 0, n = 0;
  rows.forEach(r => {
    const v = r[i];
    if (v === null || v === undefined || v === '' || (Array.isArray(v) && !v.length)) return;
    base++;
    if (hasCode(v, codes)) n++;
  });
  return { pct: base ? Math.round(1000 * n / base) / 10 : 0, n, base };
}
function countWhere(rows, tbl, field, codes) {
  const i = tbl.f[field];
  if (i === undefined) return 0;
  return rows.filter(r => hasCode(r[i], codes)).length;
}
function nonNull(rows, tbl, field) {
  const i = tbl.f[field];
  if (i === undefined) return 0;
  return rows.filter(r => r[i] !== null && r[i] !== undefined && r[i] !== '' &&
    (!Array.isArray(r[i]) || r[i].length)).length;
}

function byDay(rows, idx) {
  const m = new Map();
  rows.forEach(r => { const d = r[idx]; if (d) m.set(d, (m.get(d) || 0) + 1); });
  return [...m.entries()].sort((a, b) => a[0] < b[0] ? -1 : 1).map(([date, count]) => ({ date, count }));
}

function nums(rows, idx) {
  const out = [];
  rows.forEach(r => { const v = r[idx]; if (typeof v === 'number' && isFinite(v)) out.push(v); });
  return out;
}
function stats(arr) {
  const a = arr.slice().sort((x, y) => x - y);
  const n = a.length;
  if (!n) return null;
  const at = p => a[Math.min(n - 1, Math.max(0, Math.floor(p * (n - 1))))];
  return {
    n, min: a[0], max: a[n - 1],
    p05: at(0.05), q1: at(0.25), med: at(0.5), q3: at(0.75), p95: at(0.95),
    mean: a.reduce((s, x) => s + x, 0) / n,
  };
}
const median = arr => { const s = stats(arr); return s ? s.med : null; };

function histogram(vals, binSize) {
  if (!vals.length) return [];
  const lo = Math.floor(Math.min(...vals) / binSize) * binSize;
  const hi = Math.ceil(Math.max(...vals) / binSize) * binSize;
  const bins = new Map();
  for (let b = lo; b < hi; b += binSize) bins.set(b, 0);
  vals.forEach(v => {
    const b = Math.min(hi - binSize, Math.floor(v / binSize) * binSize);
    bins.set(b, (bins.get(b) || 0) + 1);
  });
  return [...bins.entries()].map(([b, c]) => ({ label: String(b), bin: b, value: c }));
}

/* ========================== CHART FOUNDATION ======================== */
Chart.register(ChartDataLabels);
const CV = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const S = i => CV('--series-' + i);
const PALETTE = () => [S(1), S(2), S(3), S(4), S(5), S(6), S(7), S(8)];
const SEQ = () => [CV('--seq-1'), CV('--seq-2'), CV('--seq-3'), CV('--seq-4'), CV('--seq-5')];
const TXT = () => CV('--text-2') || '#475569';
const TXT1 = () => CV('--text') || '#2D3450';
const GRID = () => CV('--grid') || '#eef1f6';
const MUTED = () => CV('--muted-fill') || '#e2e8f0';
const SURF = () => CV('--surface') || '#fff';
const FONT = { family: 'Inter' };

/* Named LBLS, not L — Leaflet owns the global `L`. */
const LBLS = a => a.map(x => x.label);
const V = a => a.map(x => x.value);
const SUM = a => a.reduce((s, x) => s + x.value, 0);
const pctOf = (a, b) => b ? Math.round(100 * a / b) : 0;
const fmt = n => (n === null || n === undefined) ? '—' : Number(n).toLocaleString();
const fmt1 = n => (n === null || n === undefined) ? '—' : Number(n).toFixed(1);

/* Merges one level into `plugins` and one level into each plugin block.
   A plain Object.assign would let any chart that sets `plugins` drop the
   `datalabels: {display:false}` default and the shared tooltip styling —
   which is exactly how the scatter ended up labelling all 1,500 points. */
function baseOpts(extra) {
  extra = extra || {};
  const defPlugins = {
    legend: { labels: { font: FONT, color: TXT(), boxWidth: 14 } },
    datalabels: { display: false },
    tooltip: {
      backgroundColor: 'rgba(30,36,56,.94)', titleFont: { family: 'Inter', size: 12, weight: '700' },
      bodyFont: { family: 'Inter', size: 12 }, padding: 10, cornerRadius: 8, displayColors: true, boxPadding: 4,
    },
  };
  const plugins = Object.assign({}, defPlugins, extra.plugins || {});
  Object.keys(defPlugins).forEach(k => {
    if (extra.plugins && extra.plugins[k]) plugins[k] = Object.assign({}, defPlugins[k], extra.plugins[k]);
  });
  const out = Object.assign({
    responsive: true, maintainAspectRatio: false,
    animation: { duration: 500 },
  }, extra);
  out.plugins = plugins;
  return out;
}

function wrapTick(label, max) {
  max = max || 18;
  const words = String(label).split(/\s+/), lines = []; let line = '';
  for (const w of words) {
    if (line && (line + ' ' + w).length > max) { lines.push(line); line = w; }
    else line = line ? line + ' ' + w : w;
  }
  if (line) lines.push(line);
  return lines.length ? lines : [String(label)];
}

/* Some survey options run to a hundred characters, so an axis or a legend has
   to shorten them. The full wording is kept on the series and shown, wrapped,
   in the tooltip — nothing on a chart is unreadable, it is only abbreviated.
   `arr` entries may carry `full`; where they do not, the visible label is
   already complete. */
const fullLabels = arr => arr.map(x => (x && x.full) || (x && x.label) || '');
const tipTitle = fulls => it => wrapTick(fulls[it[0].dataIndex] || it[0].label, 44);

/* Phone-width breakpoint, kept in step with the 720px one in theme.css. */
const isNarrow = () => window.innerWidth <= 720;

const CHARTS = {};
function mk(id, cfg) {
  const el = document.getElementById(id);
  if (!el) return null;
  const prev = Chart.getChart(el);
  if (prev) prev.destroy();
  const chart = new Chart(el, cfg);
  CHARTS[id] = chart;
  attachDownload(el, id);
  return chart;
}
function attachDownload(canvas, id) {
  const card = canvas.closest('.chart-card');
  if (!card || card.querySelector('.card-dl')) return;
  const btn = document.createElement('button');
  btn.className = 'card-dl'; btn.type = 'button';
  btn.title = 'Download this chart as PNG';
  btn.setAttribute('aria-label', 'Download this chart as PNG');
  btn.textContent = '⤓';
  btn.onclick = () => {
    const c = Chart.getChart(canvas);
    if (!c) return;
    const src = c.canvas, out = document.createElement('canvas');
    out.width = src.width; out.height = src.height;
    const ctx = out.getContext('2d');
    ctx.fillStyle = SURF(); ctx.fillRect(0, 0, out.width, out.height);
    ctx.drawImage(src, 0, 0);
    const a = document.createElement('a');
    a.href = out.toDataURL('image/png');
    a.download = 'turmeric-dashboard_' + id + '.png';
    a.click();
    toast('Chart saved as PNG');
  };
  card.appendChild(btn);
}
function fitHeight(id, n, per, pad) {
  const el = document.getElementById(id);
  if (!el) return;
  const wrap = el.closest('.chart-wrap');
  if (wrap) wrap.style.height = Math.max(150, n * (per || 30) + (pad || 54)) + 'px';
}
function noData(id, msg) {
  const el = document.getElementById(id);
  if (!el) return true;
  const wrap = el.closest('.chart-wrap');
  const prev = Chart.getChart(el);
  if (prev) prev.destroy();
  if (wrap) {
    wrap.style.height = '';
    let ph = wrap.querySelector('.chart-empty');
    if (!ph) { ph = document.createElement('div'); ph.className = 'chart-empty'; wrap.appendChild(ph); }
    ph.textContent = msg || 'No responses match the current filters.';
    ph.style.display = 'flex';
    el.style.display = 'none';
  }
  return true;
}
function clearEmpty(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const wrap = el.closest('.chart-wrap');
  if (wrap) {
    const ph = wrap.querySelector('.chart-empty');
    if (ph) ph.style.display = 'none';
  }
  el.style.display = 'block';
}
const has = arr => arr && arr.length && SUM(arr) > 0;

/* ============================ CHART TYPES =========================== */
/* `showPct` puts the share of the chart total next to the count — "161 (33%)".
   Only meaningful when the bars partition one base, so it stays off for price
   and quantity charts, where a share of the total is nonsense. */
function barChart(id, arr, color, horizontal, suffix, showPct) {
  if (!has(arr)) return noData(id);
  clearEmpty(id);
  const sfx = suffix || '';
  const total = SUM(arr);
  const withPct = showPct && !sfx && total > 0;
  const pctTxt = v => total ? Math.round(1000 * v / total) / 10 : 0;
  // A phone canvas is ~a third the width, so category labels have to break
  // earlier and each horizontal bar needs more vertical room for the extra
  // wrapped lines.
  const narrow = isNarrow();
  const catWrap = horizontal ? (narrow ? 16 : 26) : (narrow ? 8 : 11);
  const fulls = fullLabels(arr);
  // A horizontal bar has vertical room the caller cannot know about, so the
  // option text is re-expanded here to whatever fits in four wrapped lines and
  // the row height grows with the wrapping. Vertical bars keep the caller's
  // tighter label, because an x-axis category has no such room.
  const cats = (horizontal && !arr.noExpand) ? fulls.map(t => short(t, catWrap * 3)) : LBLS(arr);
  const catTicks = {
    font: { family: 'Inter', size: 11 }, color: TXT(), autoSkip: false, maxRotation: 0, minRotation: 0,
    callback: function (v) { return wrapTick(this.getLabelForValue(v), catWrap); },
  };
  const valTicks = { font: { family: 'Inter', size: 11 }, color: TXT(), callback: sfx ? (v => v + sfx) : undefined };
  if (horizontal) {
    const lines = Math.max(1, ...cats.map(t => wrapTick(t, catWrap).length));
    const per = Math.min(narrow ? 74 : 58, Math.max(narrow ? 44 : 32, lines * (narrow ? 13 : 15) + 14));
    fitHeight(id, arr.length, per, 46);
  }
  mk(id, {
    type: 'bar',
    data: { labels: cats, datasets: [{ data: V(arr), backgroundColor: color || S(1), borderRadius: 6, maxBarThickness: 44 }] },
    options: baseOpts({
      indexAxis: horizontal ? 'y' : 'x',
      layout: {
        padding: {
          top: horizontal ? 6 : 22,
          right: horizontal ? (withPct ? (narrow ? 58 : 74) : 46) : 8,
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: tipTitle(fullLabels(arr)),
            label: c => withPct
              ? ' ' + fmt(c.raw) + '  (' + pctTxt(c.raw) + '% of ' + fmt(total) + ')'
              : ' ' + fmt(c.raw) + sfx,
          },
        },
        datalabels: {
          display: true, anchor: 'end', align: horizontal ? 'right' : 'top', clamp: true, clip: false,
          color: TXT(), font: { family: 'Inter', size: 11, weight: '700' },
          formatter: v => withPct ? v + '  (' + pctTxt(v) + '%)' : v + sfx,
        },
      },
      scales: {
        x: {
          ticks: horizontal ? valTicks : catTicks, grid: { display: !horizontal, color: GRID() }, border: { display: false },
          ...(horizontal ? { beginAtZero: true, grace: '16%' } : {}),
        },
        y: {
          ticks: horizontal ? catTicks : valTicks, grid: { display: !horizontal, color: GRID() }, border: { display: false },
          beginAtZero: true, ...(horizontal ? {} : { grace: '16%' }),
        },
      },
    }),
  });
}

function donutChart(id, arr, colors) {
  if (!has(arr)) return noData(id);
  clearEmpty(id);
  mk(id, {
    type: 'doughnut',
    data: { labels: LBLS(arr), datasets: [{ data: V(arr), backgroundColor: colors || PALETTE(), borderWidth: 2, borderColor: SURF() }] },
    options: baseOpts({
      cutout: '58%',
      plugins: {
        // Right-hand legend leaves almost no ring on a phone-width card.
        legend: {
          position: isNarrow() ? 'bottom' : 'right',
          labels: {
            font: { family: 'Inter', size: isNarrow() ? 10 : 11 }, color: TXT(),
            boxWidth: 11, padding: isNarrow() ? 7 : 9,
          },
        },
        tooltip: {
          callbacks: {
            title: tipTitle(fullLabels(arr)),
            label: c => { const t = c.dataset.data.reduce((a, b) => a + b, 0) || 1; return ' ' + fmt(c.raw) + ' (' + Math.round(100 * c.raw / t) + '%)'; },
          },
        },
        datalabels: {
          display: true, color: '#fff', font: { family: 'Inter', size: 12, weight: '700' },
          formatter: (v, c) => { const t = c.dataset.data.reduce((a, b) => a + b, 0); const p = t ? Math.round(100 * v / t) : 0; return p >= 6 ? p + '%' : ''; },
        },
      },
    }),
  });
}

function dualLine(id, seriesA, seriesB, nameA, nameB) {
  const dates = [...new Set([...seriesA.map(x => x.date), ...seriesB.map(x => x.date)])].sort();
  if (!dates.length) return noData(id);
  clearEmpty(id);
  const pick = (s, d) => { const h = s.find(x => x.date === d); return h ? h.count : 0; };
  const a = dates.map(d => pick(seriesA, d)), b = dates.map(d => pick(seriesB, d));
  mk(id, {
    type: 'line',
    data: {
      labels: dates,
      datasets: [
        { label: nameA, data: a, borderColor: S(1), backgroundColor: 'rgba(60,84,157,.10)', fill: true, tension: .3, pointBackgroundColor: S(1), pointRadius: 3, pointHoverRadius: 6, borderWidth: 2.5, order: 1 },
        { label: nameB, data: b, borderColor: S(8), backgroundColor: 'rgba(204,133,27,.10)', fill: true, tension: .3, pointBackgroundColor: S(8), pointRadius: 3, pointHoverRadius: 6, borderWidth: 2.5, order: 2 },
      ],
    },
    options: baseOpts({
      interaction: { mode: 'index', intersect: false },
      layout: { padding: { top: 14 } },
      plugins: { legend: { position: 'top', align: 'end', labels: { font: { family: 'Inter', size: 11 }, color: TXT(), boxWidth: 13, padding: 10 } } },
      scales: {
        x: { ticks: { font: { family: 'Inter', size: 10 }, color: TXT(), maxRotation: 45, minRotation: 0 }, grid: { display: false }, border: { display: false } },
        y: { beginAtZero: true, grace: '14%', ticks: { font: { family: 'Inter', size: 11 }, color: TXT() }, grid: { color: GRID() }, border: { display: false } },
      },
    }),
  });
}

function cumulativeChart(id, arr, target, name, color) {
  if (!arr || !arr.length) return noData(id);
  clearEmpty(id);
  let run = 0;
  const cum = arr.map(x => (run += x.count));
  const ds = [{
    label: name, data: cum, borderColor: color, backgroundColor: 'rgba(23,145,66,.10)', fill: true,
    tension: .25, pointBackgroundColor: color, pointRadius: 2.5, pointHoverRadius: 6, borderWidth: 2.5,
  }];
  if (target) ds.push({ label: 'Target', data: arr.map(() => target), borderColor: S(2), borderDash: [7, 5], borderWidth: 2, pointRadius: 0, fill: false });
  mk(id, {
    type: 'line',
    data: { labels: arr.map(x => x.date), datasets: ds },
    options: baseOpts({
      layout: { padding: { top: 18 } },
      plugins: {
        legend: { position: 'top', align: 'end', labels: { font: { family: 'Inter', size: 11 }, color: TXT(), boxWidth: 13, padding: 10 } },
        datalabels: {
          display: c => c.datasetIndex === 0 && c.dataIndex === cum.length - 1, anchor: 'end', align: 'top',
          clamp: true, clip: false, color: TXT(), font: { family: 'Inter', size: 12, weight: '800' }, formatter: v => fmt(v),
        },
        tooltip: { callbacks: { label: c => ' ' + c.dataset.label + ': ' + fmt(c.raw), footer: it => target ? pctOf(it[0].raw, target) + '% of target' : '' } },
      },
      scales: {
        x: { ticks: { font: { family: 'Inter', size: 10 }, color: TXT(), maxRotation: 45 }, grid: { display: false }, border: { display: false } },
        y: { beginAtZero: true, suggestedMax: target ? target * 1.05 : undefined, ticks: { font: { family: 'Inter', size: 11 }, color: TXT(), callback: v => fmt(v) }, grid: { color: GRID() }, border: { display: false } },
      },
    }),
  });
}

function progressDonut(id, done, target, unit) {
  const el = document.getElementById(id);
  if (!el) return;
  clearEmpty(id);
  const rem = Math.max(target - done, 0);
  mk(id, {
    type: 'doughnut',
    data: { labels: ['Completed', 'Remaining'], datasets: [{ data: [done, rem], backgroundColor: [S(6), MUTED()], borderWidth: 0 }] },
    options: baseOpts({
      cutout: '72%',
      plugins: {
        legend: { position: 'bottom', labels: { font: { family: 'Inter', size: 11 }, color: TXT(), boxWidth: 12 } },
        datalabels: { display: false },
        tooltip: { callbacks: { label: c => ' ' + c.label + ': ' + fmt(c.raw) + (unit ? ' ' + unit : '') } },
      },
    }),
    plugins: [{
      id: 'center', afterDraw(c) {
        const { ctx, chartArea } = c; if (!chartArea) return;
        ctx.save();
        const x = (chartArea.left + chartArea.right) / 2, y = (chartArea.top + chartArea.bottom) / 2;
        ctx.textAlign = 'center';
        ctx.fillStyle = TXT1(); ctx.font = '800 26px Inter';
        ctx.fillText(pctOf(done, target) + '%', x, y - 2);
        ctx.fillStyle = CV('--text-3') || '#94a3b8'; ctx.font = '600 11px Inter';
        ctx.fillText(fmt(done) + ' / ' + fmt(target), x, y + 18);
        ctx.restore();
      },
    }],
  });
}

function groupedBar(id, labels, series, suffix, maxY) {
  if (!labels.length) return noData(id);
  clearEmpty(id);
  const sfx = suffix === undefined ? '%' : suffix;
  mk(id, {
    type: 'bar',
    data: { labels, datasets: series.map(s => ({ label: s.name, data: s.data, backgroundColor: s.color, borderRadius: 5, maxBarThickness: 36 })) },
    options: baseOpts({
      layout: { padding: { top: 20 } },
      plugins: {
        legend: { position: 'top', labels: { font: { family: 'Inter', size: 11 }, color: TXT(), boxWidth: 13 } },
        tooltip: { callbacks: { label: c => ' ' + c.dataset.label + ': ' + c.raw + sfx } },
        datalabels: { display: ctx => ctx.dataset.data.length <= 12, anchor: 'end', align: 'top', clamp: true, clip: false, color: TXT(), font: { family: 'Inter', size: 10, weight: '700' }, formatter: v => v == null ? '' : v + sfx },
      },
      scales: {
        x: { ticks: { font: { family: 'Inter', size: isNarrow() ? 9.5 : 11 }, color: TXT(), autoSkip: false, maxRotation: isNarrow() ? 45 : 0, minRotation: 0, callback: function (v) { return wrapTick(this.getLabelForValue(v), isNarrow() ? 9 : 14); } }, grid: { display: false }, border: { display: false } },
        y: { beginAtZero: true, max: maxY === undefined ? (sfx === '%' ? 100 : undefined) : maxY, grace: sfx === '%' ? undefined : '14%', ticks: { font: { family: 'Inter', size: 11 }, color: TXT(), callback: v => v + sfx }, grid: { color: GRID() }, border: { display: false } },
      },
    }),
  });
}

/* 100% stacked bars — for composition questions where the total is not the point. */
function stacked100(id, labels, series, horizontal) {
  if (!labels.length) return noData(id);
  clearEmpty(id);
  if (horizontal) fitHeight(id, labels.length, 40, 70);
  mk(id, {
    type: 'bar',
    data: { labels, datasets: series.map(s => ({ label: s.name, data: s.data, backgroundColor: s.color, borderRadius: 3, maxBarThickness: 40, stack: 'a' })) },
    options: baseOpts({
      indexAxis: horizontal ? 'y' : 'x',
      plugins: {
        legend: { position: 'top', labels: { font: { family: 'Inter', size: 10.5 }, color: TXT(), boxWidth: 12, padding: 8 } },
        tooltip: { callbacks: { label: c => ' ' + c.dataset.label + ': ' + c.raw + '%' } },
        datalabels: { display: c => c.dataset.data[c.dataIndex] >= 9, color: '#fff', font: { family: 'Inter', size: 10, weight: '700' }, formatter: v => v + '%' },
      },
      scales: {
        x: { stacked: true, max: horizontal ? 100 : undefined, ticks: { font: { family: 'Inter', size: 11 }, color: TXT(), autoSkip: false, maxRotation: 0, callback: function (v) { return horizontal ? v + '%' : wrapTick(this.getLabelForValue(v), 14); } }, grid: { display: horizontal, color: GRID() }, border: { display: false } },
        y: { stacked: true, max: horizontal ? undefined : 100, ticks: { font: { family: 'Inter', size: 11 }, color: TXT(), autoSkip: false, callback: function (v) { return horizontal ? wrapTick(this.getLabelForValue(v), 22) : v + '%'; } }, grid: { display: !horizontal, color: GRID() }, border: { display: false } },
      },
    }),
  });
}

function histChart(id, bins, color, xLabel, suffix) {
  if (!bins.length) return noData(id);
  clearEmpty(id);
  mk(id, {
    type: 'bar',
    data: { labels: bins.map(b => b.label), datasets: [{ data: bins.map(b => b.value), backgroundColor: color || S(3), borderRadius: 3, categoryPercentage: .98, barPercentage: .97 }] },
    options: baseOpts({
      layout: { padding: { top: 18 } },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { title: it => xLabel + ' ' + it[0].label + (suffix || ''), label: c => ' ' + fmt(c.raw) + ' records' } },
        datalabels: { display: false },
      },
      scales: {
        x: { title: { display: true, text: xLabel, font: { family: 'Inter', size: 11, weight: '600' }, color: TXT() }, ticks: { font: { family: 'Inter', size: 10 }, color: TXT(), maxRotation: 0, autoSkip: true, maxTicksLimit: 14 }, grid: { display: false }, border: { display: false } },
        y: { beginAtZero: true, ticks: { font: { family: 'Inter', size: 11 }, color: TXT() }, grid: { color: GRID() }, border: { display: false }, title: { display: true, text: 'Records', font: { family: 'Inter', size: 11, weight: '600' }, color: TXT() } },
      },
    }),
  });
}

/* ============================ KPI BUILDERS ========================== */
function kpi(icon, value, label, delta, deltaClass, cardClass) {
  // Range values ("Rs 680–1,080") need a smaller face or they wrap mid-number.
  const long = String(value).length > 9 ? ' long' : '';
  return `<div class="kpi-card ${cardClass || ''}"><div class="kpi-icon">${icon}</div>
    <div class="kpi-value${long}">${value}</div><div class="kpi-label">${label}</div>
    ${delta ? `<div class="kpi-delta ${deltaClass || 'navy'}">${delta}</div>` : ''}</div>`;
}
function callout(cls, icon, h, p) {
  return `<div class="callout ${cls}"><div class="callout-icon">${icon}</div><div class="callout-body"><h4>${h}</h4><p>${p}</p></div></div>`;
}
function statBox(val, label, sub, color) {
  return `<div class="stat-box" style="border-top-color:${color || 'var(--rs-red)'}">
    <div class="sb-val">${val}</div><div class="sb-lab">${label}</div>${sub ? `<div class="sb-sub">${sub}</div>` : ''}</div>`;
}
function animateKpis(scope) {
  (scope || document).querySelectorAll('.kpi-value').forEach(el => {
    if (el.dataset.done === '1') return;
    const m = el.textContent.match(/^([\d,]+(?:\.\d+)?)(.*)$/);
    if (!m) return;
    const target = parseFloat(m[1].replace(/,/g, '')), rest = m[2];
    if (isNaN(target) || target === 0) return;
    const dec = (m[1].split('.')[1] || '').length;
    el.dataset.done = '1';
    // requestAnimationFrame is paused in a background tab, so a count-up
    // started there would freeze on its first frame — which is zero. Anyone
    // opening the dashboard in a background tab would come back to a wall of
    // zeroes, so skip straight to the real figure when the page is hidden.
    if (document.hidden) return;
    const t0 = performance.now(), dur = 800;
    (function tick(t) {
      const k = Math.min(((t || performance.now()) - t0) / dur, 1), eased = 1 - Math.pow(1 - k, 3);
      const v = target * eased;
      el.textContent = (dec ? v.toFixed(dec) : Math.round(v).toLocaleString()) + rest;
      if (k < 1) requestAnimationFrame(tick);
    })(performance.now());
  });
}
function setHTML(id, html) { const e = document.getElementById(id); if (e) e.innerHTML = html; }
function setTxt(id, t) { const e = document.getElementById(id); if (e) e.textContent = t; }

let toastTimer;
function toast(msg) {
  const t = document.getElementById('toast');
  if (!t) return;
  t.textContent = msg; t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 2200);
}

/* ============================ FILTER BAR ============================ */
function buildFilterUI() {
  const mkMenu = (id, items, set, labelFn) => {
    const menu = document.getElementById(id);
    menu.innerHTML = items.map(it => {
      const v = typeof it === 'string' ? it : it.v;
      const l = typeof it === 'string' ? it : it.l;
      return `<label class="fb-opt"><input type="checkbox" value="${esc(v)}" ${set.has(v) ? 'checked' : ''}><span>${esc(labelFn ? labelFn(l) : l)}</span></label>`;
    }).join('') +
      `<div class="fb-menu-foot"><button class="fb-mini" data-act="all">Select all</button><button class="fb-mini" data-act="none">Clear</button></div>`;
    menu.querySelectorAll('input').forEach(cb => {
      cb.onchange = () => { cb.checked ? set.add(cb.value) : set.delete(cb.value); renderAll(); };
    });
    menu.querySelectorAll('.fb-mini').forEach(b => {
      b.onclick = () => {
        set.clear();
        if (b.dataset.act === 'all') items.forEach(it => set.add(typeof it === 'string' ? it : it.v));
        menu.querySelectorAll('input').forEach(cb => { cb.checked = set.has(cb.value); });
        renderAll();
      };
    });
  };

  mkMenu('fb-city-menu', CITY_LIST, F.city);
  mkMenu('fb-enum-menu', ENUM_LIST, F.enum);

  // Respondent is Awareness-Survey-only and always names the real-world
  // type, grouped by which instrument asks it — never the bare word
  // "Retailer" on its own, which is also a Sampling Survey market type.
  const grpHead = (label, first) => `<div style="font-size:9.5px;font-weight:800;letter-spacing:.8px;text-transform:uppercase;color:var(--text-3);padding:${first ? '4px' : '9px'} 9px 5px${first ? '' : ';border-top:1px solid var(--border);margin-top:6px'}">${esc(label)}</div>`;
  const respOpt = (v, l) => `<label class="fb-opt"><input type="checkbox" value="${esc(v)}" ${F.resp.has(v) ? 'checked' : ''}><span>${esc(l)}</span></label>`;
  const respMenu = document.getElementById('fb-resp-menu');
  respMenu.innerHTML =
    grpHead('Awareness · Retailer survey', true) +
    respOpt('retailer', 'Retailer') + respOpt('wholesaler', 'Wholesaler') +
    grpHead('Awareness · Consumer survey', false) +
    respOpt('hh', 'Household consumer') + respOpt('biz', 'Business consumer') +
    `<div class="fb-menu-foot"><button class="fb-mini" data-act="none">Clear</button></div>`;
  respMenu.querySelectorAll('input').forEach(cb => {
    cb.onchange = () => { cb.checked ? F.resp.add(cb.value) : F.resp.delete(cb.value); renderAll(); };
  });
  respMenu.querySelector('.fb-mini').onclick = () => {
    F.resp.clear();
    respMenu.querySelectorAll('input').forEach(cb => { cb.checked = false; });
    renderAll();
  };

  // Market covers both surveys but keeps them in clearly separate groups:
  // the Sampling Survey's broad market type, then its actual named retail
  // localities and wholesale markets, then the Awareness Survey's named
  // markets — three groups, never mixed.
  const mktMenu = document.getElementById('fb-mkt-menu');
  mktMenu.innerHTML =
    grpHead('Market type · sampling', true) +
    MKT_TYPE_LIST.map(it => `<label class="fb-opt"><input type="checkbox" data-g="t" value="${esc(it.v)}"><span>${esc(it.l)}</span></label>`).join('') +
    grpHead('Named market · sampling', false) +
    MKT_LOC_LIST.map(it => `<label class="fb-opt"><input type="checkbox" data-g="l" value="${esc(it.v)}"><span>${esc(short(it.l, 34))} <span style="opacity:.55">· ${it.isW ? 'Wholesale' : 'Retail'}</span></span></label>`).join('') +
    grpHead('Named market · awareness', false) +
    MKT_NAME_LIST.map(it => `<label class="fb-opt"><input type="checkbox" data-g="n" value="${esc(it.v)}"><span>${esc(short(it.l, 34))}</span></label>`).join('') +
    `<div class="fb-menu-foot"><button class="fb-mini" data-act="none">Clear all</button></div>`;
  mktMenu.querySelectorAll('input').forEach(cb => {
    cb.onchange = () => {
      const set = cb.dataset.g === 't' ? F.mktType : cb.dataset.g === 'l' ? F.mktLoc : F.mktName;
      cb.checked ? set.add(cb.value) : set.delete(cb.value);
      renderAll();
    };
  });
  mktMenu.querySelector('.fb-mini').onclick = () => {
    F.mktType.clear(); F.mktLoc.clear(); F.mktName.clear();
    mktMenu.querySelectorAll('input').forEach(cb => { cb.checked = false; });
    renderAll();
  };

  document.querySelectorAll('.fb-btn').forEach(btn => {
    btn.onclick = e => {
      e.stopPropagation();
      const id = 'fb-' + btn.dataset.menu + '-menu';
      const menu = document.getElementById(id);
      const wasOpen = menu.classList.contains('open');
      document.querySelectorAll('.fb-menu').forEach(m => m.classList.remove('open'));
      if (!wasOpen) menu.classList.add('open');
    };
  });
  document.addEventListener('click', () => document.querySelectorAll('.fb-menu').forEach(m => m.classList.remove('open')));
  document.querySelectorAll('.fb-menu').forEach(m => m.onclick = e => e.stopPropagation());
}

function clearFilters() {
  F.city.clear(); F.enum.clear(); F.resp.clear(); F.mktType.clear(); F.mktName.clear(); F.mktLoc.clear();
  document.querySelectorAll('.fb-menu input').forEach(cb => { cb.checked = false; });
  renderAll();
  toast('Filters cleared');
}

function paintFilterButtons() {
  const set = (id, s, base) => {
    const b = document.getElementById(id);
    if (!b) return;
    b.classList.toggle('on', s.size > 0);
    const badge = s.size ? `<span class="fb-count">${s.size}</span>` : '';
    b.innerHTML = base + ' ' + badge + ' <span class="fb-caret">▼</span>';
  };
  set('fb-city-btn', F.city, '🏙️ City');
  set('fb-enum-btn', F.enum, '👤 Enumerator');
  set('fb-resp-btn', F.resp, '🧍 Respondent');
  const mb = document.getElementById('fb-mkt-btn');
  const nm = F.mktType.size + F.mktLoc.size + F.mktName.size;
  mb.classList.toggle('on', nm > 0);
  mb.innerHTML = '🏬 Market ' + (nm ? `<span class="fb-count">${nm}</span>` : '') + ' <span class="fb-caret">▼</span>';

  const s = document.getElementById('fb-status');
  s.innerHTML = filtersActive()
    ? `Showing <b>${fmt(Q.con.length)}</b> interviews · <b>${fmt(Q.sp.length)}</b> samples`
    : `All data · ${fmt(Q.con.length)} interviews · ${fmt(Q.sp.length)} samples`;
}

const esc = s => String(s === null || s === undefined ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

/* ============================ NAVIGATION ============================ */
let currentPanel = 'overview';
function showPanel(id, btn) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(b => b.classList.toggle('active', b.dataset.panel === id));
  const panel = document.getElementById('panel-' + id);
  if (panel) panel.classList.add('active');
  currentPanel = id;

  // Keep the mobile picker and the desktop tab strip showing the same thing —
  // the breakpoint can change under you when a phone is rotated.
  const sel = document.getElementById('navSelect');
  if (sel && sel.value !== id) sel.value = id;
  // Bring the active tab into view when the strip is scrolled horizontally.
  const active = document.querySelector('.nav-tab.active');
  if (active && active.scrollIntoView) {
    active.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }

  window.scrollTo({ top: 0, behavior: 'smooth' });
  drawPanel(id);
}

/* Build the phone picker from the tab strip so there is one list, not two. */
function buildNavSelect() {
  const sel = document.getElementById('navSelect');
  if (!sel) return;
  // Tabs carry a shortened caption so they fit a laptop; the picker has room
  // for the full section name, so it uses data-full where present.
  sel.innerHTML = [...document.querySelectorAll('.nav-tab')].map((b, i) =>
    `<option value="${esc(b.dataset.panel)}">${i + 1}. ${esc(b.dataset.full || b.textContent.trim())}</option>`).join('');
  sel.value = currentPanel;
  sel.onchange = () => showPanel(sel.value);
}

/* ============================== PANELS ============================== */
function drawPanel(id) {
  const fn = PANELS[id];
  if (fn) { try { fn(); } catch (e) { console.error('panel ' + id, e); } }
  animateKpis(document.getElementById('panel-' + id));
}

const PANELS = {
  overview: drawOverview, map: drawMap, sampling: drawSampling,
  retail: drawRetail, consumer: drawConsumer, adult: drawAdult,
  lead: drawLead, coverage: drawCoverage,
};

function renderAll() {
  recompute();
  paintFilterButtons();
  Object.values(CHARTS).forEach(c => { try { c.destroy(); } catch (e) {} });
  Object.keys(CHARTS).forEach(k => delete CHARTS[k]);
  document.querySelectorAll('.kpi-value').forEach(el => el.dataset.done = '');
  drawPanel(currentPanel);
}

/* ---------- 01 OVERVIEW ---------- */
function drawOverview() {
  const m = D.meta;
  const con = Q.con, ts = Q.ts, sp = Q.sp;
  const awTarget = m.aw.target, tsTarget = m.ts.target;
  const dailyAw = byDay(con, AW.f.date), dailyTs = byDay(ts, TS.f.date);
  const days = new Set([...dailyAw.map(x => x.date), ...dailyTs.map(x => x.date)]).size;
  const grams = nums(sp, SP.f.qty).reduce((a, b) => a + b, 0);

  setHTML('ov-kpis', [
    kpi('🗣️', fmt(con.length), 'Awareness Survey interviews', pctOf(con.length, awTarget) + '% of target', 'navy', 'teal'),
    kpi('🏪', fmt(ts.length), 'Sampling Survey visits', pctOf(ts.length, tsTarget) + '% of target', 'navy', 'turmeric'),
    kpi('🧪', fmt(sp.length), 'Samples banked', (grams / 1000).toFixed(1) + ' kg collected', 'green', 'purple'),
    kpi('📅', fmt(days), 'Field days', dailyAw.length ? 'Avg ' + Math.round(con.length / Math.max(1, days)) + ' interviews/day' : '', 'navy', 'navy'),
    kpi('🏙️', fmt(new Set([...con.map(r => cityOf('aw', r[AW.f.city])), ...ts.map(r => cityOf('ts', r[TS.f.sample_city]))].filter(Boolean)).size), 'Cities active', 'Across both surveys', 'navy', 'amber'),
  ].join(''));

  const topCity = (() => {
    const c = new Map();
    sp.forEach(r => { const n = cityOf('ts', r[SP.f.city]); if (n) c.set(n, (c.get(n) || 0) + 1); });
    const e = [...c.entries()].sort((a, b) => b[1] - a[1])[0];
    return e ? e[0] + ' (' + e[1] + ')' : '—';
  })();
  const rsRetail = Q.rs.filter(r => vendorTypeOf(r) !== 'wholesaler').length;
  const rsWholesale = Q.rs.filter(r => vendorTypeOf(r) === 'wholesaler').length;
  const tsWholesale = ts.filter(r => r[TS.f.market_name] === '1').length;
  const tsRetail = ts.length - tsWholesale;
  const leadKnow = leadCascade(con);
  setHTML('ov-callouts', [
    callout('teal', '🗣️', 'Awareness Survey', `${fmt(rsRetail)} retailer, ${fmt(rsWholesale)} wholesaler and ${fmt(Q.cs.length)} consumer interviews completed against a ${fmt(awTarget)} target.`),
    callout('turmeric', '🧪', 'Sampling Survey', `${fmt(tsRetail)} retail-market and ${fmt(tsWholesale)} wholesale-market vendor visits — ${fmt(sp.length)} samples banked, ${(sp.length / Math.max(1, ts.length)).toFixed(1)} per visit.`),
    callout('purple', '📍', 'Heaviest sampling city', `${topCity} samples collected — the largest single-city contribution to the laboratory batch.`),
    callout('amber', '🧠', 'Lead awareness', `${leadKnow.steps[1].pct}% of respondents know what lead is; ${leadKnow.steps[2].pct}% have heard it reaches turmeric.`),
  ].join(''));

  // Field-operations headlines. The Field Ops section was retired; these are
  // the figures worth keeping in front of the reader, without the exception
  // list that used to sit beside them.
  const awDur = nums(con, AW.f.dur).map(x => x / 60);
  const tsDur = nums(ts, TS.f.dur).map(x => x / 60);
  const sa = stats(awDur), sv = stats(tsDur);
  const enums = new Set([...con.map(r => enumOf('aw', r[AW.f.Data_Collector])),
    ...ts.map(r => enumOf('ts', r[TS.f.enum_name]))].filter(Boolean).map(x => x.trim()));
  const tsGpsOk = ts.filter(r => typeof r[TS.f.lat] === 'number').length;
  const awGpsOk = con.filter(r => typeof r[AW.f.lat] === 'number').length;
  setHTML('ov-ops', [
    statBox(fmt(enums.size), 'Active enumerators', 'Across both surveys', 'var(--series-1)'),
    statBox(sa ? fmt1(sa.med) + ' min' : '—', 'Median interview length',
      sa ? `Middle 50%: ${Math.round(sa.q1)}–${Math.round(sa.q3)} min` : '', 'var(--series-3)'),
    statBox(sv ? fmt1(sv.med) + ' min' : '—', 'Median sampling visit',
      sv ? `${fmt(sv.n)} visits timed` : '', 'var(--series-8)'),
    statBox((con.length / Math.max(1, days)).toFixed(1), 'Interviews per field day',
      `Across ${fmt(days)} active days`, 'var(--series-4)'),
    statBox(pctOf(awGpsOk, con.length) + '%', 'Awareness interviews with GPS',
      `${fmt(awGpsOk)} of ${fmt(con.length)} located`, 'var(--series-2)'),
    statBox(pctOf(tsGpsOk, ts.length) + '%', 'Sampling visits with GPS',
      `${fmt(tsGpsOk)} of ${fmt(ts.length)} located`, 'var(--series-5)'),
  ].join(''));

  drawEnumTable();

  dualLine('ovDaily', dailyAw, dailyTs, 'Awareness Survey', 'Sampling Survey');
  progressDonut('ovProgAw', con.length, awTarget, 'interviews');
  progressDonut('ovProgTs', ts.length, tsTarget, 'vendor visits');
  donutChart('ovMix', [
    { label: 'Retailer', value: rsRetail },
    { label: 'Wholesaler', value: rsWholesale },
    { label: 'Consumer', value: Q.cs.length },
  ], [S(1), S(5), S(8)]);

  barChart('ovAwCity', cityDist(con, 'aw', AW, AW.f.city), S(3), true, '', true);
  barChart('ovTsCity', cityDist(sp, 'ts', SP, SP.f.city), S(8), true, '', true);
  cumulativeChart('ovCumAw', dailyAw, null, 'Cumulative interviews', S(6));
  cumulativeChart('ovCumTs', byDay(sp, SP.f.date), null, 'Cumulative samples', S(4));

  setTxt('ov-foot', `Awareness Survey target ${fmt(awTarget)} consented interviews across ${m.aw.n_cities} cities; Sampling Survey target ${fmt(tsTarget)} vendor visits across ${m.ts.n_cities} cities. Data through ${m.data_through || '—'}. Live fieldwork data. Dashboard by Research Solutions (M&A Research Solutions LLC) | www.rs.org.pk`);
}

/* Awareness Survey respondents come in three types: the Retailer_survey
   instrument covers both Retailer and Wholesaler vendors (told apart by
   type_of_vendor), and the Consumer_survey instrument covers Consumers. */
function vendorTypeOf(awRow) {
  const i = AW.f.type_of_vendor;
  if (i === undefined) return null;
  return pretty(lab('aw', 'type_of_vendor', awRow[i])) === 'Wholesaler' ? 'wholesaler' : 'retailer';
}

/* Enumerator-wise collection counts, kept strictly apart by survey.
   Each instrument carries its own GPS question -- geo_2 on the awareness
   interview itself, "gps" on the sampling vendor visit -- so the two GPS
   figures are computed and shown separately, never blended into one
   number. Sampling visits split the same way the Market filter does —
   market_name ('1'=wholesale) says whether that specific vendor is a
   retail-market or wholesale-market vendor. */
function enumCollectionRows() {
  const map = new Map();
  const row = name => {
    if (!map.has(name)) map.set(name, { name, retailer: 0, wholesaler: 0, consumer: 0, awTotal: 0, awGps: 0, tsRetail: 0, tsWholesale: 0, visits: 0, tsGps: 0 });
    return map.get(name);
  };
  Q.con.forEach(r => {
    const name = (enumOf('aw', r[AW.f.Data_Collector]) || '').trim();
    if (!name) return;
    const rw = row(name);
    if (r[AW.f.Type_of_survey] === 'CS') { rw.consumer++; rw.awTotal++; }
    else if (r[AW.f.Type_of_survey] === 'RS') {
      if (vendorTypeOf(r) === 'wholesaler') rw.wholesaler++; else rw.retailer++;
      rw.awTotal++;
    }
    if (typeof r[AW.f.lat] === 'number') rw.awGps++;
  });
  Q.ts.forEach(r => {
    const name = (enumOf('ts', r[TS.f.enum_name]) || '').trim();
    if (!name) return;
    const rw = row(name);
    if (r[TS.f.market_name] === '1') rw.tsWholesale++; else rw.tsRetail++;
    rw.visits++;
    if (typeof r[TS.f.lat] === 'number') rw.tsGps++;
  });
  return [...map.values()].sort((a, b) => (b.awTotal + b.visits) - (a.awTotal + a.visits));
}

function drawEnumTable() {
  const body = document.getElementById('ov-enum-body');
  if (!body) return;
  const rows = enumCollectionRows();
  body.innerHTML = rows.length ? rows.map(r => `<tr>
    <td class="strong">${esc(r.name)}</td>
    <td class="num">${r.retailer ? fmt(r.retailer) : '—'}</td>
    <td class="num">${r.wholesaler ? fmt(r.wholesaler) : '—'}</td>
    <td class="num">${r.consumer ? fmt(r.consumer) : '—'}</td>
    <td class="num strong">${r.awTotal ? fmt(r.awTotal) : '—'}</td>
    <td class="num">${r.awTotal ? fmt(r.awGps) : '—'}</td>
    <td class="num">${r.awTotal ? pctOf(r.awGps, r.awTotal) + '%' : '—'}</td>
    <td class="num">${r.tsRetail ? fmt(r.tsRetail) : '—'}</td>
    <td class="num">${r.tsWholesale ? fmt(r.tsWholesale) : '—'}</td>
    <td class="num strong">${r.visits ? fmt(r.visits) : '—'}</td>
    <td class="num">${r.visits ? fmt(r.tsGps) : '—'}</td>
    <td class="num">${r.visits ? pctOf(r.tsGps, r.visits) + '%' : '—'}</td>
  </tr>`).join('') : `<tr><td colspan="12" class="tbl-empty">No field data under the current filters.</td></tr>`;
}

function cityDist(rows, ds, tbl, idx) {
  const m = new Map();
  rows.forEach(r => { const n = cityOf(ds, r[idx]); if (n) m.set(n, (m.get(n) || 0) + 1); });
  return [...m.entries()].sort((a, b) => b[1] - a[1]).map(([label, value]) => ({ label, value }));
}

/* ---------- 02 MAP ---------- */
let MAP = null, MAP_LAYER = null;
function drawMap() {
  const ts = Q.ts, sp = Q.sp;
  const pts = ts.filter(r => typeof r[TS.f.lat] === 'number' && typeof r[TS.f.lon] === 'number');
  const acc = nums(ts, TS.f.acc);

  setHTML('map-stats', [
    statBox(fmt(pts.length), 'GPS-located vendors', `${pctOf(pts.length, ts.length)}% of visits`, 'var(--turmeric)'),
    statBox(fmt(new Set(ts.map(r => cityOf('ts', r[TS.f.sample_city])).filter(Boolean)).size), 'Cities covered', 'Sampling frame', 'var(--series-1)'),
    statBox(fmt(sp.length), 'Samples mapped', 'Linked to a GPS point', 'var(--series-6)'),
    statBox(acc.length ? Math.round(median(acc)) + ' m' : '—', 'Median GPS accuracy', 'Device-reported', 'var(--series-3)'),
    statBox(fmt(new Set(ts.map(r => r[TS.f.wholesale_market] || r[TS.f.locality_retail]).filter(Boolean)).size), 'Distinct localities', 'Markets & localities', 'var(--series-5)'),
  ].join(''));

  const el = document.getElementById('map');
  if (!el) return;
  if (!MAP) {
    MAP = L.map('map', { scrollWheelZoom: false });
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 18, attribution: '© OpenStreetMap contributors',
    }).addTo(MAP);
    MAP.on('click', () => MAP.scrollWheelZoom.enable());
    MAP.on('mouseout', () => MAP.scrollWheelZoom.disable());
    const legend = L.control({ position: 'bottomright' });
    legend.onAdd = () => {
      const d = L.DomUtil.create('div', 'map-legend');
      d.innerHTML = `<b>Market type</b>
        <i style="background:${S(1)}"></i>Wholesale market<br>
        <i style="background:${S(8)}"></i>Retail market`;
      return d;
    };
    legend.addTo(MAP);
  }
  if (MAP_LAYER) { MAP.removeLayer(MAP_LAYER); MAP_LAYER = null; }

  const spByVendor = new Map();
  sp.forEach(r => {
    const k = r[SP.f.vkey];
    if (!spByVendor.has(k)) spByVendor.set(k, []);
    spByVendor.get(k).push(r);
  });

  const group = (typeof L.markerClusterGroup === 'function')
    ? L.markerClusterGroup({ maxClusterRadius: 46, spiderfyOnMaxZoom: true, showCoverageOnHover: false })
    : L.layerGroup();

  pts.forEach(r => {
    const isWhole = r[TS.f.market_name] === '1';
    const col = isWhole ? S(1) : S(8);
    const mine = spByVendor.get(r[TS.f.key]) || [];
    const prices = mine.map(x => x[SP.f.price_per_kg]).filter(Boolean);
    const types = [...new Set(mine.map(x => pretty(lab('ts', 'collected_sample_type', x[SP.f.type]))))].filter(Boolean);
    const loc = r[TS.f.market_name] === '1'
      ? lab('ts', 'wholesale_market', r[TS.f.wholesale_market])
      : lab('ts', 'locality_retail', r[TS.f.locality_retail]);
    const mk2 = L.circleMarker([r[TS.f.lat], r[TS.f.lon]], {
      radius: 6, color: col, weight: 1.5, fillColor: col, fillOpacity: .65,
    });
    mk2.bindPopup(
      `<b>${esc(r[TS.f.vendor_name] || 'Vendor ' + r[TS.f.vendor_id])}</b><br>
       Vendor ID: ${esc(r[TS.f.vendor_id] || '—')}<br>
       ${esc(cityOf('ts', r[TS.f.sample_city]) || '—')} · ${esc(pretty(lab('ts', 'market_name', r[TS.f.market_name])))}<br>
       ${loc ? esc(short(loc, 44)) + '<br>' : ''}
       Shop size: ${esc(pretty(lab('ts', 'size_of_shop', r[TS.f.size_of_shop])))}<br>
       <b>${mine.length}</b> sample${mine.length === 1 ? '' : 's'}${types.length ? ' · ' + esc(types.map(t => short(t, 20)).join(', ')) : ''}<br>
       ${prices.length ? 'Median Rs ' + fmt(Math.round(median(prices))) + '/kg<br>' : ''}
       <span style="color:#94a3b8">${esc(r[TS.f.date] || '')} · ${esc((enumOf('ts', r[TS.f.enum_name]) || '').trim())}</span>`
    );
    group.addLayer(mk2);
  });

  MAP_LAYER = group;
  MAP.addLayer(group);
  if (pts.length) {
    MAP.fitBounds(L.latLngBounds(pts.map(r => [r[TS.f.lat], r[TS.f.lon]])), { padding: [30, 30], maxZoom: 12 });
  } else {
    MAP.setView([30.4, 69.4], 5);
  }
  setTimeout(() => MAP.invalidateSize(), 60);

  setTxt('map-note', `${fmt(pts.length)} of ${fmt(ts.length)} vendor visits carry a usable GPS fix. Click a cluster to zoom in, or a single point for the vendor record. Scroll-zoom activates after you click the map.`);

  donutChart('mapMkt', distOf(ts, 'ts', TS, 'market_name'), [S(1), S(8)]);

  const cities = [...new Set(sp.map(r => cityOf('ts', r[SP.f.city])).filter(Boolean))];
  const cityTotals = cities.map(c => ({
    c, n: sp.filter(r => cityOf('ts', r[SP.f.city]) === c).length,
  })).sort((a, b) => b.n - a.n);
  const labels = cityTotals.map(x => x.c);
  const wh = labels.map(c => sp.filter(r => cityOf('ts', r[SP.f.city]) === c && r[SP.f.market] === '1').length);
  const rt = labels.map(c => sp.filter(r => cityOf('ts', r[SP.f.city]) === c && r[SP.f.market] === '2').length);
  if (!labels.length) { noData('mapCityStack'); return; }
  clearEmpty('mapCityStack');
  fitHeight('mapCityStack', labels.length, 30, 60);
  mk('mapCityStack', {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Wholesale', data: wh, backgroundColor: S(1), borderRadius: 3, maxBarThickness: 22, stack: 's' },
        { label: 'Retail', data: rt, backgroundColor: S(8), borderRadius: 3, maxBarThickness: 22, stack: 's' },
      ],
    },
    options: baseOpts({
      indexAxis: 'y',
      layout: { padding: { right: 40 } },
      plugins: {
        legend: { position: 'top', align: 'end', labels: { font: { family: 'Inter', size: 11 }, color: TXT(), boxWidth: 12 } },
        datalabels: {
          display: c => c.datasetIndex === 1, anchor: 'end', align: 'right', clamp: true, clip: false,
          color: TXT(), font: { family: 'Inter', size: 11, weight: '700' },
          formatter: (v, c) => wh[c.dataIndex] + rt[c.dataIndex],
        },
      },
      scales: {
        x: { stacked: true, beginAtZero: true, grace: '4%', ticks: { font: { family: 'Inter', size: 11 }, color: TXT() }, grid: { color: GRID() }, border: { display: false } },
        y: { stacked: true, ticks: { font: { family: 'Inter', size: 11.5, weight: '600' }, color: TXT(), autoSkip: false }, grid: { display: false }, border: { display: false } },
      },
    }),
  });
}

/* ---------- 03 SAMPLING ---------- */
function drawSampling() {
  const ts = Q.ts, sp = Q.sp;
  const grams = nums(sp, SP.f.qty).reduce((a, b) => a + b, 0);
  const perVendor = ts.map(r => r[TS.f.n_samples] || 0);
  const tsWholesale = ts.filter(r => r[TS.f.market_name] === '1').length;
  const tsRetail = ts.length - tsWholesale;

  setHTML('sm-kpis', [
    kpi('🏪', fmt(ts.length), 'Vendor visits', `${fmt(tsRetail)} retail-market · ${fmt(tsWholesale)} wholesale-market`, 'navy', 'turmeric'),
    kpi('🧪', fmt(sp.length), 'Samples banked', `${(sp.length / Math.max(1, ts.length)).toFixed(1)} per vendor`, 'navy', 'purple'),
    kpi('⚖️', (grams / 1000).toFixed(1), 'Kilograms collected', `Median ${fmt(median(nums(sp, SP.f.qty)))} g per sample`, 'navy', 'teal'),
    kpi('🎨', fmt(new Set(sp.map(r => r[SP.f.type]).filter(Boolean)).size), 'Product types sampled', 'Of 4 in the protocol', 'navy', 'amber'),
    kpi('✨', shareIn(sp, SP, 'basis', ['2', '3']).pct + '%', 'Chosen for brightness', 'Highest lead-risk selection', 'down', 'navy'),
    kpi('📦', fmt(perVendor.length ? Math.max(...perVendor) : 0), 'Most from one vendor', 'Single-visit maximum', 'navy', 'green'),
  ].join(''));

  const avail = multiOf(ts, 'ts', TS, 'shop_sample_type', { pct: true, sort: false, maxLabel: 34 });
  const coll = multiOf(ts, 'ts', TS, 'collected_sample_type', { pct: true, sort: false, maxLabel: 34 });
  barChart('smAvail', avail.rows, S(3), true, '%');
  barChart('smColl', coll.rows, S(8), true, '%');

  barChart('smType', typeDist(sp), S(4), false, '', true);
  donutChart('smBasis', distOf(sp, 'ts', SP, 'basis', {}).map(x => ({
    ...x, full: sampleTypeLabel('sample_type_2', x.code),
    label: short(sampleTypeLabel('sample_type_2', x.code), 34),
  })));
  donutChart('smSize', distOf(ts, 'ts', TS, 'size_of_shop'), [S(3), S(1), S(4)]);

  // Whole dried roots on open display vs produced on request. Asked only where
  // the shop stocks whole roots, so the base is the vendors who answered it.
  // The instrument wording ("Not displayed – brought from back/inside upon
  // asking") is too long for a donut legend, so the ring uses a compact form
  // and the tooltip keeps the option exactly as it was asked.
  const ROOT_SHORT = { '1': 'Openly displayed', '2': 'Brought out on request' };
  const rootDisp = distOf(ts, 'ts', TS, 'whole_root_display', {})
    .map(x => ({ ...x, label: ROOT_SHORT[x.code] || short(x.label, 34) }));
  const rootBase = nonNull(ts, TS, 'whole_root_display');
  donutChart('smRootDisplay', rootDisp, [S(3), S(6)]);
  const openShare = shareIn(ts, TS, 'whole_root_display', ['1']);
  setTxt('smRoot-desc', rootBase
    ? `Enumerator observation, base ${fmt(rootBase)} vendors stocking whole dried roots — ${openShare.pct}% had them on open display`
    : 'Enumerator observation: whether whole dried roots were on open display or produced from the back on request');

  // basis composition within each product type
  const types = ord('ts', 'collected_sample_type');
  const bases = ord('ts', 'sample_type_2');
  const tl = types.filter(t => sp.some(r => r[SP.f.type] === t));
  if (tl.length) {
    const series = bases.map((b, i) => ({
      name: short(sampleTypeLabel('sample_type_2', b), 34),
      color: S((i % 8) + 1),
      data: tl.map(t => {
        const inT = sp.filter(r => r[SP.f.type] === t);
        return inT.length ? Math.round(100 * inT.filter(r => r[SP.f.basis] === b).length / inT.length) : 0;
      }),
    }));
    stacked100('smBasisType', tl.map(t => short(lab('ts', 'collected_sample_type', t), 30)), series);
  } else noData('smBasisType');

  perVendorChart('smPerVendor', perVendor);

  setTxt('sm-foot', `Sampling protocol: one to three samples per turmeric product type stocked, with the label written as vendor ID _ product type _ selection basis. ${fmt(sp.length)} samples from ${fmt(ts.length)} vendor visits currently banked, totalling ${(grams / 1000).toFixed(1)} kg. Where a shop stocked whole dried roots the enumerator also recorded whether those roots were on open display or produced from the back on request${rootBase ? ` — ${openShare.pct}% openly displayed across ${fmt(rootBase)} such vendors` : ''}. Selection basis matters analytically — samples chosen because they looked bright or shiny are the ones the laboratory results should be read against first.`);
}

/* How many vendor visits produced 0 samples, 1 sample, 2 samples … Each bar is
   a count of *visits*, not of samples, so the bars add up to the number of
   vendor visits in the current filter — a bare "0 / 1 / 2" axis read as a
   sample count, so the categories and both axes are named explicitly. */
function perVendorChart(id, perVendor) {
  if (!perVendor.length) return noData(id);
  clearEmpty(id);
  const cnt = new Map();
  perVendor.forEach(n => cnt.set(n, (cnt.get(n) || 0) + 1));
  const keys = [...cnt.keys()].sort((a, b) => a - b);
  const visits = perVendor.length;
  const banked = perVendor.reduce((a, b) => a + b, 0);
  const labels = keys.map(k => k === 0 ? 'None' : k + (k === 1 ? ' sample' : ' samples'));
  const data = keys.map(k => cnt.get(k));
  const pct = v => Math.round(1000 * v / visits) / 10;
  const narrow = isNarrow();

  mk(id, {
    type: 'bar',
    data: { labels, datasets: [{ data, backgroundColor: S(5), borderRadius: 6, maxBarThickness: 54 }] },
    options: baseOpts({
      layout: { padding: { top: 24 } },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: it => it[0].label + ' collected at the visit',
            label: c => ` ${fmt(c.raw)} vendor visits (${pct(c.raw)}% of ${fmt(visits)})`,
          },
        },
        datalabels: {
          display: true, anchor: 'end', align: 'top', clamp: true, clip: false,
          color: TXT(), font: { family: 'Inter', size: 11, weight: '700' },
          formatter: v => narrow ? String(v) : `${fmt(v)}  (${pct(v)}%)`,
        },
      },
      scales: {
        x: {
          title: {
            display: true, text: 'Samples banked at a single vendor visit',
            font: { family: 'Inter', size: 11, weight: '600' }, color: TXT(),
          },
          ticks: {
            font: { family: 'Inter', size: 11 }, color: TXT(), autoSkip: false, maxRotation: 0,
            callback: function (v) { return wrapTick(this.getLabelForValue(v), narrow ? 8 : 12); },
          },
          grid: { display: false }, border: { display: false },
        },
        y: {
          beginAtZero: true, grace: '16%',
          title: {
            display: true, text: 'Vendor visits',
            font: { family: 'Inter', size: 11, weight: '600' }, color: TXT(),
          },
          ticks: { font: { family: 'Inter', size: 11 }, color: TXT() },
          grid: { color: GRID() }, border: { display: false },
        },
      },
    }),
  });
  setTxt('smPerVendor-note',
    `${fmt(visits)} vendor visits produced ${fmt(banked)} samples — an average of ${(banked / Math.max(1, visits)).toFixed(1)} per visit, `
    + `most often ${labels[data.indexOf(Math.max(...data))].toLowerCase()}. Bars count visits, not samples, so they add to ${fmt(visits)}.`);
}

function sampleTypeLabel(field, code) {
  return lab('ts', field, code) || code;
}
function typeDist(sp) {
  const m = new Map();
  sp.forEach(r => { const t = r[SP.f.type]; if (t) m.set(t, (m.get(t) || 0) + 1); });
  return ord('ts', 'collected_sample_type').filter(t => m.has(t))
    .map(t => ({ code: t, label: short(lab('ts', 'collected_sample_type', t), 30), value: m.get(t) }));
}

/* ---------- 04 RETAILER ---------- */
function drawRetail() {
  const rs = Q.rs;
  if (!rs.length) {
    ['rtSold', 'rtTopBottom', 'rtMix', 'rtVendor', 'rtCust', 'rtSes', 'rtSource', 'rtChoose', 'rtStable', 'rtComfort']
      .forEach(id => noData(id, 'No retailer interviews match the current filters.'));
    setHTML('rt-kpis', '');
    setTxt('rt-foot', 'No retailer interviews in the current selection.');
    return;
  }
  const mixFields = ['Fresh_Turmeric_Roots', 'Dried_Turmeric_Roots', 'Loose_Turmeric_Powder',
    'Packaged_Branded_Turmeric_Powder', 'Packaged_Unbranded_Turmeric_Powder'];
  const mixLabels = ['Fresh roots', 'Dried roots', 'Loose powder', 'Packaged branded', 'Packaged unbranded'];
  const mixMeans = mixFields.map(f => {
    const v = nums(rs, AW.f[f]);
    return v.length ? Math.round(v.reduce((a, b) => a + b, 0) / v.length) : 0;
  });

  setHTML('rt-kpis', [
    kpi('🏪', fmt(rs.length), 'Retailer interviews', `${shareIn(rs, AW, 'type_of_vendor', ['1']).pct}% wholesalers`, 'navy', 'teal'),
    kpi('🌿', fmt1(avgMultiCount(rs, AW.f.Q2)), 'Types sold per vendor', 'Average product breadth', 'navy', 'green'),
    kpi('🤝', shareIn(rs, AW, 'Q17', ['1']).pct + '%', 'Use a regular supplier', 'Same person supplies', 'navy', 'purple'),
    kpi('⭐', shareIn(rs, AW, 'Q18', ['1']).pct + '%', 'Choose on quality', 'Named product quality', 'up', 'amber'),
    kpi('🏠', shareIn(rs, AW, 'Q10', ['1', '3']).pct + '%', 'Serve households', 'Directly or partly', 'navy', 'navy'),
    kpi('😊', shareIn(rs, AW, 'Q36', ['4']).pct + '%', 'Fully comfortable', 'Enumerator assessment', 'up', 'turmeric'),
  ].join(''));

  barChart('rtSold', multiOf(rs, 'aw', AW, 'Q2', { pct: true, sort: false, maxLabel: 34 }).rows, S(8), true, '%');

  const t3 = distOf(rs, 'aw', AW, 'Q3', { maxLabel: 26 });
  const t3b = distOf(rs, 'aw', AW, 'Q3_b', { maxLabel: 26 });
  const allT = [...new Set([...t3.map(x => x.code), ...t3b.map(x => x.code)])];
  const gl = allT.map(c => short(lab('aw', 'Q3', c), 36));
  groupedBar('rtTopBottom', gl, [
    { name: 'Highest selling', color: S(6), data: allT.map(c => (t3.find(x => x.code === c) || { value: 0 }).value) },
    { name: 'Lowest selling', color: S(2), data: allT.map(c => (t3b.find(x => x.code === c) || { value: 0 }).value) },
  ], '', undefined);

  barChart('rtMix', mixLabels.map((l, i) => ({ label: l, value: mixMeans[i] })), S(1), false, '%');
  donutChart('rtVendor', distOf(rs, 'aw', AW, 'type_of_vendor'), [S(1), S(8)]);
  donutChart('rtCust', distOf(rs, 'aw', AW, 'Q10'));
  donutChart('rtSes', distOf(rs, 'aw', AW, 'Q12'), [S(5), S(3), S(4)]);
  barChart('rtSource', distOf(rs, 'aw', AW, 'Q16', { sort: true, maxLabel: 30 }), S(3), true, '', true);
  barChart('rtChoose', multiOf(rs, 'aw', AW, 'Q18', { pct: true, maxLabel: 34 }).rows, S(4), true, '%');
  donutChart('rtStable', distOf(rs, 'aw', AW, 'Q17'), [S(6), S(2)]);
  barChart('rtComfort', distOf(rs, 'aw', AW, 'Q36', {}), S(6), true, '', true);

  setTxt('rt-foot', `Base: ${fmt(rs.length)} completed retailer interviews. Sales mix is the mean reported share of turnover per product type among retailers who gave a split, so the five bars approximately sum to 100%. Multiple-response questions use the number of retailers asked as the base, so percentages sum above 100.`);
}
function avgMultiCount(rows, idx) {
  const c = rows.map(r => Array.isArray(r[idx]) ? r[idx].length : 0).filter(x => x);
  return c.length ? c.reduce((a, b) => a + b, 0) / c.length : 0;
}

/* ---------- 05 CONSUMER ---------- */
function drawConsumer() {
  const cs = Q.cs;
  if (!cs.length) {
    ['csGender', 'csAge', 'csSes', 'csOcc', 'csSource', 'csFreq', 'csPurpose', 'csQty', 'csDecide']
      .forEach(id => noData(id, 'No consumer interviews match the current filters.'));
    setHTML('cs-kpis', '');
    setTxt('cs-foot', 'No consumer interviews in the current selection.');
    return;
  }
  const hh = nums(cs, AW.f.Q_18);
  setHTML('cs-kpis', [
    kpi('🛒', fmt(cs.length), 'Consumer interviews', `${shareIn(cs, AW, 'Q_8', ['1']).pct}% are the buyer`, 'navy', 'teal'),
    kpi('👩', shareIn(cs, AW, 'Q_6', ['female']).pct + '%', 'Female respondents', 'Enumerator-observed', 'navy', 'purple'),
    kpi('🍲', shareIn(cs, AW, 'Q_9', ['5', '6']).pct + '%', 'Cook with turmeric daily', 'Most days or more', 'up', 'turmeric'),
    kpi('🏡', hh.length ? fmt1(hh.reduce((a, b) => a + b, 0) / hh.length) : '—', 'People fed per household', `${fmt(hh.length)} reported`, 'navy', 'green'),
    kpi('📦', shareIn(cs, AW, 'Q_10', ['3']).pct + '%', 'Buy packaged powder', 'Lowest-risk purchase form', 'up', 'navy'),
    kpi('🌾', shareIn(cs, AW, 'Q_10', ['4', '5', '6']).pct + '%', 'Buy whole roots', 'Higher adulteration exposure', 'down', 'amber'),
  ].join(''));

  donutChart('csGender', distOf(cs, 'aw', AW, 'Q_6'), [S(1), S(7), S(4)]);
  barChart('csAge', distOf(cs, 'aw', AW, 'Q_7', { maxLabel: 14 }), S(3), false, '', true);
  barChart('csSes', distOf(cs, 'aw', AW, 'Q_2', {}), S(4), true, '', true);
  barChart('csOcc', distOf(cs, 'aw', AW, 'Q_3', { sort: true, maxLabel: 36 }), S(5), true, '', true);
  barChart('csSource', multiOf(cs, 'aw', AW, 'Q_10', { pct: true, maxLabel: 40 }).rows, S(8), true, '%');
  barChart('csFreq', distOf(cs, 'aw', AW, 'Q_9', { maxLabel: 30 }), S(6), true, '', true);
  donutChart('csPurpose', distOf(cs, 'aw', AW, 'Q_1', { maxLabel: 26 }));

  // normalise reported monthly quantity to grams where a unit was captured
  const qg = [];
  cs.forEach(r => {
    const v = r[AW.f.Q_16], u = r[AW.f.Q_16_unit];
    if (typeof v !== 'number') return;
    if (u === 'Kg') qg.push(v * 1000);
    else if (u === 'gram') qg.push(v);
  });
  histChart('csQty', histogram(qg.filter(x => x <= 3000), 250), S(4), 'Grams per month', ' g');
  barChart('csDecide', multiOf(cs, 'aw', AW, 'Q_26', { pct: true, maxLabel: 34 }).rows, S(1), true, '%');

  setTxt('cs-foot', `Base: ${fmt(cs.length)} completed consumer interviews. Monthly quantity is normalised to grams for respondents who reported in kilograms or grams; those answering in roots or packets are excluded from that chart only. Purchase form is the operative exposure variable — loose powder and whole roots carry materially different adulteration risk from sealed branded packs.`);
}

/* ---------- 06 ADULTERATION ---------- */
const AD_ROWS = [
  { label: 'Turmeric overall', rs: 'Q19', cs: 'Q_30' },
  { label: 'Packaged ground turmeric', rs: 'Q20', cs: 'Q_31' },
  { label: 'Loose ground turmeric', rs: 'Q21', cs: 'Q_32' },
  { label: 'Dried whole roots', rs: 'Q22', cs: 'Q_33' },
  { label: 'Fresh whole roots', rs: 'Q23', cs: 'Q_34' },
];
const COMMON_CODES = ['4', '5'];

function scaleDist(rows, field) {
  const i = AW.f[field];
  if (i === undefined) return null;
  const counts = new Map();
  let base = 0;
  rows.forEach(r => {
    const v = r[i];
    if (!v) return;
    const l = pretty(lab('aw', field, v));
    if (NONSUBSTANTIVE.test(l)) return;
    base++;
    counts.set(v, (counts.get(v) || 0) + 1);
  });
  return { counts, base };
}

function drawAdult() {
  const rs = Q.rs, cs = Q.cs, con = Q.con;
  const overall = combinedShare(rs, 'Q19', cs, 'Q_30', COMMON_CODES);
  const packaged = combinedShare(rs, 'Q20', cs, 'Q_31', COMMON_CODES);
  const loose = combinedShare(rs, 'Q21', cs, 'Q_32', COMMON_CODES);
  const dried = combinedShare(rs, 'Q22', cs, 'Q_33', COMMON_CODES);

  const leadNamed = namedLead(con);
  setHTML('ad-kpis', [
    kpi('⚠️', overall.pct + '%', 'Say adulteration is common', `Base ${fmt(overall.base)} respondents`, 'down', 'amber'),
    kpi('🧂', loose.pct + '%', 'Loose ground seen as risky', 'Common or very common', 'down', 'turmeric'),
    kpi('📦', packaged.pct + '%', 'Packaged ground seen as risky', 'Common or very common', 'neutral', 'teal'),
    kpi('🌾', dried.pct + '%', 'Dried roots seen as risky', 'Common or very common', 'neutral', 'purple'),
    kpi('☠️', leadNamed.pct + '%', 'Name lead as an adulterant', `${fmt(leadNamed.n)} respondents, unprompted`, 'down', 'navy'),
    kpi('🔬', shareIn(cs, AW, 'Q_28', ['1']).pct + '%', 'Consumers believe adulteration exists', `Base ${fmt(shareIn(cs, AW, 'Q_28', ['1']).base)}`, 'navy', 'green'),
  ].join(''));

  setHTML('ad-insight',
    `<strong>Read across the matrix, not down it.</strong> Perceived risk is concentrated in <strong>loose ground turmeric</strong> (${loose.pct}% call it common or very common) while sealed branded packs are trusted far more. That gap is the behavioural lever the campaign has to work with — and it is only useful if the laboratory results actually support it. ${leadNamed.pct}% of respondents named lead or lead chromate specifically, without being prompted.`);

  buildMatrix(rs, cs);
  barChart('adRs', AD_ROWS.map(r => ({ label: short(r.label, 26), value: shareIn(rs, AW, r.rs, COMMON_CODES).pct })), S(1), true, '%');
  barChart('adCs', AD_ROWS.map(r => ({ label: short(r.label, 26), value: shareIn(cs, AW, r.cs, COMMON_CODES).pct })), S(8), true, '%');

  barChart('adFood', mergeMulti([[rs, 'Q25'], [rs, 'Q28'], [cs, 'Q_35'], [cs, 'Q_37']], 34), S(4), true, '', true);
  barChart('adNonFood', mergeMulti([[rs, 'Q26'], [rs, 'Q29'], [cs, 'Q_36'], [cs, 'Q_38']], 34), S(2), true, '', true);
  barChart('adSource', mergeMulti([[rs, 'Q24'], [cs, 'Q_29']], 38), S(3), true, '', true);

  const cities = [...new Set(con.map(r => cityOf('aw', r[AW.f.city])).filter(Boolean))].sort();
  barChart('adCity', cities.map(c => {
    const sub = con.filter(r => cityOf('aw', r[AW.f.city]) === c);
    return { label: c, value: combinedShare(sub.filter(r => r[AW.f.Type_of_survey] === 'RS'), 'Q19', sub.filter(r => r[AW.f.Type_of_survey] === 'CS'), 'Q_30', COMMON_CODES).pct };
  }).sort((a, b) => b.value - a.value), S(5), true, '%');

  setTxt('ad-foot', `Both instruments use the same five-point commonality scale, so retailer and consumer responses are directly comparable. "Don't know" and "refused" are excluded from every base on this page. Matrix rows are percentages of the respondents who answered that specific product question, so row bases differ — a respondent who said adulteration is rare overall was not asked the product-level follow-ups.`);
}

function combinedShare(rsRows, rsField, csRows, csField, codes) {
  const a = shareIn(rsRows, AW, rsField, codes), b = shareIn(csRows, AW, csField, codes);
  const base = a.base + b.base, n = a.n + b.n;
  return { pct: base ? Math.round(1000 * n / base) / 10 : 0, n, base };
}
function namedLead(con) {
  let n = 0;
  con.forEach(r => {
    const fields = ['Q26', 'Q29', 'Q_36', 'Q_38'];
    const hit = fields.some(f => { const v = r[AW.f[f]]; return Array.isArray(v) && v.includes('2'); });
    if (hit) n++;
  });
  return { n, pct: con.length ? Math.round(1000 * n / con.length) / 10 : 0 };
}
function mergeMulti(pairs, maxLabel) {
  const counts = new Map();
  pairs.forEach(([rows, field]) => {
    const i = AW.f[field];
    if (i === undefined) return;
    rows.forEach(r => {
      const v = r[i];
      if (!Array.isArray(v)) return;
      v.forEach(c => {
        const l = pretty(lab('aw', field, c));
        if (NONSUBSTANTIVE.test(l)) return;
        counts.set(l, (counts.get(l) || 0) + 1);
      });
    });
  });
  return [...counts.entries()].sort((a, b) => b[1] - a[1])
    .map(([label, value]) => ({ full: label, label: short(label, maxLabel || 34), value }));
}

function buildMatrix(rs, cs) {
  const tbl = document.getElementById('ad-matrix');
  if (!tbl) return;
  const scaleField = 'Q19';
  const codes = ord('aw', scaleField).filter(c => !NONSUBSTANTIVE.test(pretty(lab('aw', scaleField, c))));
  const seq = SEQ();

  const rowsData = AD_ROWS.map(def => {
    const a = scaleDist(rs, def.rs), b = scaleDist(cs, def.cs);
    const counts = new Map();
    let base = 0;
    [a, b].forEach(x => {
      if (!x) return;
      base += x.base;
      x.counts.forEach((v, k) => counts.set(k, (counts.get(k) || 0) + v));
    });
    return { label: def.label, base, pct: codes.map(c => base ? Math.round(100 * (counts.get(c) || 0) / base) : 0), raw: codes.map(c => counts.get(c) || 0) };
  });

  const maxPct = Math.max(1, ...rowsData.flatMap(r => r.pct));
  const colorFor = p => {
    if (!p) return 'var(--track)';
    const k = Math.min(4, Math.floor((p / maxPct) * 5));
    return seq[k];
  };
  const textFor = p => (p / maxPct) > 0.55 ? '#fff' : 'var(--text)';

  tbl.innerHTML =
    `<thead><tr><th class="rowhead">Product type</th>${codes.map(c => `<th title="${esc(pretty(lab('aw', scaleField, c)))}">${esc(short(lab('aw', scaleField, c), 26))}</th>`).join('')}<th>Base</th></tr></thead>` +
    `<tbody>${rowsData.map(r => `<tr><td class="rowhead">${esc(r.label)}</td>${r.pct.map((p, i) =>
      `<td class="cell" style="background:${colorFor(p)};color:${textFor(p)}" title="${esc(r.label)} — ${esc(pretty(lab('aw', scaleField, codes[i])))}: ${r.raw[i]} of ${r.base}">${p}%<small>${r.raw[i]}</small></td>`).join('')}<td class="cell" style="background:var(--surface-2);color:var(--text-2)">${fmt(r.base)}</td></tr>`).join('')}</tbody>`;

  setHTML('ad-legend', seq.map(c => `<i style="background:${c}"></i>`).join(''));
  setTxt('ad-legend-txt', `0% to ${maxPct}% of the row base`);
}

/* ---------- 07 LEAD ---------- */
function leadCascade(con) {
  const isRS = r => r[AW.f.Type_of_survey] === 'RS';
  const any = (r, pairs) => pairs.some(([f, test]) => test(r[AW.f[f]]));
  const eq = v => x => x === v;
  const inSet = arr => x => arr.includes(x);
  const notNull = () => x => x !== null && x !== undefined && x !== '' && (!Array.isArray(x) || x.length > 0);

  const base = con.length;
  const knows = con.filter(r => isRS(r) ? r[AW.f.Q33] === '1' : r[AW.f.Q_57a] === '1');
  const inTurmeric = con.filter(r => isRS(r) ? notNull()(r[AW.f.Q33_ii]) : r[AW.f.Q_57_i] === '1');
  const concerned = con.filter(r => isRS(r) ? inSet(['3', '4'])(r[AW.f.Q34]) : inSet(['3', '4'])(r[AW.f.Q_57b]));
  const knowsHow = con.filter(r => isRS(r) ? notNull()(r[AW.f.Q35]) : r[AW.f.Q_57c] === '1');
  const acted = con.filter(r => isRS(r) ? r[AW.f.Q35_i] === '1' : notNull()(r[AW.f.Q_57c_i]));

  const step = (label, sub, arr) => ({ label, sub, n: arr.length, pct: base ? Math.round(1000 * arr.length / base) / 10 : 0 });
  return {
    base,
    steps: [
      step('All consented respondents', 'Retailer and consumer instruments combined', con),
      step('Know what lead (sisa) is', 'Answered yes when asked directly', knows),
      step('Have heard lead reaches turmeric', 'Aware of the specific adulteration route', inTurmeric),
      step('Concerned about children', 'Moderate or high level of concern', concerned),
      step('Know how to avoid exposure', 'Could name a protective step', knowsHow),
      step('Have acted on it', 'Took a step in the recent period', acted),
    ],
  };
}

function drawLead() {
  const con = Q.con, rs = Q.rs, cs = Q.cs;
  const c = leadCascade(con);
  const st = c.steps;

  setHTML('ld-kpis', [
    kpi('🧠', st[1].pct + '%', 'Know what lead is', `${fmt(st[1].n)} of ${fmt(c.base)}`, 'navy', 'teal'),
    kpi('🌿', st[2].pct + '%', 'Know it reaches turmeric', `${fmt(st[2].n)} respondents`, 'down', 'turmeric'),
    kpi('👶', st[3].pct + '%', 'Concerned for children', 'Moderate or high concern', 'up', 'amber'),
    kpi('🛡️', st[4].pct + '%', 'Know a protective step', 'Could name one', 'neutral', 'purple'),
    kpi('✋', st[5].pct + '%', 'Have acted', 'Knowledge-to-action gap', 'down', 'green'),
    kpi('🏛️', shareIn(rs, AW, 'Q35_iii', ['1']).pct + '%', 'Food authority visited', 'Retailers, last 90 days', 'navy', 'navy'),
  ].join(''));

  const colors = [S(1), S(3), S(8), S(4), S(5), S(2)];
  const el = document.getElementById('ld-funnel');
  if (el) {
    el.innerHTML = st.map((s, i) => {
      const w = Math.max(24, s.pct);
      const drop = i > 0 ? (st[i - 1].pct - s.pct).toFixed(1) : null;
      return (drop !== null && +drop > 0 ? `<div class="funnel-drop">▼ ${drop} pp lost at this step</div>` : '') +
        `<div class="funnel-step" style="background:${colors[i]};width:${w}%;min-width:280px">
           <div><div class="funnel-label">${esc(s.label)}</div><div class="funnel-sub">${esc(s.sub)}</div></div>
           <div class="funnel-val">${s.pct}%<span>${fmt(s.n)}</span></div>
         </div>`;
    }).join('');
  }

  const concernRs = distOf(rs, 'aw', AW, 'Q34', { maxLabel: 26 });
  const concernCs = distOf(cs, 'aw', AW, 'Q_57b', { maxLabel: 26 });
  const merged = new Map();
  [...concernRs, ...concernCs].forEach(x => merged.set(x.label, (merged.get(x.label) || 0) + x.value));
  barChart('ldConcern', [...merged.entries()].map(([label, value]) => ({ label, value })), S(4), true, '', true);

  const cities = [...new Set(con.map(r => cityOf('aw', r[AW.f.city])).filter(Boolean))].sort();
  groupedBar('ldCity', cities, [
    { name: 'Know what lead is', color: S(1), data: cities.map(ct => leadCascade(con.filter(r => cityOf('aw', r[AW.f.city]) === ct)).steps[1].pct) },
    { name: 'Know it reaches turmeric', color: S(8), data: cities.map(ct => leadCascade(con.filter(r => cityOf('aw', r[AW.f.city]) === ct)).steps[2].pct) },
  ]);

  const cr = leadCascade(rs), cc = leadCascade(cs);
  groupedBar('ldSplit', ['Know lead', 'In turmeric', 'Concerned', 'Know how', 'Acted'], [
    { name: 'Retailers', color: S(1), data: [1, 2, 3, 4, 5].map(i => cr.steps[i].pct) },
    { name: 'Consumers', color: S(8), data: [1, 2, 3, 4, 5].map(i => cc.steps[i].pct) },
  ]);

  barChart('ldAvoid', mergeMulti([[rs, 'Q35'], [cs, 'Q_57c_i']], 46), S(6), true, '', true);
  barChart('ldRisk', distOf(cs, 'aw', AW, 'Q_57_iii', { sort: true, maxLabel: 30 }), S(2), true, '', true);
  donutChart('ldAction', distOf(rs, 'aw', AW, 'Q35_i'), [S(6), S(2)]);
  const sfa = distOf(rs, 'aw', AW, 'Q35_iii');
  donutChart('ldSfa', sfa, [S(6), S(2)]);

  setTxt('ld-foot', `The cascade combines both instruments: retailers answer Q33/Q34/Q35 and consumers answer Q57a/Q57b/Q57c, which are the same constructs worded for each audience. Every step is a percentage of all ${fmt(c.base)} consented respondents, so the drops are additive and comparable. The step that matters commercially is the gap between knowing a protective action and taking one — ${(st[4].pct - st[5].pct).toFixed(1)} percentage points here.`);
}

/* ---------- 08 COVERAGE ---------- */
let covRows = [], covSort = { key: 'samples', dir: -1 };
function drawCoverage() {
  const con = Q.con, ts = Q.ts, sp = Q.sp;
  const cities = new Set([
    ...con.map(r => cityOf('aw', r[AW.f.city])),
    ...ts.map(r => cityOf('ts', r[TS.f.sample_city])),
  ].filter(Boolean));

  covRows = [...cities].map(c => {
    const a = con.filter(r => cityOf('aw', r[AW.f.city]) === c);
    const v = ts.filter(r => cityOf('ts', r[TS.f.sample_city]) === c);
    const s = sp.filter(r => cityOf('ts', r[SP.f.city]) === c);
    const types = new Set(s.map(r => r[SP.f.type]).filter(Boolean));
    const ppk = nums(s, SP.f.price_per_kg);
    return {
      city: c,
      aw: a.length, rs: a.filter(r => r[AW.f.Type_of_survey] === 'RS').length,
      cs: a.filter(r => r[AW.f.Type_of_survey] === 'CS').length,
      vendors: v.length, samples: s.length,
      types: types.size, typePct: Math.round(100 * types.size / 4),
      price: ppk.length ? Math.round(median(ppk)) : null,
      scope: a.length && v.length ? 'both' : (v.length ? 'ts' : 'aw'),
    };
  });

  setHTML('cv-stats', [
    statBox(fmt(cities.size), 'Cities in scope', 'Either survey', 'var(--rs-red)'),
    statBox(fmt(covRows.filter(r => r.scope === 'both').length), 'Covered by both', 'Awareness + sampling', 'var(--series-6)'),
    statBox(fmt(con.length), 'Awareness interviews', 'Consented', 'var(--series-3)'),
    statBox(fmt(ts.length), 'Vendors visited', 'Sampling survey', 'var(--turmeric)'),
    statBox(fmt(sp.length), 'Samples banked', 'Awaiting laboratory', 'var(--series-5)'),
    statBox(fmt(covRows.filter(r => r.types === 4).length), 'Cities with all 4 types', 'Full product coverage', 'var(--series-1)'),
  ].join(''));

  renderCovTable();

  const mkt = new Map();
  con.forEach(r => { const v = r[AW.f.market_name]; if (v) mkt.set(v, (mkt.get(v) || 0) + 1); });
  barChart('cvAwMkt', [...mkt.entries()].sort((a, b) => b[1] - a[1])
    .map(([v, n]) => ({
      full: pretty(lab('aw', 'market_name', v)),
      label: short(lab('aw', 'market_name', v), 34), value: n,
    })), S(3), true, '', true);

  const loc = new Map();
  ts.forEach(r => {
    const isW = r[TS.f.market_name] === '1';
    const v = isW ? r[TS.f.wholesale_market] : r[TS.f.locality_retail];
    if (!v) return;
    // Retail localities are multi-line cells in the instrument; the first
    // line names the locality, the rest are the enumerator's landmarks.
    const l = pretty(lab('ts', isW ? 'wholesale_market' : 'locality_retail', v));
    loc.set(l, (loc.get(l) || 0) + 1);
  });
  // Locality names carry the enumerator's landmarks after the place name, so
  // these bars keep the trimmed name and leave the rest to the tooltip —
  // sixteen fully-expanded labels would run the card to two screens.
  const locRows = [...loc.entries()].sort((a, b) => b[1] - a[1]).slice(0, 16)
    .map(([label, value]) => ({ full: label, label: short(label, 32), value }));
  locRows.noExpand = true;
  barChart('cvTsLoc', locRows, S(8), true, '', true);

  setTxt('cv-foot', `Type coverage is the share of the four turmeric product types in the sampling protocol that have been collected at least once in that city — a city at 50% has half the product range still to sample. Median price is across all sampled types in that city and is not adjusted for product mix, so cities skewed towards branded packs will read high.`);
}

function renderCovTable() {
  const q = (document.getElementById('cov-search').value || '').toLowerCase().trim();
  const f = document.getElementById('cov-filter').value;
  let rows = covRows.filter(r => (!q || r.city.toLowerCase().includes(q)) && (!f || r.scope === f));
  const k = covSort.key, d = covSort.dir;
  rows.sort((a, b) => {
    const x = a[k], y = b[k];
    return (typeof x === 'string' ? x.localeCompare(y) : (x || 0) - (y || 0)) * d;
  });

  const cols = [
    ['city', 'City', false], ['aw', 'Interviews', true], ['rs', 'Retailer', true], ['cs', 'Consumer', true],
    ['vendors', 'Vendors', true], ['samples', 'Samples', true], ['typePct', 'Type coverage', true],
    ['price', 'Median Rs/kg', true], ['scope', 'Scope', true],
  ];
  document.getElementById('cov-head').innerHTML = '<tr>' + cols.map(([k2, l, num]) =>
    `<th class="${num ? 'num' : ''}" onclick="sortCov('${k2}')">${esc(l)}${covSort.key === k2 ? `<span class="arrow">${covSort.dir > 0 ? '▲' : '▼'}</span>` : ''}</th>`).join('') + '</tr>';

  const scopeBadge = s => s === 'both' ? '<span class="badge badge-green">Both</span>'
    : s === 'ts' ? '<span class="badge badge-turmeric">Sampling</span>'
      : '<span class="badge badge-teal">Awareness</span>';
  const barColor = p => p >= 100 ? S(6) : p >= 50 ? S(4) : p > 0 ? S(2) : MUTED();

  document.getElementById('cov-body').innerHTML = rows.length ? rows.map(r => `<tr>
    <td class="strong">${esc(r.city)}</td>
    <td class="num">${fmt(r.aw)}</td>
    <td class="num">${fmt(r.rs)}</td>
    <td class="num">${fmt(r.cs)}</td>
    <td class="num">${fmt(r.vendors)}</td>
    <td class="num strong">${fmt(r.samples)}</td>
    <td class="num"><span class="bar-mini"><span style="width:${r.typePct}%;background:${barColor(r.typePct)}"></span></span><span class="pct-num">${r.typePct}%</span></td>
    <td class="num">${r.price ? 'Rs ' + fmt(r.price) : '—'}</td>
    <td class="num">${scopeBadge(r.scope)}</td></tr>`).join('')
    : `<tr><td colspan="9" class="tbl-empty">No cities match the current filters.</td></tr>`;

  setTxt('cov-count', `${rows.length} of ${covRows.length} cities`);
  window.__covVisible = rows;
}
function sortCov(k) {
  covSort = { key: k, dir: covSort.key === k ? -covSort.dir : (k === 'city' ? 1 : -1) };
  renderCovTable();
}

/* ============================== EXPORT ============================== */
function csvCell(v) {
  const s = (v === null || v === undefined) ? '' : String(v);
  return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}
function download(name, text) {
  const blob = new Blob(['﻿' + text], { type: 'text/csv;charset=utf-8;' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1500);
  toast('CSV downloaded');
}
function exportTable() {
  const stamp = (D.meta.data_through || 'export').replace(/-/g, '');
  const rows = window.__covVisible || [];
  const head = ['City', 'Interviews', 'Retailer', 'Consumer', 'Vendors', 'Samples', 'Types sampled', 'Type coverage %', 'Median Rs per kg', 'Scope'];
  download(`turmeric_coverage_${stamp}.csv`,
    [head.join(','), ...rows.map(r => [r.city, r.aw, r.rs, r.cs, r.vendors, r.samples, r.types, r.typePct, r.price, r.scope].map(csvCell).join(','))].join('\n'));
}

/* The header is sticky and its height changes with viewport width (the meta
   block wraps), so the filter bar's sticky offset has to be measured, not
   hard-coded. */
function syncStickyOffset() {
  const h = document.querySelector('.header');
  if (!h) return;
  document.documentElement.style.setProperty('--header-h', h.offsetHeight + 'px');
}

/* =============================== BOOT =============================== */
function boot(payload) {
  D = payload || window.DASHBOARD_DATA || null;
  if (!D) {
    document.getElementById('main-area').innerHTML =
      `<div class="loading"><div>⚠️</div><div>Could not load <code>data/dashboard_data.js</code>.<br>
       Run <code>python scripts/update_dashboard.py</code> to build it.</div></div>`;
    return;
  }
  AW = mkTable(D.aw); TS = mkTable(D.ts); SP = mkTable(D.samples); LB = D.labels || {};
  buildDimensions();
  buildFilterUI();

  const m = D.meta;
  setTxt('meta-scope', `${m.aw.n_cities} awareness · ${m.ts.n_cities} sampling cities`);
  setTxt('meta-line', `Data through ${m.data_through || '—'}`);
  setTxt('meta-pill', `Updated ${m.generated_at}`);
  setTxt('foot-build', `Build ${m.generated_at} · data through ${m.data_through || '—'} · ${fmt(m.aw.n_submissions)} awareness submissions · ${fmt(m.ts.n_samples)} samples`);

  setTxt('banner-tag', 'Live');
  document.getElementById('banner-text').innerHTML =
    `Live fieldwork data, rebuilt daily. Snapshot dated <strong style="color:#fff">${esc(m.data_through || '—')}</strong>.`;
  setTxt('banner-pill', `${fmt(m.aw.n_submissions)} interviews · ${fmt(m.ts.n_samples)} samples`);

  document.querySelectorAll('.nav-tab').forEach(b => {
    b.onclick = () => showPanel(b.dataset.panel, b);
  });
  buildNavSelect();
  document.getElementById('cov-search').oninput = renderCovTable;
  document.getElementById('cov-filter').onchange = renderCovTable;

  syncStickyOffset();
  let rz, wasNarrow = isNarrow();
  window.addEventListener('resize', () => {
    clearTimeout(rz);
    rz = setTimeout(() => {
      syncStickyOffset();
      if (MAP) MAP.invalidateSize();
      // Label wrapping and bar heights are chosen per breakpoint, so crossing
      // it (rotating a phone, mostly) means the panel has to be rebuilt.
      const now = isNarrow();
      if (now !== wasNarrow) { wasNarrow = now; drawPanel(currentPanel); }
    }, 200);
  });

  renderAll();
}
