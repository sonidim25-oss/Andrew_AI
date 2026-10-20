import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {LocalSDKAdapter} from '../sdk-adapter.mjs';

async function fixture(t,{delay=0,timeout=200,status=200}={}){
 let calls=0;
 const server=createServer((request,response)=>{calls++;request.resume();setTimeout(()=>{response.writeHead(status,{'Content-Type':'application/json'});response.end(JSON.stringify({saved:'fixture'}));},delay);});
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 t.after(()=>new Promise(resolve=>{server.closeAllConnections();server.close(resolve);}));
 const sdk=new LocalSDKAdapter({providerTimeoutMs:timeout});
 sdk.connection=async()=>({url:`http://127.0.0.1:${server.address().port}`,key:'fixture'});
 return {sdk,get calls(){return calls;}};
}
test('provider_transport_has_eleven_minute_default_without_changing_control_deadline',()=>{
 assert.equal(new LocalSDKAdapter({}).providerTimeoutMs,660000);
});
test('long_provider_reply_is_received_once_within_extended_deadline',async t=>{
 const f=await fixture(t,{delay:60,timeout:200});
 assert.deepEqual(await f.sdk.request({},'/chat/completions',{}),{saved:'fixture'});assert.equal(f.calls,1);
});
test('provider_deadline_and_disconnect_never_retry_the_request',async t=>{
 const f=await fixture(t,{delay:200,timeout:20});
 await assert.rejects(f.sdk.request({},'/chat/completions',{}));assert.equal(f.calls,1);
});
test('provider_transport_does_not_follow_redirects',async t=>{
 const f=await fixture(t,{status:302});await assert.rejects(f.sdk.request({},'/chat/completions',{}));assert.equal(f.calls,1);
});
