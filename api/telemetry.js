// A same-origin adapter to Space Station; the ingestion key stays on Vercel.
// Every refusal says exactly why, in Space Station's own words when it refused; only the key is masked.
const { randomUUID } = require('node:crypto');
function problem(e, i) {
  if (!e || typeof e !== 'object') return `event ${i} must be an object, got ${JSON.stringify(e)}`;
  if (typeof e.type !== 'string' || !e.type) return `event ${i} needs a nonempty string type, got ${JSON.stringify(e.type)}`;
  if (e.id && !/^[a-f0-9-]{36}$/i.test(e.id)) return `event ${i} id must be a UUID, got ${JSON.stringify(e.id)}`;
  if (e.metadata && (typeof e.metadata !== 'object' || Array.isArray(e.metadata))) return `event ${i} metadata must be an object, got ${JSON.stringify(e.metadata)}`;
}
module.exports = async function telemetry(req, res) {
  const key = process.env.SILICON_DOCS_TABLE_KEY;
  const mask = text => key ? String(text).split(key).join('[redacted]') : String(text);
  const refuse = (status, error) => res.status(status).json({error:mask(error)});
  if (req.method !== 'POST') return refuse(405, `POST required, got ${req.method}`);
  const origins = ['https://docs.teamofsilicons.com'];
  if (process.env.VERCEL_URL) origins.push('https://' + process.env.VERCEL_URL);
  if (!origins.includes(req.headers.origin)) return refuse(403, `origin rejected: ${JSON.stringify(req.headers.origin ?? null)} is not one of ${origins.join(', ')}`);
  const length = Number(req.headers['content-length'] || 0);
  if (length > 65536) return refuse(413, `batch too large: Content-Length ${length} exceeds 65536 bytes`);
  let body;
  try { body = typeof req.body === 'string' ? JSON.parse(req.body) : req.body; }
  catch (error) { return refuse(400, `invalid JSON: ${error.message}`); }
  const size = JSON.stringify(body || {}).length;
  if (size > 65536) return refuse(413, `batch too large: ${size} bytes of JSON exceeds 65536`);
  if (body?.table !== 'silicondocs') return refuse(400, `invalid telemetry batch: table must be "silicondocs", got ${JSON.stringify(body?.table ?? null)}`);
  if (!Array.isArray(body.events)) return refuse(400, `invalid telemetry batch: events must be an array, got ${JSON.stringify(body.events ?? null)}`);
  if (body.events.length > 40) return refuse(400, `invalid telemetry batch: ${body.events.length} events exceeds 40`);
  const invalid = body.events.map(problem).filter(Boolean);
  if (invalid.length) return refuse(400, `invalid telemetry batch: ${invalid.join('; ')}`);
  if (!key) return refuse(503, 'telemetry unavailable: SILICON_DOCS_TABLE_KEY is not set on this deployment');
  const records = body.events.map(e => ({key,
    metadata:{record_id:e.id || randomUUID(),table_id:'silicondocs',event_ts_ms:Date.now()},
    record:{type:e.type,data:e.data ?? {},metadata:{...e.metadata,source:'silicon-docs'}}}));
  const url = 'https://backend.spacestation.teamofsilicons.com/api/ingest';
  let upstream, text;
  try {
    upstream = await fetch(url, {
      method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({batch_id:randomUUID(),records}),signal:AbortSignal.timeout(8000)
    });
    text = typeof upstream.text === 'function' ? await upstream.text() : JSON.stringify(await upstream.json());
  } catch (error) {
    return refuse(502, `telemetry delivery unavailable: ${url} failed: ${error?.cause?.message ? `${error.message} (${error.cause.message})` : error?.message ?? error}`);
  }
  let ack;
  try { ack = JSON.parse(text); }
  catch (error) { return refuse(502, `telemetry delivery rejected: ${url} answered HTTP ${upstream.status} that is not JSON (${error.message}):\n${text || '(empty body)'}`); }
  if (!upstream.ok || (ack?.rejected || []).some(r => r?.code !== 'duplicate')) {
    return refuse(502, `telemetry delivery rejected: ${url} answered HTTP ${upstream.status}:\n${JSON.stringify(ack)}`);
  }
  return res.status(202).json({accepted:records.length});
};
