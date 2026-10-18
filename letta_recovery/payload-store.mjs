import * as fs from 'node:fs';
import {join,resolve,parse,sep} from 'node:path';
import {randomUUID} from 'node:crypto';
import {sha,LIMITS,canonical} from './contracts.mjs';

export function safeDirectory(path,{create=false}={}){
 const absolute=resolve(path);let current=parse(absolute).root;
 for(const part of absolute.slice(current.length).split(sep).filter(Boolean)){
  current=join(current,part);
  try{const info=fs.lstatSync(current);if(!info.isDirectory()||info.isSymbolicLink())throw Error('unsafe directory');}
  catch(error){if(error.code!=='ENOENT')throw error;if(!create)return false;fs.mkdirSync(current,{mode:0o700});}
 }
 return true;
}
export function syncDirectory(path){const fd=fs.openSync(path,fs.constants.O_RDONLY|fs.constants.O_DIRECTORY|fs.constants.O_NOFOLLOW);try{fs.fsyncSync(fd);}finally{fs.closeSync(fd);}}
export function readRegular(path,limit){
 const fd=fs.openSync(path,fs.constants.O_RDONLY|fs.constants.O_NOFOLLOW|fs.constants.O_NONBLOCK);
 try{
  const before=fs.fstatSync(fd);if(!before.isFile())throw Error('unsafe file');if(before.size>limit)throw Error('storage_limit');
  const bytes=Buffer.alloc(before.size+1);let offset=0,count;
  while(offset<bytes.length&&(count=fs.readSync(fd,bytes,offset,bytes.length-offset,null))>0)offset+=count;
  const after=fs.fstatSync(fd);if(offset!==before.size||before.mtimeMs!==after.mtimeMs||before.ctimeMs!==after.ctimeMs)throw Error('state_corrupt');
  return bytes.subarray(0,offset);
 }finally{fs.closeSync(fd);}
}
export function readJSON(path,limit=LIMITS.metadata){
 const bytes=readRegular(path,limit);const value=JSON.parse(bytes.toString('utf8'));
 if(canonical(value)!==bytes.toString('utf8'))throw Error('noncanonical state');return value;
}
export function atomicWrite(path,bytes){
 const temporary=path+'.'+randomUUID()+'.tmp';let fd;
 try{
  fd=fs.openSync(temporary,'wx',0o600);fs.writeFileSync(fd,bytes);fs.fsyncSync(fd);fs.closeSync(fd);fd=undefined;
  if(fs.existsSync(path)&&!fs.lstatSync(path).isFile())throw Error('unsafe file');
  fs.renameSync(temporary,path);syncDirectory(resolve(path,'..'));
 }finally{if(fd!==undefined)fs.closeSync(fd);try{fs.unlinkSync(temporary);}catch(error){if(error.code!=='ENOENT')throw error;}}
}
export class PayloadStore{
 constructor({root,limits=LIMITS}){this.root=resolve(root);this.limits={...LIMITS,...limits};}
 usage(){if(!safeDirectory(this.root))return 0;return fs.readdirSync(this.root).reduce((sum,name)=>{const p=join(this.root,name),s=fs.lstatSync(p);if(!s.isFile()||s.isSymbolicLink())throw Error('unsafe payload');return sum+s.size;},0);}
 putPayload(bytes){
  if(!Buffer.isBuffer(bytes)||bytes.length>this.limits.payload)throw Error('storage_limit');
  safeDirectory(this.root,{create:true});const hash=sha(bytes),path=join(this.root,hash);
  if(fs.existsSync(path)){if(!this.readPayload(hash).equals(bytes))throw Error('state_corrupt');return {sha:hash,size:bytes.length};}
  if(this.usage()+bytes.length>this.limits.conversation)throw Error('storage_limit');
  atomicWrite(path,bytes);return {sha:hash,size:bytes.length};
 }
 readPayload(hash){
  if(typeof hash!=='string'||!/^([a-f0-9]{64})$/.test(hash)||!safeDirectory(this.root))throw Error('state_corrupt');
  const bytes=readRegular(join(this.root,hash),this.limits.payload);if(sha(bytes)!==hash)throw Error('state_corrupt');return bytes;
 }
}
