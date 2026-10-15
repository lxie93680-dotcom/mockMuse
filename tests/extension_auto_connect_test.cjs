// Simulated Chrome lifecycle, including metadata hidden without activeTab.
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const source=fs.readFileSync('extension/worker.js','utf8');
const tick=()=>new Promise(r=>setTimeout(r,5));
async function fixture(settings={},initial=[{id:1,url:'https://one.example',active:false},{id:2,url:'https://two.example',active:true},{id:3,url:'chrome://extensions',active:false}]){
 let tabs=initial,listener,detachListener;const sockets=[],attached=new Set(),calls=[];
 class Socket{static OPEN=1;sent=[];constructor(){this.readyState=1;sockets.push(this);setImmediate(()=>this.onopen());}send(s){const m=JSON.parse(s);this.sent.push(m);if(m.type==='hello')setImmediate(()=>this.onmessage({data:JSON.stringify({type:'ready'})}));}close(){this.readyState=3;this.onclose?.({code:1000});}}
 const chrome={runtime:{id:'a'.repeat(32),onMessage:{addListener:f=>listener=f},onStartup:{addListener:()=>{}}},
  storage:{local:{get:async()=>settings,set:async v=>Object.assign(settings,v)}},
  tabs:{query:async()=>tabs.map(({id,active})=>({id,active})),get:async id=>{if(!tabs.some(t=>t.id===id))throw Error('closed');return {id,status:'complete'};},update:async(id,p)=>Object.assign(tabs.find(t=>t.id===id),p),create:async p=>{const t={id:4,...p};tabs.push(t);return t;}},
  debugger:{getTargets:async()=>tabs.map(t=>({...t,tabId:t.id,attached:attached.has(t.id)})),attach:async t=>{attached.add(t.tabId);calls.push(['attach',t.tabId]);},detach:async t=>{attached.delete(t.tabId);detachListener(t,'canceled_by_user');},
   onEvent:{addListener:()=>{}},onDetach:{addListener:f=>detachListener=f},sendCommand:async(t,m,p)=>{if(!attached.has(t.tabId))throw Error('not attached');if(m==='Runtime.evaluate'){const tab=tabs.find(x=>x.id===t.tabId);return {result:{value:p.expression==='location.href'?tab.url:{title:'Fixture',url:tab.url,elements:[],width:100,height:100}}};}return {};}}
 };
 const boot=()=>vm.runInNewContext(source,{chrome,WebSocket:Socket,URL,Date,Promise,setTimeout,clearTimeout,setInterval,clearInterval});
 const popup=m=>new Promise(r=>listener(m,{id:chrome.runtime.id},r));
 const command=async(action,extra={})=>{const ws=sockets.at(-1),id=Math.random().toString();ws.onmessage({data:JSON.stringify({type:'command',id,action,...extra})});for(let i=0;i<100;i++){const result=ws.sent.find(m=>m.id===id);if(result)return result;await tick();}throw Error('no response');};
 boot();await tick();return {settings,sockets,attached,calls,popup,command,boot,close:async id=>{tabs=tabs.filter(t=>t.id!==id);attached.delete(id);detachListener({tabId:id},'target_closed');await tick();},cancel:async id=>{attached.delete(id);detachListener({tabId:id},'canceled_by_user');await tick();}};
}
(async()=>{
 const f=await fixture();assert.equal(f.sockets.length,1,'worker load automatically connects');
 const list=(await f.command('list_tabs')).result.tabs;assert.deepEqual(list.map(t=>t.id),[1,2]);
 assert.ok((await f.command('read')).result);assert.ok(f.attached.has(2));
 assert.ok((await f.command('switch_tab',{ref:1})).result);assert.ok(f.attached.has(1));assert.ok(!f.attached.has(2));
 assert.ok((await f.command('switch_tab',{ref:3})).error);assert.ok(f.attached.has(1),'unsupported target preserves current page');
 await f.close(1);assert.ok((await f.command('read')).result);assert.ok(f.attached.has(2));assert.equal(f.sockets.length,1);
 await f.cancel(2);assert.equal(f.settings.autoConnect,false);assert.equal(f.sockets.at(-1).readyState,3);
 const count=f.sockets.length;f.boot();await tick();assert.equal(f.sockets.length,count,'user cancellation survives worker restart');
 assert.ok((await f.popup({type:'connect',server:'http://127.0.0.1:8000',tabId:3})).status.includes('已连接'));
 assert.equal(f.settings.autoConnect,true);await f.popup({type:'disconnect'});
 const empty=await fixture({},[{id:3,url:'chrome://newtab',active:true}]);
 assert.equal(empty.sockets.length,1);assert.ok((await empty.command('open',{url:'https://new.example'})).result);assert.ok(empty.attached.has(4));
 await empty.popup({type:'disconnect'});
 console.log('Automatic connection, all-page metadata, tab switch/closure, new page and persistent user stop tests passed');
})().catch(e=>{console.error(e);process.exit(1);});
