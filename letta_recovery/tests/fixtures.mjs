import {mkdtempSync,mkdirSync,openSync,closeSync,unlinkSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
export const binding={store_id:'store-fixture',agent_id:'agent-fixture',conversation_id:'conversation-fixture',session_id:'session-fixture',cycle_id:'cycle-fixture',client_message_id:'input-fixture',consumer_id:'consumer-fixture',input_sha:'02b274a59952be0f9eac275ac9aff5d11347760d1f5fbb6b188dbb4d4af3c6e3',owner_revision:'0',config_sha:'b'.repeat(64)};
export function acquireLock(path){const fd=openSync(path+'.test-lock','wx',0o600);return ()=>{closeSync(fd);unlinkSync(path+'.test-lock');};}
export function directory(t){const path=mkdtempSync(join(tmpdir(),'andrew-checkpoint-'));t.after(()=>rmSync(path,{recursive:true,force:true}));return path;}
