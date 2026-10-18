/** Capability remains absent until the complete persistent executor is configured. */
export function createLocalRecovery({sdk,root,enabled=false,gate}){
 return Object.freeze({capability:null});
}
