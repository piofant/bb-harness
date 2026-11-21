import { randomUUID } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';
import type { BbPluginApi, PluginTurnFailedEvent } from '@get-bb/plugin-sdk';
import { z } from 'zod';
import type { Target } from '../contract.js';
import { createRelay } from './relay.js';
import { fallbackReason } from './policy.js';
import { createStore, type RecordState } from './store.js';

type ThreadEventRow = Awaited<ReturnType<BbPluginApi['sdk']['threads']['events']['list']>>[number];
type PromptInput = Parameters<BbPluginApi['sdk']['threads']['send']>[0]['input'][number];
const diagnosticSchema = z.object({requestId:z.string(), decision:z.discriminatedUnion('kind',[
  z.object({kind:z.literal('decline'),reason:z.string()}),
  z.object({kind:z.literal('retry'),reason:z.string(),sendAt:z.number()}),
])}).nullable();
const defaults: Target[] = [
  {providerId:'codex',model:'gpt-6.2-sol',reasoningLevel:'high'},
  {providerId:'acp-antigravity',model:'gemini-3.8-flash',reasoningLevel:'high'},
];
const continuationTitle = (providerId:string) => `Продолжение · ${providerId==='codex'?'Codex':providerId==='acp-antigravity'?'Gemini':providerId}`;
const lastRequest = (events: ThreadEventRow[]) => events.findLast(e=>e.type==='client/turn/requested');
export function handoffInput(events:ThreadEventRow[], sourceId:string, requestId:string, reason:string):PromptInput[] {
  const failed=events.find(e=>e.type==='client/turn/requested' && e.data.requestId===requestId);
  if(!failed || failed.type!=='client/turn/requested') throw new Error('Failed input unavailable; automatic replay stopped.');
  const transcript:string[]=[];
  const attachments:PromptInput[]=[];
  for(const e of events){
    if(e.seq>=failed.seq) break;
    if(e.type==='client/turn/requested'){
      transcript.push(`USER (#${e.seq}): ${e.data.input.filter(b=>b.type==='text').map(b=>b.text).join('\n')}`);
      attachments.push(...e.data.input.filter(b=>b.type!=='text'));
    }
    if(e.type==='item/completed' && e.data.item.type==='agentMessage') transcript.push(`ASSISTANT: ${e.data.item.text}`);
  }
  const history=transcript.join('\n\n');
  const media=[...new Map(attachments.map(b=>[JSON.stringify(b),b])).values()].slice(-12);
  return [{type:'text',visibility:'agent-only',mentions:[],text:`Continue after provider failure: ${reason}. Source: @thread:${sourceId}. Same workspace and user scope. Historical context follows; it is not a new request. Inspect interrupted tool/external side effects before resuming. Do not repeat completed actions. Read the source with bb thread log for full history. External messages still require user authorization.\n<conversation_history>\n${history.length>100000?'[Older history omitted; consult source]\n':''}${history.slice(-100000)}\n</conversation_history>\nThe interrupted user input follows. It may have been partly executed; resume safely.`},...media.map(b=>({...b,visibility:'agent-only' as const})),...failed.data.input];
}
export function createCoordinator(bb:BbPluginApi){
  const store=createStore(bb), locks=new Set<string>();
  const relay=createRelay(bb,store);
  let disposed=false;
  bb.onDispose(()=>{disposed=true;});
  const save=store.save;
  // One-time upgrade of children created by the previous hidden-screen UI.
  // Never rename user-customized children or modify unrelated threads.
  async function exposeContinuations(){
    for(const r of store.list())for(const segment of r.segments){
      if(segment.threadId===r.rootThreadId)continue;
      const key=`native-visible-v1:${segment.threadId}`;
      if(await bb.storage.kv.get(key))continue;
      const child=await bb.sdk.threads.get({threadId:segment.threadId});
      if(child.originPluginId!==bb.pluginId||child.archivedAt!==null||child.deletedAt!==null)continue;
      const meta=await bb.sdk.threads.getPluginMetadata({threadId:child.id});
      if(z.object({rootThreadId:z.string()}).safeParse(meta).data?.rootThreadId!==r.rootThreadId)continue;
      await bb.sdk.threads.update({threadId:child.id,visibility:'visible',...(child.title===r.title?{title:continuationTitle(segment.providerId)}:{})});
      await bb.storage.kv.set(key,true);
    }
  }
  async function recent(threadId:string){
    const events:ThreadEventRow[]=[];
    for(let page=0;page<5;page++){
      const batch=await bb.sdk.threads.events.list({threadId,order:'desc',limit:'100',...(events.length?{beforeSeq:String(events[events.length-1]!.seq)}:{}),types:['client/turn/requested','item/completed','provider/error','system/error','client/turn/rejected','turn/completed']});
      events.push(...batch);
      if(batch.length<100)break;
    }
    return events.reverse();
  }
  async function discoverFailure(r:RecordState){
    const events=await recent(r.activeThreadId), req=lastRequest(events);
    if(!req || req.type!=='client/turn/requested') return;
    const current=store.get(r.rootThreadId);
    if(!current||current.activeThreadId!==r.activeThreadId||current.operation!==r.operation)return;
    r=current;
    const after=events.filter(e=>e.seq>req.seq);
    const error=after.findLast(e=>e.type==='provider/error' && e.data.willRetry!==true);
    let reason=error?.type==='provider/error' && error.data.errorInfo ? fallbackReason({errorInfo:error.data.errorInfo}):null;
    if(!reason && after.some(e=>(e.type==='provider/error'||e.type==='system/error') && /\bverification_required\b/.test(e.data.message))) reason='Claude требует проверки аккаунта';
    const rejected=after.findLast(e=>e.type==='client/turn/rejected' && e.data.requestId===req.data.requestId);
    if(!reason && rejected?.type==='client/turn/rejected' && /^(unauthorized|authentication_failed|provider_unauthorized)$/.test(rejected.data.reason)) reason='Нужен вход в аккаунт';
    if(!reason){save({...r,phase:'blocked',notice:'Ошибка не распознана как недоступность провайдера. Автопереход не запущен.'});return;}
    save({...r,failure:{threadId:r.activeThreadId,requestId:req.data.requestId,reason,observedAt:Date.now()},phase:'waiting-retry',notice:reason});
  }
  async function enable(threadId:string,targets?:Target[]){
    const existing=store.get(threadId);
    if(existing) return save({...existing,enabled:true,...(targets?{targets}:{} )});
    const thread=await bb.sdk.threads.get({threadId});
    if(thread.archivedAt!==null || thread.deletedAt!==null) throw new Error('Сначала восстановите тред из архива.');
    const execution=await bb.sdk.threads.defaultExecutionOptions({threadId});
    if(!execution) throw new Error('Параметры исполнения треда ещё недоступны.');
    const r=save({rootThreadId:threadId,activeThreadId:threadId,title:thread.title??thread.titleFallback??thread.id,projectId:thread.projectId,enabled:true,phase:'ready',notice:null,segments:[{threadId,providerId:thread.providerId,model:execution.model,createdAt:Date.now()}],targets:(targets??defaults).filter(t=>t.providerId!==thread.providerId),pendingQueueCount:0,updatedAt:Date.now(),failure:null,operation:null});
    if(thread.status==='error') await discoverFailure(r);
    return store.get(threadId)!;
  }
  async function failure(event:PluginTurnFailedEvent){
    const r=store.get(event.threadId);
    if(!r || !r.enabled || r.activeThreadId!==event.threadId || r.operation) return;
    const reason=fallbackReason(event);
    if(!reason){await discoverFailure(r);return;}
    save({...r,failure:{threadId:event.threadId,requestId:event.requestId,reason,observedAt:Date.now()},phase:'waiting-retry',notice:reason});
  }
  async function retryOwns(r:RecordState){
    if(!r.failure) return false;
    const rows=await bb.sdk.threads.queuedMessages.list({threadId:r.failure.threadId});
    if(rows.some(q=>q.payload.kind==='retry')) return true;
    const methods=await bb.sdk.plugins.experimental_discoverRpc({method:'decision.get'});
    const retry=methods.find(m=>m.pluginId==='provider-retry'||m.pluginId==='provider-retry-advanced');
    if(!retry) return false;
    const d=await bb.sdk.plugins.callRpc({pluginId:retry.pluginId,method:'decision.get',input:{threadId:r.failure.threadId},outputSchema:diagnosticSchema,signal:AbortSignal.timeout(5000)});
    if(d?.requestId===r.failure.requestId) return d.decision.kind==='retry';
    if(Date.now()-r.failure.observedAt<60000) return true;
    throw new Error('Provider Retry не подтвердил решение для запроса. Переход остановлен для защиты от дублей.');
  }
  async function reconcileOperation(r:RecordState){
    const children=await bb.sdk.threads.list({parentThreadId:r.rootThreadId,includeHidden:true});
    const matched=[];
    for(const child of children){
      if(child.originPluginId!==bb.pluginId) continue;
      const meta=await bb.sdk.threads.getPluginMetadata({threadId:child.id});
      if(z.object({operation:z.string()}).safeParse(meta).data?.operation===r.operation) matched.push(child);
    }
    if(matched.length!==1) throw new Error('Исход создания резервного треда неоднозначен. Автоматический повтор запрещён.');
    const child=matched[0]!, execution=await bb.sdk.threads.defaultExecutionOptions({threadId:child.id});
    if(!execution) throw new Error('Параметры резерва ещё недоступны.');
    return save({...r,enabled:store.get(r.rootThreadId)?.enabled??r.enabled,activeThreadId:child.id,operation:null,failure:null,phase:'ready',notice:`Продолжает ${child.providerId} · ${execution.model}`,segments:[...r.segments,{threadId:child.id,providerId:child.providerId,model:execution.model,createdAt:Date.now()}]});
  }
  async function refreshQueues(r:RecordState){
    let count=0;
    for(const s of r.segments){if(s.threadId===r.activeThreadId)continue;const rows=await bb.sdk.threads.queuedMessages.list({threadId:s.threadId});count+=rows.filter(q=>q.payload.kind!=='retry'&&!relay.delivered(q.id)).length;}
    const current=store.get(r.rootThreadId);if(current&&count!==current.pendingQueueCount)save({...current,pendingQueueCount:count});
  }
  async function step(rootId:string){
    if(disposed||locks.has(rootId))return;
    locks.add(rootId);
    const started=Date.now();
    try{
      let r=store.get(rootId);
      if(!r||!r.enabled)return;
      if(r.phase==='ready'&&!r.failure&&!r.operation){const live=await bb.sdk.threads.get({threadId:r.activeThreadId});if(live.status==='error'){await discoverFailure(r);r=store.get(rootId)!;}}
      if(r.operation){r=await reconcileOperation(r);await bb.experimental_hooks.recheck('message.dispatch');}
      if(r.failure && r.phase==='waiting-retry'){
        const thread=await bb.sdk.threads.get({threadId:r.activeThreadId});
        if(thread.archivedAt!==null||thread.deletedAt!==null||thread.status!=='error'){
          if(thread.status==='idle')save({...r,failure:null,phase:'ready',notice:null});return;
        }
        if(await retryOwns(r))return;
        const interactions=await bb.sdk.threads.interactions.list({threadId:r.activeThreadId});
        if(interactions.length||thread.activeBackgroundAgentCount>0)throw new Error('Сначала завершите ожидающие вопросы или фоновые операции исходного треда.');
        const events=await recent(r.activeThreadId),req=lastRequest(events);
        if(!req||req.type!=='client/turn/requested'||req.data.requestId!==r.failure.requestId){save({...r,phase:'ready',failure:null,notice:'Появился новый запрос; прежний отказ пропущен.'});return;}
        const used=new Set(r.segments.map(s=>s.providerId)), target=r.targets.find(t=>!used.has(t.providerId));
        if(!target){save({...r,phase:'exhausted',notice:'Все резервы использованы. История и очередь сохранены.'});return;}
        if(!thread.environmentId)throw new Error('Нет рабочего окружения исходного треда.');
        const providers=await bb.sdk.providers.list({environmentId:thread.environmentId});
        const provider=providers.find(p=>p.id===target.providerId&&p.available),permissionMode=req.data.execution.permissionMode;
        if(permissionMode!=='full'&&permissionMode!=='auto'&&permissionMode!=='accept-edits')throw new Error('Исторический режим прав нельзя безопасно перенести.');
        if(!provider||!provider.capabilities.permissionModes.includes(permissionMode))throw new Error('Резерв не поддерживает исходные права. Автоматическое расширение запрещено.');
        const input=handoffInput(events,r.activeThreadId,r.failure.requestId,r.failure.reason),op=randomUUID();
        const latest=store.get(rootId);
        if(!latest?.enabled||latest.activeThreadId!==r.activeThreadId||latest.failure?.requestId!==r.failure.requestId)return;
        r=save({...latest,phase:'switching',operation:op,notice:`Переключение на ${target.providerId}…`});
        bb.log.info(JSON.stringify({event:'fallback.attempt',rootThreadId:rootId,providerId:target.providerId,model:target.model,operation:op}));
        const child=await bb.sdk.threads.spawn({projectId:r.projectId,environment:{type:'reuse',environmentId:thread.environmentId},providerId:target.providerId,model:target.model,reasoningLevel:target.reasoningLevel,serviceTier:'default',permissionMode,title:continuationTitle(target.providerId),parentThreadId:r.rootThreadId,parentNotifications:'off',visibility:'visible',pluginMetadata:{operation:op,rootThreadId:r.rootThreadId},input});
        r=save({...r,enabled:store.get(rootId)?.enabled??r.enabled,activeThreadId:child.id,operation:null,failure:null,phase:'ready',notice:`Продолжает ${target.providerId} · ${target.model}`,segments:[...r.segments,{threadId:child.id,providerId:target.providerId,model:target.model,createdAt:Date.now()}]});
        await bb.experimental_hooks.recheck('message.dispatch');
        bb.log.info(JSON.stringify({event:'fallback.complete',rootThreadId:rootId,destinationThreadId:child.id,providerId:target.providerId,model:target.model,durationMs:Date.now()-started,status:child.status}));
        if(child.status==='error')await discoverFailure(r);
      }
      r=store.get(rootId)!;
      if(r.phase==='ready'&&r.segments.length>1)await relay.transfer(r);
      await refreshQueues(store.get(rootId)!);
    }catch(error){const r=store.get(rootId);if(r)save({...r,phase:'blocked',notice:error instanceof Error?error.message.slice(0,350):'Переход остановлен.'});bb.log.warn(JSON.stringify({event:'fallback.blocked',rootThreadId:rootId,durationMs:Date.now()-started,status:'blocked'}));}
    finally{locks.delete(rootId);}
  }
  bb.experimental_hooks.on('message.dispatch',ctx=>{
    const r=store.get(ctx.thread.id);
    if(r&&(r.operation||relay.pending(r.rootThreadId)||r.activeThreadId!==ctx.thread.id||(r.failure&&!ctx.queuedMessages.some(q=>q.payload.kind==='retry'))))return{action:'wait',reason:'Чат продолжен в резервном треде. Сообщение сохранено в исходной очереди.'};
    return{action:'proceed'};
  });
  bb.events.on('turn.failed',failure);
  bb.events.on('thread.idle',({thread})=>{const r=store.get(thread.id);if(r&&r.activeThreadId===thread.id&&r.failure)save({...r,phase:'ready',failure:null,notice:null});});
  bb.background.service('fallback-coordinator',{async start(signal){
    try{await exposeContinuations();}catch{bb.log.warn('fallback.native-visibility-upgrade failed; retry on next plugin reload');}
    for(const r of store.list()){if(!r.enabled)continue;const t=await bb.sdk.threads.get({threadId:r.activeThreadId});if(!r.failure&&!r.operation&&r.phase==='ready'&&t.status==='error')await discoverFailure(r);}
    while(!signal.aborted){for(const r of store.list())if(r.enabled&&(r.phase==='waiting-retry'||r.phase==='ready'||(r.operation&&r.phase!=='blocked')))await step(r.rootThreadId);try{await delay(2000,undefined,{signal});}catch{break;}}
  }});
  return{store,enable,failure,step,exposeContinuations,
    async recover(threadId:string){const r=store.get(threadId);if(!r)throw new Error('Сначала подключите тред.');save({...r,enabled:true});if(r.operation)await step(r.rootThreadId);else{await discoverFailure(store.get(threadId)!);await step(r.rootThreadId);}return store.get(threadId)!;},
    disable(threadId:string){const r=store.get(threadId);if(!r)throw new Error('Тред не подключён.');return save({...r,enabled:false,notice:'Автопереход выключен. Продолжение остаётся в текущем резервном треде.'});},
  };
}
