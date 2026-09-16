// node backend/tests/live.mjs /absolute/private-fixture.json
// Fixture: base, silicon, org_id, publisher_token, reader_token, optional testing_context/caller.
import fs from 'node:fs';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
const input=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const base=input.base;
const body={silicon:input.silicon,org_id:input.org_id,...(input.testing_context?{testing_context:input.testing_context}:{})};
async function post(path,data=body,token=input.reader_token,headers={}){
    const response=await fetch(base+path,{method:'POST',headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{}),...headers},body:typeof data==='string'?data:JSON.stringify(data)});
    return [response.status,await response.json()];
}
async function socket(path){
    const ws=new WebSocket(base.replace(/^http/,'ws')+path);const queue=[];let waiter;
    ws.addEventListener('message',event=>{const value=JSON.parse(String(event.data));if(waiter){const resolve=waiter;waiter=null;resolve(value)}else{queue.push(value)}});
    await new Promise((resolve,reject)=>{ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',reject,{once:true})});
    return {ws,send:value=>ws.send(JSON.stringify(value)),next:()=>queue.length?Promise.resolve(queue.shift()):new Promise((resolve,reject)=>{const timeout=setTimeout(()=>{waiter=null;reject(new Error('WebSocket message timed out'))},20000);waiter=value=>{clearTimeout(timeout);resolve(value)}})};
}
const [status,identity]=await post('/v1/auth/status',input.testing_context?{testing_context:input.testing_context}:{});
assert.equal(status,200);assert.equal(identity.authenticated,true);
assert.equal((await post('/v1/config',body,null))[0],401);
const publisher=await socket('/v1/publish');
publisher.send({...body,type:'register',silicon:'wrong:'+input.org_id,access_token:input.publisher_token});
assert.ok((await publisher.next()).error);
publisher.send({...body,type:'register',telemetry:false,access_token:input.publisher_token,configuration:{silicon:{token:'must-not-leak'},isi:{main:{model:'verification'}}}});
assert.equal((await publisher.next()).type,'registered');
const [configStatus,config]=await post('/v1/config');assert.equal(configStatus,200);assert.equal(config.online,true);assert.equal(config.configuration.silicon.token,'[REDACTED]');
assert.equal((await post('/v1/config',{...body,org_id:'unselected-test-org',silicon:'bot:unselected-test-org'}))[0],401);
const [ticketStatus,ticket]=await post('/v1/subscribe');assert.equal(ticketStatus,200);
const subscriber=await socket('/v1/subscribe');subscriber.send({ticket:ticket.ticket});assert.equal((await subscriber.next()).type,'snapshot');
const duplicate=await socket('/v1/subscribe');duplicate.send({ticket:ticket.ticket});assert.ok((await duplicate.next()).error);duplicate.ws.close();
publisher.send({type:'event',telemetry:false,silicon:input.silicon,data:{isi:'main',message:'SILICON_REALTIME_OK'}});assert.equal((await publisher.next()).type,'ack');assert.equal((await subscriber.next()).data.message,'SILICON_REALTIME_OK');
publisher.send({type:'ping',telemetry:false});assert.equal((await publisher.next()).type,'pong');assert.equal((await subscriber.next()).data.online,true);
if(input.caller){
    for(const action of ['config','subscribe']){
        const raw=JSON.stringify(body),endpoint=action==='config'?'silicon.configuration.read':'silicon.events.subscribe';
        const r=spawnSync('iam',['--test',input.caller.environment_id,'--json','app','obo','exchange','tos>silicon-realtime',endpoint,'--as-app-id',input.caller.app_id,'--app-secret',input.caller.app_secret,'--subject-token',input.caller.access_token,'--org-context',input.org_id,'--method','POST','--body',raw],{encoding:'utf8'});
        if(r.status!==0)throw new Error('IAM OBO exchange rejected: '+r.stderr);
        const proof=JSON.parse(r.stdout);const [code,value]=await post('/v1/obo/'+action,raw,null,{'X-OBO-Proof':proof.access_proof});assert.equal(code,200,'OBO '+action+' failed');
        assert.equal((await post('/v1/obo/'+action,raw,null,{'X-OBO-Proof':proof.access_proof}))[0],401,'proof replay must fail');
        if(action==='config')assert.equal(value.online,true);
        else {const delegated=await socket('/v1/subscribe');delegated.send({ticket:value.ticket});assert.equal((await delegated.next()).type,'snapshot');delegated.ws.close()}
    }
    console.log('IAM OBO configuration, subscription, and single-use proof verification passed');
}
publisher.send({type:'execute',silicon:input.silicon,command:'must-not-run'});assert.ok((await publisher.next()).error);
publisher.ws.close();const offline=await subscriber.next();assert.equal(offline.type,'presence');assert.equal(offline.data.online,false);subscriber.ws.close();
console.log('Live IAM authentication, identity/org boundaries, configuration redaction, WSS event delivery, ping/pong, read-only rejection, single-use tickets, and offline presence passed');
