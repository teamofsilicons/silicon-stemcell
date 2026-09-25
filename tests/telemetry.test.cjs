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
    // Refusals say why: which event, and Space Station's own status and words, with only the key masked.
    const invalid=await call({table:'silicondocs',events:[{type:'ok'},{type:''}]});
    assert.equal(invalid.status,400);assert.match(invalid.value.error,/event 1 needs a nonempty string type, got ""/);
    assert.match((await call('{"table":')).value.error,/^invalid JSON: /);
    global.fetch = async () => ({ok:false,status:422,text:async()=>JSON.stringify({rejected:[{code:'schema',reason:'bad record for '+process.env.SILICON_DOCS_TABLE_KEY}]})});
    const rejected=await call({table:'silicondocs',events:[{type:'pageview'}]});
    assert.equal(rejected.status,502);
    assert.match(rejected.value.error,/answered HTTP 422:\n.*"code":"schema","reason":"bad record for \[redacted\]"/);
    assert(!JSON.stringify(rejected).includes(process.env.SILICON_DOCS_TABLE_KEY));
    global.fetch = async () => { throw Object.assign(new Error('fetch failed'),{cause:new Error('getaddrinfo ENOTFOUND backend')}); };
    assert.match((await call({table:'silicondocs',events:[{type:'pageview'}]})).value.error,/fetch failed \(getaddrinfo ENOTFOUND backend\)/);
  } finally {global.fetch=originalFetch;delete process.env.SILICON_DOCS_TABLE_KEY;}
});
