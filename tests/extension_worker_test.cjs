// Protocol test with a simulated Chrome API; this does not drive the user's browser.
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
let listener,eventListener,detachListener;
let streamAlreadyActive=false;
const calls=[];const sockets=[];
class Socket {
 static OPEN=1;
 constructor(){this.readyState=1;sockets.push(this);setImmediate(()=>this.onopen());}
 send(s){const m=JSON.parse(s);this.sent.push(m);if(m.type==='hello')setImmediate(()=>this.onmessage({data:JSON.stringify({type:'ready'})}));}
 sent=[];
 close(){this.readyState=3;this.onclose?.({code:1000});}
}
let tabURL='https://example.com';
let observation={title:'Fixture',url:'https://example.com',text:'fixture',elements:[],width:1440,height:900};
const chrome={
 runtime:{id:'a'.repeat(32),onMessage:{addListener:f=>listener=f},onStartup:{addListener:()=>{}}},
 tabs:{query:async()=>[{id:1,url:tabURL,title:'Fixture',active:true},{id:2,url:'https://other.example',title:'Other'}],create:async(params)=>{tabURL=params.url;return {id:3,url:tabURL}},get:async()=>({url:tabURL,status:'complete'}),update:async(id,params)=>{tabURL=params.url}},
 storage:{local:{get:async()=>({autoConnect:false}),set:async()=>{}}},
 debugger:{getTargets:async()=>[],attach:async()=>{},detach:async()=>{},onEvent:{addListener:f=>eventListener=f},onDetach:{addListener:f=>detachListener=f},
 sendCommand:async(target,method,params)=>{calls.push({target,method,params});
 if(method==='Runtime.evaluate')return {result:{value:params.expression==='location.href'?observation.url:params.expression.includes('const elements=')?observation:params.expression.includes('return {x,y,tag')?{x:100,y:80,tag:'INPUT'}:undefined}};
 if(method==='Page.startScreencast'&&streamAlreadyActive)throw new Error('Screencast is already active');
 if(method==='Page.getLayoutMetrics')return {cssVisualViewport:{clientWidth:1440,clientHeight:900}};
 if(method==='Page.captureScreenshot')return {data:'/9g='};return {};}}
};
vm.runInNewContext(fs.readFileSync('extension/worker.js','utf8'),{chrome,WebSocket:Socket,URL,Date,Promise,setTimeout:(fn,ms)=>setTimeout(fn,ms===3000?5:ms),clearTimeout,setInterval,clearInterval});
const popup=m=>new Promise(resolve=>listener(m,{id:chrome.runtime.id},resolve));
const command=async(action,extra={})=>{
 const ws=sockets.at(-1),id=String(Math.random());ws.onmessage({data:JSON.stringify({type:'command',id,action,...extra})});
 for(let n=0;n<100;n++){const result=ws.sent.find(m=>m.id===id);if(result)return result;await new Promise(r=>setTimeout(r,1));}
 throw new Error('command not answered');
};
(async()=>{
 assert.ok((await popup({type:'connect',server:'http://remote.example',token:'test',tabId:1})).error);
 assert.match((await popup({type:'connect',server:'http://127.0.0.1:8000',token:'test',tabId:1})).status,/已连接/);
 assert.equal((await command('read')).result.width,1440);
 tabURL=undefined;assert.equal((await command('read')).result.title,'Fixture');
 assert.ok((await command('screenshot')).result.data);
 streamAlreadyActive=true;assert.ok((await command('stream_start')).result);streamAlreadyActive=false;
 tabURL='about:blank';assert.ok((await command('read')).error);
 assert.equal((await command('open',{url:'https://example.com'})).result.title,'Fixture');
 await command('fill',{ref:0,text:'中文'});
 assert.ok(calls.some(c=>c.method==='Input.insertText'&&c.params.text==='中文'));
 await command('manual',{input:{action:'down',x:42,y:99,button:'left'}});
 await command('manual',{input:{action:'move',x:100,y:99}});
 const move=calls.filter(c=>c.method==='Input.dispatchMouseEvent').at(-1);
 assert.equal(move.params.buttons,1);
 await command('manual',{input:{action:'up',x:100,y:99,button:'left'}});
 await command('stream_start');
 await eventListener({tabId:1},'Page.screencastFrame',{sessionId:1,data:'/9g='});
 assert.ok(sockets.at(-1).sent.some(m=>m.type==='frame'&&m.width===1440&&m.height===900));
 const detachedSocket=sockets.at(-1);
 detachListener({tabId:1},'target_closed');
 await new Promise(r=>setTimeout(r,1));
 assert.match((await popup({type:'status'})).status,/网页已关闭/);
 assert.equal(sockets.at(-1),detachedSocket);
 assert.equal((await command('read')).result.title,'Fixture');
 assert.equal((await command('list_tabs')).result.tabs.length,2);
 assert.equal((await command('switch_tab',{ref:2})).result.title,'Fixture');
 assert.ok(calls.some(c=>c.method==='Page.enable'&&c.target.tabId===2));

 // A late old command must never reply on a replacement connection.
 assert.match((await popup({type:'connect',server:'http://127.0.0.1:8000',token:'test',tabId:1})).status,/已连接/);
 const previous=sockets.at(-1),commandId='late-old-command';
 previous.onmessage({data:JSON.stringify({type:'command',id:commandId,action:'wait',amount:30})});
 await new Promise(r=>setTimeout(r,1));
 previous.close();
 const replacement=await popup({type:'connect',server:'http://127.0.0.1:8000',token:'test',tabId:1});
 assert.match(replacement.status,/已连接/);
 assert.ok(!sockets.at(-1).sent.some(m=>m.id===commandId));
 previous.onmessage({data:JSON.stringify({type:'ready'})});
 assert.match((await popup({type:'status'})).status,/已连接/);
 // Retry temporary competing-control rejection without dropping tab authorization.
 const busy=sockets.at(-1);busy.readyState=3;busy.onclose({code:1013});
 await new Promise(r=>setTimeout(r,20));
 assert.notEqual(sockets.at(-1),busy);
 assert.match((await popup({type:'status'})).status,/已连接/);
 const rejected=sockets.at(-1),count=sockets.length;
 rejected.readyState=3;rejected.onclose({code:1008});
 await new Promise(r=>setTimeout(r,20));
 assert.equal(sockets.length,count);
 assert.match((await popup({type:'status'})).status,/拒绝/ );
 await popup({type:'disconnect'});
 console.log('Extension worker protocol, input, stream and detach tests passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
