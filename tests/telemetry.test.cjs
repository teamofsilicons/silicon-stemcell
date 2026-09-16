const test = require('node:test');
const assert = require('node:assert/strict');
const handler = require('../api/telemetry.js');
test('docs telemetry rejects foreign origins, invalid/oversized batches and preserves event identity', async () => {
  const originalFetch = global.fetch;
  process.env.SILICON_DOCS_TABLE_KEY = 'table-silicondocs-00000000000000000000000000000000';
  let sent;
  global.fetch = async (_, options) => { sent = JSON.parse(options.body); return {ok:true,json:async()=>({rejected:[]})}; };
  const call = async (body, headers={origin:'https://docs.teamofsilicons.com'}) => {
    let status, value;
    await handler({method:'POST',body,headers},{status(s){status=s;return this;},json(v){value=v;}});
    return {status,value};
  };
  try {
    assert.equal((await call({table:'silicondocs',events:[]},{origin:'https://evil.example'})).status,403);
    assert.equal((await call({table:'private',events:[]})).status,400);
    assert.equal((await call({table:'silicondocs',events:[null]})).status,400);
    assert.equal((await call({table:'silicondocs',events:[],padding:'x'.repeat(66000)})).status,413);
    const id='12345678-1234-4234-8234-123456789abc';
    const result=await call({table:'silicondocs',events:[{id,type:'pageview',data:{path:'/'}}]});
    assert.equal(result.status,202);assert.equal(sent.records[0].metadata.record_id,id);
    assert.equal(sent.records[0].record.metadata.source,'silicon-docs');
    assert(!JSON.stringify(result).includes(process.env.SILICON_DOCS_TABLE_KEY));
  } finally {global.fetch=originalFetch;delete process.env.SILICON_DOCS_TABLE_KEY;}
});
