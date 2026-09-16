// A same-origin adapter to Space Station; the ingestion key stays on Vercel.
const { randomUUID } = require('node:crypto');
module.exports = async function telemetry(req, res) {
  if (req.method !== 'POST') return res.status(405).json({error:'POST required'});
  const origins = ['https://docs.teamofsilicons.com'];
  if (process.env.VERCEL_URL) origins.push('https://' + process.env.VERCEL_URL);
  if (!origins.includes(req.headers.origin)) return res.status(403).json({error:'origin rejected'});
  if (Number(req.headers['content-length'] || 0) > 65536) return res.status(413).json({error:'batch too large'});
  let body;
  try { body = typeof req.body === 'string' ? JSON.parse(req.body) : req.body; }
  catch { return res.status(400).json({error:'invalid JSON'}); }
  if (JSON.stringify(body || {}).length > 65536) return res.status(413).json({error:'batch too large'});
  if (body?.table !== 'silicondocs' || !Array.isArray(body.events) || body.events.length > 40 ||
      body.events.some(e => !e || typeof e.type !== 'string' || !e.type ||
        (e.id && !/^[a-f0-9-]{36}$/i.test(e.id)) || (e.metadata && (typeof e.metadata !== 'object' || Array.isArray(e.metadata))))) {
    return res.status(400).json({error:'invalid telemetry batch'});
  }
  const key = process.env.SILICON_DOCS_TABLE_KEY;
  if (!key) return res.status(503).json({error:'telemetry unavailable'});
  const records = body.events.map(e => ({key,
    metadata:{record_id:e.id || randomUUID(),table_id:'silicondocs',event_ts_ms:Date.now()},
    record:{type:e.type,data:e.data ?? {},metadata:{...e.metadata,source:'silicon-docs'}}}));
  try {
    const upstream = await fetch('https://backend.spacestation.teamofsilicons.com/api/ingest', {
      method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({batch_id:randomUUID(),records}),signal:AbortSignal.timeout(8000)
    });
    const ack = await upstream.json();
    if (!upstream.ok || (ack.rejected || []).some(r => r.code !== 'duplicate')) return res.status(502).json({error:'telemetry delivery rejected'});
    return res.status(202).json({accepted:records.length});
  } catch { return res.status(502).json({error:'telemetry delivery unavailable'}); }
};
