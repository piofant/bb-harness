import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createFakePluginHost,makeThreadResponse,makeTurnFailedEvent,experimental_scanPublicSdkOnly} from '@get-bb/plugin-sdk/testing';
import {createCoordinator,handoffInput} from '../src/coordinator.js';
import {fallbackReason} from '../src/policy.js';

const text=(value:string)=>({type:'text' as const,text:value,mentions:[]});
function fixture(options:{retry?:boolean;mode?:'full'|'auto';ambiguous?:boolean}={}){
 const execution={model:'claude-sonnet-4-6',reasoningLevel:'high',permissionMode:options.mode??'full',serviceTier:'default',source:'client/turn/requested' as const};
 const req={id:'e1',threadId:'root',seq:1,createdAt:1,scope:{kind:'thread' as const},type:'client/turn/requested' as const,data:{requestId:'request',input:[text('remember nonce-abc')],execution}};
 let children:ReturnType<typeof makeThreadResponse>[]=[],spawnCount=0;
 const h=createFakePluginHost({pluginId:'claude-codex-fallback',sdk:{threads:{
  get:async({threadId})=>makeThreadResponse({id:threadId,projectId:'p',environmentId:'env',providerId:threadId==='root'?'claude-code':'codex',status:threadId==='root'?'error':'idle'}),
  defaultExecutionOptions:async()=>execution,
  events:{list:async()=>[req] as any},
  interactions:{list:async()=>[]},queuedMessages:{list:async()=>[]},
  list:async()=>children,
  getPluginMetadata:async()=>({operation:operation}),
  spawn:async(args)=>{spawnCount++;operation=String(args.pluginMetadata?.operation);children=[makeThreadResponse({id:'child',projectId:'p',environmentId:'env',providerId:'codex',status:'idle',originPluginId:'claude-codex-fallback'})];if(options.ambiguous)throw new Error('lost response');return children[0]!;},
 },providers:{list:async()=>[{id:'codex',available:true,capabilities:{permissionModes:['full']}}] as any},plugins:{experimental_discoverRpc:async()=>options.retry?[{pluginId:'provider-retry'}] as any:[],callRpc:async()=>({requestId:'request',decision:{kind:'retry',reason:'limit',sendAt:Date.now()+100000}}) as any}}});
 let operation='';
 return{...h,c:createCoordinator(h.bb),get spawnCount(){return spawnCount;},req};
}
test('verification is eligible; policy/context/unknown failures are not',()=>{
 for(const category of ['policy-violation','context-window-exceeded','bad-request'])assert.equal(fallbackReason({errorInfo:{category} as any}),null);
 assert.equal(fallbackReason({errorInfo:null}),null);
 assert.match(fallbackReason({errorInfo:{category:'unauthorized',providerCode:'verification_required'} as any})!,/проверки/);
});
test('real SQLite state and one attributed child despite duplicate/concurrent notifications',async()=>{
 const h=fixture();try{
 await h.c.enable('root');
 await h.c.failure(makeTurnFailedEvent({threadId:'root',requestId:'request',errorInfo:{category:'unauthorized',httpStatusCode:403,providerCode:'verification_required'}}));
 await Promise.all([h.c.step('root'),h.c.step('root')]);
 assert.equal(h.spawnCount,1);assert.equal(h.c.store.get('root')!.activeThreadId,'child');
 await h.c.failure(makeTurnFailedEvent({threadId:'root',requestId:'request'}));await h.c.step('root');assert.equal(h.spawnCount,1);
 const call=h.harness.inspection.sdk.callsTo('threads.spawn')[0];assert.ok(call);assert.equal(h.c.store.list().length,1);
 assert.equal((call[0] as any).model,'gpt-6.2-sol');
 assert.equal((call[0] as any).reasoningLevel,'high');
 assert.equal((call[0] as any).visibility,'visible');
 assert.equal((call[0] as any).parentThreadId,'root');
 assert.equal((call[0] as any).title,'Продолжение · Codex');
 }finally{await h.harness.lifecycle.dispose();}
});
test('provider-retry ownership prevents handoff',async()=>{
 const h=fixture({retry:true});try{await h.c.enable('root');await h.c.failure(makeTurnFailedEvent({threadId:'root',requestId:'request',errorInfo:{category:'rate-limit'}}));await h.c.step('root');assert.equal(h.spawnCount,0);assert.equal(h.c.store.get('root')!.phase,'waiting-retry');}finally{await h.harness.lifecycle.dispose();}
});
test('permission incompatibility fails closed',async()=>{
 const h=fixture({mode:'auto'});try{await h.c.enable('root');await h.c.failure(makeTurnFailedEvent({threadId:'root',requestId:'request',errorInfo:{category:'unauthorized'}}));await h.c.step('root');assert.equal(h.spawnCount,0);assert.equal(h.c.store.get('root')!.phase,'blocked');}finally{await h.harness.lifecycle.dispose();}
});
test('ambiguous spawn reconciles existing operation without creating a second child',async()=>{
 const h=fixture({ambiguous:true});try{await h.c.enable('root');await h.c.failure(makeTurnFailedEvent({threadId:'root',requestId:'request',errorInfo:{category:'unauthorized'}}));await h.c.step('root');assert.equal(h.c.store.get('root')!.phase,'blocked');await h.c.step('root');assert.equal(h.spawnCount,1);assert.equal(h.c.store.get('root')!.activeThreadId,'child');}finally{await h.harness.lifecycle.dispose();}
});
test('handoff contains interrupted input and refuses missing input',()=>{
 const h=fixture();try{assert.match(JSON.stringify(handoffInput([h.req] as any,'root','request','verification')),/nonce-abc/);assert.throws(()=>handoffInput([],'root','missing','failure'));}finally{void h.harness.lifecycle.dispose();}
});
test('public SDK imports only',async()=>{const result=await experimental_scanPublicSdkOnly(new URL('..',import.meta.url).pathname,{allow:[/^react$/, /^react-dom$/, /^@radix-ui\/react-(slot|checkbox|dialog)$/, /^class-variance-authority$/, /^clsx$/, /^tailwind-merge$/, /^@\/(components|lib)\//, /^vitest$/, /^@testing-library\/react$/]});assert.deepEqual(result.violations,[]);assert.deepEqual(result.privateDependencies,[]);});

test('native upgrade reveals only owned continuations once and preserves custom titles',async()=>{
 const h=fixture();try{
  await h.c.enable('root');
  const r=h.c.store.get('root')!;
  h.c.store.save({...r,activeThreadId:'custom',segments:[...r.segments,...['child','custom','foreign'].map(threadId=>({threadId,providerId:'codex',model:'gpt-6.1-sol',createdAt:2}))]});
  h.harness.sdk.stub('threads.get',async({threadId}:any)=>makeThreadResponse({id:threadId,originPluginId:threadId==='foreign'?'other-plugin':'claude-codex-fallback',visibility:'hidden',title:threadId==='custom'?'My title':r.title}));
  h.harness.sdk.stub('threads.getPluginMetadata',async()=>({rootThreadId:'root'}));
  h.harness.sdk.stub('threads.update',async({threadId}:any)=>makeThreadResponse({id:threadId,visibility:'visible'}));
  await h.c.exposeContinuations();await h.c.exposeContinuations();
  assert.deepEqual(h.harness.inspection.sdk.callsTo('threads.update'),[
   [{threadId:'child',visibility:'visible',title:'Продолжение · Codex'}],
   [{threadId:'custom',visibility:'visible'}],
  ]);
 }finally{await h.harness.lifecycle.dispose();}
});

test('queue copy retains exact attachments/schedule and originals, never sends twice',async()=>{
 const h=fixture();try{
 await h.c.enable('root');await h.c.failure(makeTurnFailedEvent({threadId:'root',requestId:'request',errorInfo:{category:'unauthorized'}}));await h.c.step('root');
 const original={id:'q1',updatedAt:7,content:[text('next'),{type:'localImage',path:'/same/image.png'}],payload:{kind:'inline'},groupWithNext:false,permissionMode:'full',sendAt:9000000000000,senderThreadId:null};
 const dest:any[]=[];
 h.harness.sdk.stub('threads.queuedMessages.list',async({threadId}:any)=>threadId==='root'?[original]:dest);
 h.harness.sdk.stub('threads.queuedMessages.create',async(args:any)=>{const row={id:'copy',content:args.input,updatedAt:10,sendAt:null};dest.push(row);return row;});
 h.harness.sdk.stub('threads.queuedMessages.update',async(args:any)=>{if(args.threadId==='root')return original;dest[0].sendAt=args.sendAt;return dest[0];});
 await h.c.step('root');await h.c.step('root');
 assert.equal(h.harness.inspection.sdk.callsTo('threads.queuedMessages.create').length,1);
 assert.deepEqual(dest[0].content,original.content);assert.equal(dest[0].sendAt,original.sendAt);
 assert.equal(h.harness.inspection.sdk.callsTo('threads.queuedMessages.delete').length,0);
 assert.equal(h.c.store.get('root')!.pendingQueueCount,0);
 }finally{await h.harness.lifecycle.dispose();}
});
test('ambiguous queue create retains original and does not create another copy',async()=>{
 const h=fixture();try{
 await h.c.enable('root');await h.c.failure(makeTurnFailedEvent({threadId:'root',requestId:'request',errorInfo:{category:'unauthorized'}}));await h.c.step('root');
 const row={id:'q1',updatedAt:7,content:[text('must survive')],payload:{kind:'inline'},groupWithNext:false,permissionMode:'full',sendAt:null};
 h.harness.sdk.stub('threads.queuedMessages.list',async({threadId}:any)=>threadId==='root'?[row]:[]);
 h.harness.sdk.stub('threads.queuedMessages.update',async()=>row);
 h.harness.sdk.stub('threads.queuedMessages.create',async()=>{throw new Error('ambiguous timeout');});
 await h.c.step('root');await h.c.step('root');
 assert.equal(h.c.store.get('root')!.phase,'blocked');assert.equal(h.harness.inspection.sdk.callsTo('threads.queuedMessages.create').length,1);assert.equal(h.harness.inspection.sdk.callsTo('threads.queuedMessages.delete').length,0);
 }finally{await h.harness.lifecycle.dispose();}
});
test('SQLite conversation survives plugin reload',async()=>{
 const h=fixture();try{
 await h.c.enable('root');await h.c.failure(makeTurnFailedEvent({threadId:'root',requestId:'request',errorInfo:{category:'unauthorized'}}));await h.c.step('root');
 let restored:ReturnType<typeof createCoordinator>|undefined;
 await h.harness.lifecycle.reload(bb=>{restored=createCoordinator(bb);});
 assert.equal(restored!.store.get('root')!.activeThreadId,'child');assert.equal(restored!.store.get('child')!.segments.length,2);
 await restored!.step('root');assert.equal(h.spawnCount,1);
 }finally{await h.harness.lifecycle.dispose();}
});
test('second unavailable provider advances to Antigravity and exhaustion stops',async()=>{
 const h=fixture();try{
 await h.c.enable('root');await h.c.failure(makeTurnFailedEvent({threadId:'root',requestId:'request',errorInfo:{category:'unauthorized'}}));await h.c.step('root');
 h.harness.sdk.stub('threads.get',async({threadId}:any)=>makeThreadResponse({id:threadId,projectId:'p',environmentId:'env',providerId:'codex',status:'error'}));
 h.harness.sdk.stub('providers.list',async()=>[{id:'acp-antigravity',available:true,capabilities:{permissionModes:['full']}}]);
 h.harness.sdk.stub('threads.spawn',async(args:any)=>makeThreadResponse({id:'gemini',projectId:'p',environmentId:'env',providerId:args.providerId,status:'idle'}));
 await h.c.failure(makeTurnFailedEvent({threadId:'child',requestId:'request',errorInfo:{category:'overloaded'}}));await h.c.step('root');
 assert.equal(h.c.store.get('root')!.activeThreadId,'gemini');assert.equal(h.c.store.get('root')!.segments[2]!.providerId,'acp-antigravity');
 await h.c.failure(makeTurnFailedEvent({threadId:'gemini',requestId:'request',errorInfo:{category:'overloaded'}}));await h.c.step('root');assert.equal(h.c.store.get('root')!.phase,'exhausted');
 }finally{await h.harness.lifecycle.dispose();}
});
test('disable during in-flight spawn is retained and prevents subsequent recovery',async()=>{
 const h=fixture();try{
 await h.c.enable('root');await h.c.failure(makeTurnFailedEvent({threadId:'root',requestId:'request',errorInfo:{category:'unauthorized'}}));
 let release!:(child:ReturnType<typeof makeThreadResponse>)=>void,entered!:()=>void;
 const started=new Promise<void>(resolve=>{entered=resolve;});
 h.harness.sdk.stub('threads.spawn',async()=>{entered();return new Promise<ReturnType<typeof makeThreadResponse>>(resolve=>{release=resolve;});});
 const task=h.c.step('root');await started;h.c.disable('root');release(makeThreadResponse({id:'late-child',providerId:'codex',status:'idle'}));await task;
 assert.equal(h.c.store.get('root')!.enabled,false);assert.equal(h.c.store.get('root')!.activeThreadId,'late-child');
 }finally{await h.harness.lifecycle.dispose();}
});

 test('explicit targets update connected chats while ordinary enable preserves configuration',async()=>{
 const h=fixture();try{
 await h.c.enable('root');
 const targets=[{providerId:'codex' as const,model:'gpt-6.2-sol',reasoningLevel:'high' as const}];
 await h.c.enable('root',targets);await h.c.enable('root');
 assert.deepEqual(h.c.store.get('root')!.targets,targets);
 assert.equal(h.c.store.get('root')!.segments.length,1);
 }finally{await h.harness.lifecycle.dispose();}
 });
