import type { BbPluginApi } from '@get-bb/plugin-sdk';
import { z } from 'zod';
import type { Store, RecordState } from './store.js';
// Originals remain held as a durable archive. An ambiguous create is never replayed.
export function createRelay(bb:BbPluginApi,store:Store){
  const pending=(rootId:string)=>Boolean(store.db.prepare("SELECT source_id FROM relays WHERE root_id=? AND status!='delivered' LIMIT 1").get(rootId));
  async function transfer(r:RecordState){
    for(const segment of r.segments){
      if(segment.threadId===r.activeThreadId)continue;
      const rows=await bb.sdk.threads.queuedMessages.list({threadId:segment.threadId});
      for(const row of rows){
        if(!store.get(r.rootThreadId)?.enabled)return;
        if(row.payload.kind==='retry')continue;
        const saved=z.object({status:z.string(),snapshot:z.string()}).optional().parse(store.db.prepare('SELECT status,snapshot FROM relays WHERE source_id=?').get(row.id));
        if(saved){
          if(saved.status!=='delivered')throw new Error('Исход переноса очереди неоднозначен. Оригинал сохранён; автоматический повтор остановлен.');
          if(JSON.stringify(JSON.parse(saved.snapshot).content)!==JSON.stringify(row.content))throw new Error('Сообщение в исходной очереди изменено после переноса. Проверьте обе очереди.');
          continue;
        }
        if(row.groupWithNext)throw new Error('Группа в исходной очереди сохранена. Перед продолжением разделите группу: автоматический перенос группы пока недоступен.');
        const execution=await bb.sdk.threads.defaultExecutionOptions({threadId:r.activeThreadId});
        if(!execution)throw new Error('Нет параметров исполнения для переноса очереди.');
        if(row.permissionMode&&row.permissionMode!==execution.permissionMode)throw new Error('Права сообщения очереди отличаются от резервного треда; оригинал сохранён.');
        store.db.prepare('INSERT INTO relays VALUES (?,?,?,?,?)').run(row.id,r.rootThreadId,null,JSON.stringify({content:row.content,originalSendAt:row.sendAt}),'creating');
        // Persistently park the original before copying: uninstall must not replay it.
        await bb.sdk.threads.queuedMessages.update({threadId:segment.threadId,queuedMessageId:row.id,expectedUpdatedAt:row.updatedAt,sendAt:253402300799000});
        const copy=await bb.sdk.threads.queuedMessages.create({threadId:r.activeThreadId,input:row.content,model:execution.model,reasoningLevel:execution.reasoningLevel,permissionMode:execution.permissionMode,...(row.senderThreadId?{senderThreadId:row.senderThreadId}:{})});
        store.db.prepare("UPDATE relays SET destination_id=?,status='created' WHERE source_id=?").run(copy.id,row.id);
        if(row.sendAt!==null)await bb.sdk.threads.queuedMessages.update({threadId:r.activeThreadId,queuedMessageId:copy.id,expectedUpdatedAt:copy.updatedAt,sendAt:row.sendAt});
        const durable=await bb.sdk.threads.queuedMessages.list({threadId:r.activeThreadId});
        if(!durable.some(q=>q.id===copy.id&&JSON.stringify(q.content)===JSON.stringify(row.content)&&q.sendAt===row.sendAt))throw new Error('Не подтверждена запись очереди в резерв. Оригинал сохранён.');
        store.db.prepare("UPDATE relays SET status='delivered' WHERE source_id=?").run(row.id);
      }
    }
    await bb.experimental_hooks.recheck('message.dispatch');
  }
  const delivered=(sourceId:string)=>Boolean(store.db.prepare("SELECT source_id FROM relays WHERE source_id=? AND status='delivered'").get(sourceId));
  return{pending,transfer,delivered};
}
