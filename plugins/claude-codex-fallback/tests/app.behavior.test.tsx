// @vitest-environment jsdom
import {it,expect} from 'vitest';
import {act,fireEvent,waitFor} from '@testing-library/react';
import {loadPluginApp,renderSlot} from '@get-bb/plugin-sdk/testing/app';
import type {Chat} from '../contract';
const chat:Chat={rootThreadId:'root',title:'Test chat',projectId:'p',activeThreadId:'root',enabled:true,phase:'ready',notice:null,segments:[{threadId:'root',providerId:'claude-code',model:'claude',createdAt:1}],targets:[],pendingQueueCount:0,updatedAt:1};
const continued:Chat={...chat,activeThreadId:'child',segments:[...chat.segments,{threadId:'child',providerId:'codex',model:'gpt-6.1-sol',createdAt:2}]};
it.each(['root','child'])('opening an existing connected native thread %s keeps that thread open',async(threadId)=>{
 const app=await loadPluginApp(()=>import('../app'));
 const view=renderSlot(app.appOverlays[0]!,{},{context:{threadId},rpc:{get:()=>continued}});
 try{await act(async()=>{});expect(view.inspection.navigateCalls).toEqual([]);}finally{view.lifecycle.unmount();}
});
it('handoff while source is open automatically follows its new native continuation',async()=>{
 const app=await loadPluginApp(()=>import('../app'));let current=chat;
 const view=renderSlot(app.appOverlays[0]!,{},{context:{threadId:'root'},rpc:{get:()=>current}});
 try{
  await act(async()=>{});expect(view.inspection.navigateCalls).toEqual([]);
  current=continued;await view.behavior.emitRealtime('changed',{});
  await waitFor(()=>expect(view.inspection.navigateCalls).toEqual([{method:'toThread',threadId:'child'}]));
  await view.behavior.emitRealtime('changed',{});expect(view.inspection.navigateCalls).toHaveLength(1);
 }finally{view.lifecycle.unmount();}
});
it('late lookup cannot navigate after route owner unmounts',async()=>{
 const app=await loadPluginApp(()=>import('../app'));let resolve!:(value:Chat)=>void;
 const pending=new Promise<Chat>(r=>{resolve=r;});
 const view=renderSlot(app.appOverlays[0]!,{},{context:{threadId:'root'},rpc:{get:()=>pending}});
 view.lifecycle.unmount();await act(async()=>{resolve(continued);await pending;});expect(view.inspection.navigateCalls).toEqual([]);
});
it('unconnected threads and failed lookups stay native',async()=>{
 const app=await loadPluginApp(()=>import('../app'));let fail=false;
 const view=renderSlot(app.appOverlays[0]!,{},{context:{threadId:'root'},rpc:{get:()=>{if(fail)throw new Error('offline');return null;}}});
 try{await act(async()=>{});fail=true;await view.behavior.emitRealtime('changed',{});expect(view.inspection.navigateCalls).toEqual([]);}finally{view.lifecycle.unmount();}
});
it('legacy plugin chat link opens the native continuation instead of a second chat UI',async()=>{
 const app=await loadPluginApp(()=>import('../app'));
 const view=renderSlot(app.navPanels[0]!,{subPath:'root'},{rpc:{get:()=>continued}});
 try{await waitFor(()=>expect(view.inspection.navigateCalls).toEqual([{method:'toThread',threadId:'child'}]));expect(view.queryByTestId('bb-thread-chat')).toBeNull();}finally{view.lifecycle.unmount();}
});
it('successful connection opens native thread and errors remain visible',async()=>{
 const app=await loadPluginApp(()=>import('../app'));let fail=true;
 const view=renderSlot(app.navPanels[0]!,{subPath:'root'},{rpc:{get:()=>null,enable:()=>{if(fail)throw new Error('connect unavailable');return chat;}}});
 try{
  const button=await view.findByRole('button',{name:'Подключить этот тред'});await waitFor(()=>expect((button as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(button);await view.findByRole('alert');expect(view.getByRole('alert').textContent).toContain('connect unavailable');
  fail=false;fireEvent.click(button);await waitFor(()=>expect(view.inspection.navigateCalls).toEqual([{method:'toThread',threadId:'root'}]));
 }finally{view.lifecycle.unmount();}
});
it('source banner links directly to continuation without technical status panels',async()=>{
 const app=await loadPluginApp(()=>import('../app'));
 const view=renderSlot(app.composerCustomizations[0]!.banners![0]!,{},{composer:{scope:{kind:'thread',threadId:'root'}},rpc:{get:()=>continued}});
 try{
  await view.findByText('Этот чат продолжен на Codex');fireEvent.click(view.getByRole('button',{name:'Открыть продолжение →'}));
  expect(view.inspection.navigateCalls).toEqual([{method:'toThread',threadId:'child'}]);expect(view.queryByText(/Fallback-чат|Активный сегмент|Открыть единый чат/)).toBeNull();
 }finally{view.lifecycle.unmount();}
});
it('active continuation has no extra composer banner',async()=>{
 const app=await loadPluginApp(()=>import('../app'));
 const view=renderSlot(app.composerCustomizations[0]!.banners![0]!,{},{composer:{scope:{kind:'thread',threadId:'child'}},rpc:{get:()=>continued}});
 try{await act(async()=>{});expect(view.container.textContent).toBe('');}finally{view.lifecycle.unmount();}
});
it('blocked continuation exposes recovery and its reason',async()=>{
 const app=await loadPluginApp(()=>import('../app'));
 const view=renderSlot(app.composerCustomizations[0]!.banners![0]!,{},{composer:{scope:{kind:'thread',threadId:'child'}},rpc:{get:()=>({...continued,phase:'blocked',notice:'permission mismatch'}),recover:()=>({...continued,activeThreadId:'gemini'})}});
 try{
  await view.findByText('Автопереход остановлен');expect(view.getByText('permission mismatch')).toBeDefined();
  fireEvent.click(view.getByRole('button',{name:'Повторить переход'}));await waitFor(()=>expect(view.inspection.navigateCalls).toEqual([{method:'toThread',threadId:'gemini'}]));
 }finally{view.lifecycle.unmount();}
});
