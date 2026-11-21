import type { BbPluginApi } from '@get-bb/plugin-sdk';
import { rpcContract } from './contract.js';
import { createCoordinator } from './src/coordinator.js';
export default function plugin(bb:BbPluginApi){
  const c=createCoordinator(bb),view=c.store.publicChat;
  bb.rpc.register(rpcContract,{
    list:()=>({chats:c.store.list().map(view)}),
    get:({threadId})=>{const r=c.store.get(threadId);return r?view(r):null;},
    enable:async({threadId,targets})=>view(await c.enable(threadId,targets)),
    disable:({threadId})=>view(c.disable(threadId)),
    recover:async({threadId})=>view(await c.recover(threadId)),
  });
  bb.cli.register({name:'conversation-fallback',summary:'Один чат с Claude → Codex → Antigravity',
    commands:['list','status','enable','disable','recover'].map(name=>({name,summary:name,usage:`bb conversation-fallback ${name} [thread-id] [--json]`})),
    async run(argv){const args=argv.filter(a=>a!=='--json'),[command,id]=args;
      try{
        if(command==='list')return{exitCode:0,stdout:JSON.stringify({chats:c.store.list().map(view)},null,2)};
        if(!id||args.length!==2)return{exitCode:1,stderr:'Usage: bb conversation-fallback list | status|enable|disable|recover <thread-id> [--json]'};
        const r=command==='enable'?await c.enable(id):command==='disable'?c.disable(id):command==='recover'?await c.recover(id):command==='status'?c.store.get(id):null;
        return r?{exitCode:0,stdout:JSON.stringify(view(r),null,2)}:{exitCode:1,stderr:'Неизвестная команда или тред не подключён.'};
      }catch(error){return{exitCode:1,stderr:error instanceof Error?error.message:'Ошибка команды.'};}
    },
  });
}
