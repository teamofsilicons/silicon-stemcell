const MAX_TEXT = 160;
const MAX_BATCH = 40;
const MAX_QUEUE = 200;
const MAX_RETRIES = 2;
const MAX_PAYLOAD = 64 * 1024;
const FLUSH_MS = 1000;
const RETRY_MS = 5000;
const REQUEST_MS = 10000;
const RETRY = Symbol('space-station-retry');

const clip = (value, max = MAX_TEXT) => String(value ?? '').replace(/\s+/g, ' ').trim().slice(0, max);
const fallbackUuid = () => `xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx`.replace(/[xy]/g, (c) => { const r = Math.random() * 16 | 0; return (c === 'x' ? r : (r & 3) | 8).toString(16); });
const eventId = () => {
  try {
    if (typeof process !== 'undefined' && process.versions?.node && typeof require === 'function') return require('node:crypto').randomUUID();
    return globalThis.crypto?.randomUUID?.() || fallbackUuid();
  } catch (_) { return fallbackUuid(); }
};
const finiteRate = (value) => Math.max(0, Math.min(1, Number.isFinite(Number(value)) ? Number(value) : 1));
const cleanUrl = (value) => { try { const u = new URL(String(value), globalThis.location?.origin || 'http://localhost'); return `${u.origin}${u.pathname}`; } catch (_) { return undefined; } };
function jsonValue(value, max = 8192) {
  try {
    const parsed = JSON.parse(JSON.stringify(value ?? {}));
    return JSON.stringify(parsed).length <= max ? parsed : { error: 'value_too_large' };
  } catch (_) { return { error: 'unserializable_value' }; }
}
function browserName(ua) { if (/Edg\//i.test(ua)) return 'edge'; if (/Chrome\//i.test(ua)) return 'chrome'; if (/Firefox\//i.test(ua)) return 'firefox'; if (/Safari\//i.test(ua)) return 'safari'; return 'unknown'; }
function nextBatch(table, events, offset) {
  let end = Math.min(offset + MAX_BATCH, events.length);
  while (end > offset + 1 && JSON.stringify({ table, events: events.slice(offset, end) }).length > MAX_PAYLOAD) end -= 1;
  return events.slice(offset, end);
}

/** Adapt a browser batch for the normal server ingest contract. Keep the table key server-side. */
function toIngestBatch({ table, key, events, batchId = eventId() }) {
  if (!table || !key || !Array.isArray(events) || events.length > MAX_BATCH) throw new TypeError('toIngestBatch({table, key, events}) requires a table, server-side key, and at most 40 events');
  return {
    batch_id: batchId,
    records: events.map((event) => ({
      key,
      metadata: { record_id: event.id || eventId(), table_id: table, event_ts_ms: Date.parse(event.metadata?.occurred_at) || Date.now() },
      record: { type: event.type, data: event.data, metadata: event.metadata || {} },
    })),
  };
}

/** Create a framework-agnostic browser analytics and events sender. */
function createSpaceStationWeb(options = {}) {
  const root = typeof globalThis === 'undefined' ? {} : globalThis;
  const win = root.window;
  const doc = win?.document;
  const nav = root.navigator || win?.navigator;
  const endpoint = options.endpoint || '/api/web/telemetry';
  const transport = options.fetch || root.fetch;
  const analyticsTable = options.analyticsTable;
  const eventsTable = options.eventsTable;
  const rate = finiteRate(options.sampleRate);
  const queues = new Map();
  const cleanups = [];
  const session = eventId();
  let enabled = options.enabled !== false;
  let destroyed = false;
  let timer;
  let retryTimer;
  let flushing;
  const ua = clip(nav?.userAgent || '');
  const boundQueues = () => { while ([...queues.values()].reduce((n, q) => n + q.length, 0) > MAX_QUEUE) { const first = queues.keys().next().value; const q = queues.get(first); q?.shift(); if (!q?.length) queues.delete(first); } };

  function context() {
    const base = {
      session_id: session,
      url: cleanUrl(root.location?.href),
      path: typeof root.location?.pathname === 'string' ? root.location.pathname : undefined,
      referrer: cleanUrl(doc?.referrer),
      user_agent: ua || undefined,
      browser: browserName(ua),
      device: /Mobi|Android|iPhone|iPad/i.test(ua) ? 'mobile' : 'desktop',
      language: nav?.language,
      viewport: win ? { width: win.innerWidth, height: win.innerHeight } : undefined,
      screen: root.screen ? { width: root.screen.width, height: root.screen.height, pixel_ratio: root.devicePixelRatio || 1 } : undefined,
    };
    for (const key of Object.keys(base)) if (base[key] === undefined) delete base[key];
    return base;
  }

  function enqueue(table, type, data, metadata, sampled) {
    if (!enabled || !table || destroyed || (sampled && Math.random() > rate)) return;
    const event = { id: eventId(), type: clip(type), data: jsonValue(data), metadata: { ...context(), ...jsonValue(metadata, 4096), occurred_at: new Date().toISOString() } };
    Object.defineProperty(event, RETRY, { value: 0, writable: true, enumerable: false });
    const list = queues.get(table) || [];
    list.push(event);
    queues.set(table, list);
    boundQueues();
    if (list.length >= MAX_BATCH) void flush(table);
    else if (!timer) timer = setTimeout(() => { timer = undefined; void flush(); }, FLUSH_MS);
  }

  function analytics(type, data, metadata) { enqueue(analyticsTable, type, data, metadata, true); }
  function track(name, data = {}, metadata = {}) {
    if (!name || typeof name !== 'string') throw new TypeError('track(name, data, metadata): name must be a non-empty string');
    enqueue(eventsTable, name, data, metadata, false);
  }
  function setEnabled(value) { enabled = value !== false; if (!enabled) queues.clear(); return enabled; }

  async function send(table, events) {
    if (typeof transport !== 'function') throw new Error('Space Station web telemetry requires fetch or options.fetch');
    let timeout;
    const controller = typeof AbortController !== 'undefined' ? new AbortController() : undefined;
    try {
      const request = transport(endpoint, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ table, events }), keepalive: true, ...(controller ? { signal: controller.signal } : {}) });
      const response = await Promise.race([request, new Promise((_, reject) => { timeout = setTimeout(() => { controller?.abort(); reject(new Error('telemetry request timed out')); }, REQUEST_MS); })]);
      if (!response?.ok) throw new Error(`telemetry endpoint returned HTTP ${response?.status ?? 'unknown'}`);
    } finally { if (timeout) clearTimeout(timeout); }
  }

  async function flush(table) {
    if (flushing) return flushing;
    flushing = (async () => {
      const result = { sent: 0, failed: 0, dropped: 0, queued: 0 };
      for (const key of table ? [table] : [...queues.keys()]) {
        const events = queues.get(key); if (!events?.length) continue;
        queues.delete(key);
        for (let offset = 0; offset < events.length;) {
          if (!enabled || destroyed) { result.dropped += events.length - offset; break; }
          const batch = nextBatch(key, events, offset);
          offset += batch.length;
          try { await send(key, batch); result.sent += batch.length; }
          catch (error) {
            result.failed += batch.length;
            const retry = batch.filter((event) => { event[RETRY] += 1; return event[RETRY] <= MAX_RETRIES; });
            result.dropped += batch.length - retry.length;
            if (enabled && !destroyed && retry.length) { queues.set(key, [...retry, ...(queues.get(key) || [])]); boundQueues(); }
          }
        }
      }
      result.queued = [...queues.values()].reduce((n, q) => n + q.length, 0);
      if (result.queued && !retryTimer) retryTimer = setTimeout(() => { retryTimer = undefined; void flush(); }, RETRY_MS); if (!result.queued && retryTimer) { clearTimeout(retryTimer); retryTimer = undefined; }
      return result;
    })().finally(() => { flushing = undefined; });
    return flushing;
  }

  const on = (target, event, handler, opts) => { if (!target?.addEventListener) return; target.addEventListener(event, handler, opts); cleanups.push(() => target.removeEventListener(event, handler, opts)); };
  if (win && analyticsTable) {
    const page = () => analytics('page_view');
    page();
    let activeSince = Date.now();
    let leftPage = false;
    const leave = () => { if (leftPage) return; leftPage = true; analytics('page_exit', { elapsed_ms: Math.max(0, Date.now() - activeSince) }); void flush(); };
    const resume = () => { if (!leftPage) return; leftPage = false; activeSince = Date.now(); };
    on(doc, 'visibilitychange', () => doc.visibilityState === 'hidden' ? leave() : resume());
    on(win, 'pagehide', leave);
    on(doc, 'click', (event) => { const target = event.target?.closest?.('*') || event.target; analytics('click', { tag: target?.tagName?.toLowerCase(), role: target?.getAttribute?.('role') || undefined, marker: clip(target?.getAttribute?.('data-spacestation-event')) || undefined }); }, { passive: true });
    on(win, 'error', (event) => analytics('error', { kind: 'runtime', message: clip(event.message), source: cleanUrl(event.filename), line: event.lineno, column: event.colno }));
    on(win, 'unhandledrejection', () => analytics('error', { kind: 'unhandledrejection' }));
    let scrollTimer;
    on(win, 'scroll', () => { if (scrollTimer) return; scrollTimer = setTimeout(() => { scrollTimer = undefined; const height = Math.max(1, doc.documentElement?.scrollHeight || 1); analytics('scroll', { percent: Math.min(100, Math.round(((win.scrollY + win.innerHeight) / height) * 100)) }); }, 250); }, { passive: true });
    cleanups.push(() => { if (scrollTimer) clearTimeout(scrollTimer); });
    const timing = () => { const entry = root.performance?.getEntriesByType?.('navigation')?.[0]; if (entry) analytics('timing', { dns_ms: entry.domainLookupEnd - entry.domainLookupStart, connect_ms: entry.connectEnd - entry.connectStart, response_ms: entry.responseEnd - entry.responseStart, dom_content_loaded_ms: entry.domContentLoadedEventEnd, load_ms: entry.loadEventEnd }); };
    if (doc.readyState === 'complete') timing(); else on(win, 'load', timing, { once: true });
    for (const method of ['pushState', 'replaceState']) { const original = win.history?.[method]; if (!original) continue; win.history[method] = function wrappedHistory() { const value = original.apply(this, arguments); page(); return value; }; cleanups.push(() => { if (win.history[method]) win.history[method] = original; }); }
    on(win, 'popstate', page);
    if (typeof win.fetch === 'function') { const original = win.fetch; const wrapped = function wrappedFetch(input, init) { const started = Date.now(); const url = typeof input === 'string' ? input : input?.url || ''; if (cleanUrl(url) === cleanUrl(endpoint)) return original.apply(this, arguments); return original.apply(this, arguments).then((response) => { analytics('network', { method: init?.method || input?.method || 'GET', url: cleanUrl(url), status: response.status, duration_ms: Date.now() - started }); return response; }, (error) => { analytics('network_error', { method: init?.method || input?.method || 'GET', url: cleanUrl(url), duration_ms: Date.now() - started }); throw error; }); }; win.fetch = wrapped; cleanups.push(() => { if (win.fetch === wrapped) win.fetch = original; }); }
  }
  return { track, flush, setEnabled, isEnabled: () => enabled, analytics: (name, data, metadata) => analytics(name, data, metadata), destroy: async () => { if (destroyed) return; destroyed = true; cleanups.splice(0).forEach((cleanup) => cleanup()); if (timer) clearTimeout(timer); if (retryTimer) clearTimeout(retryTimer); await flush(); } };
}

export { createSpaceStationWeb, toIngestBatch };
export default createSpaceStationWeb;
