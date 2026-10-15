let socket=null, tabId=null, status='未连接', heartbeat=null, reconnect=null;
let credentials=null, stopped=true, chain=Promise.resolve(), buttons=0;
let width=1280,height=800,url='',lastFrame=0,metricsAt=0;
const sendOn=(ws,m)=>{if(ws===socket&&ws?.readyState===WebSocket.OPEN) ws.send(JSON.stringify(m));};
const send=m=>sendOn(socket,m);
const cdp=(method,params={})=>chrome.debugger.sendCommand({tabId},method,params);
let attached=false, streaming=false;
const eligible=tab=>/^https?:\/\//.test(tab?.url||'');
async function browserTabs(){
 const tabs=await chrome.tabs.query({}),targets=await chrome.debugger.getTargets();
 return tabs.map(t=>{const d=targets.find(d=>d.tabId===t.id);return {...t,url:t.url||d?.url,title:t.title||d?.title||''};});
}
async function target(id){
 const tab=(await browserTabs()).find(t=>t.id===id);httpURL(tab?.url);
 if(tabId===id&&attached)return;
 const old=tabId;tabId=null;attached=false;
 if(old!==null)try{await chrome.debugger.detach({tabId:old});}catch{}
 try{await chrome.debugger.attach({tabId:id},'1.3');tabId=id;attached=true;await cdp('Page.enable');
  if(streaming)await cdp('Page.startScreencast',{format:'jpeg',quality:70,everyNthFrame:1});
  metricsAt=0;lastFrame=0;buttons=0;
 }catch(e){tabId=null;attached=false;throw e;}
}
async function ensureTarget(){
 if(tabId!==null&&attached)return;
 const tabs=await browserTabs();
 const tab=tabs.find(t=>t.active&&eligible(t))||tabs.find(eligible);
 if(!tab)throw new Error('Chrome 中没有可操作网页，请打开 HTTP/HTTPS 网页');
 await target(tab.id);
}
async function resume(){
 const settings=await chrome.storage.local.get(['server','autoConnect']);
 if(settings.autoConnect===false)return;
 const server=httpURL(settings.server||'http://127.0.0.1:8000');
 if(!['127.0.0.1','localhost','[::1]'].includes(server.hostname))throw new Error('自动连接仅支持本机工作台');
 // Recover a debugger target retained across a service-worker restart.
 const targets=await chrome.debugger.getTargets();
 const existing=targets.find(t=>t.attached&&t.tabId&&/^https?:\/\//.test(t.url||''));
 if(existing){try{await chrome.debugger.sendCommand({tabId:existing.tabId},'Page.enable');tabId=existing.tabId;attached=true;}catch{}}
 credentials={server:server.origin};stopped=false;await connectSocket();
}

function httpURL(value){if(typeof value!=='string'||!value)throw new Error('受控标签页地址不可用，请打开网页或重新连接Muse扩展');let u;try{u=new URL(value);}catch{throw new Error('受控标签页地址无效，请重新连接Muse扩展');}if(!['http:','https:'].includes(u.protocol))throw new Error('只支持 HTTP/HTTPS 页面，请先打开目标网页');return u;}
async function evaluate(expression){
  const r=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
  if(r.exceptionDetails){const d=r.exceptionDetails;const description=d.exception?.description||d.text||'页面操作失败';const location=d.url?` (${d.url}:${(d.lineNumber||0)+1})`:'';throw new Error(description+location);}
  return r.result.value;
}
async function currentURL(){
 const tab=await chrome.tabs.get(tabId);
 // activeTab may no longer expose tab.url after cross-origin navigation.
 const value=tab.url || await evaluate('location.href');
 httpURL(value);return value;
}
async function metrics(){
  const m=await cdp('Page.getLayoutMetrics');const v=m.cssVisualViewport;
  width=Math.round(v.clientWidth);height=Math.round(v.clientHeight);
  url=await currentURL();metricsAt=Date.now();
  return {width,height,url};
}
const observation=`(()=>{
 document.querySelectorAll('[data-muse-ref]').forEach(e=>e.removeAttribute('data-muse-ref'));
 const semantic=[...document.querySelectorAll('a,button,input,textarea,select,li,[role="button"],[role="option"],[tabindex],[contenteditable="true"]')];
 const icons=[...document.querySelectorAll('div,span,i,em')].filter(e=>getComputedStyle(e).cursor==='pointer' && ![...e.children].some(c=>getComputedStyle(c).cursor==='pointer'));
 const elements=[...new Set([...semantic,...icons])]
 .filter(e=>e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})&&e.getBoundingClientRect().width&&e.getBoundingClientRect().height)
 .slice(0,120).map((e,i)=>{e.setAttribute('data-muse-ref',String(i));const r=e.getBoundingClientRect();return {ref:i,tag:e.tagName.toLowerCase(),x:r.x+r.width/2,y:r.y+r.height/2,context:(e.parentElement?.innerText||'').slice(0,160),text:(e.innerText||e.getAttribute('aria-label')||e.getAttribute('title')||e.querySelector('img')?.getAttribute('alt')||e.getAttribute('placeholder')||'').slice(0,160),type:e.getAttribute('type'),placeholder:e.getAttribute('placeholder'),value:e.value||'',disabled:!!e.disabled,href:e.getAttribute('href')};});
 return {url:location.href,title:document.title,text:document.body?.innerText||'',html:document.body?.outerHTML||'',elements,width:innerWidth,height:innerHeight};
})()`;
async function read(){const r=await evaluate(observation);width=r.width;height=r.height;url=r.url;return r;}
async function point(ref,text=null){
 return evaluate(`(()=>{
 const visible=e=>e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})&&e.getBoundingClientRect().width&&e.getBoundingClientRect().height;
 let e;
 if(${JSON.stringify(text)}!==null){const t=${JSON.stringify(text)};const matches=[...document.querySelectorAll('body *')].filter(e=>visible(e)&&e.innerText?.trim()===t&&!([...e.children].some(c=>visible(c)&&c.innerText?.trim()===t)));if(matches.length!==1)throw new Error('需要唯一可见文字');e=matches[0];}
 else e=document.querySelector('[data-muse-ref="'+${Number(ref)}+'"]');
 if(!e||!visible(e))throw new Error('元素已失效，请重新读取页面');
 e.scrollIntoView({block:'center',inline:'nearest'});
 const r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;
 const hit=document.elementFromPoint(x,y);if(!hit||!(e.contains(hit)||hit.contains(e)))throw new Error('元素被遮挡');
 return {x,y,tag:e.tagName,type:e.type};})()`);
}
async function mouse(type,x,y,button='none',extra={}){
 return cdp('Input.dispatchMouseEvent',{type,x:Math.max(0,Math.min(width-1,x)),y:Math.max(0,Math.min(height-1,y)),button,buttons,...extra});
}
async function click(x,y,button='left'){
 buttons=button==='right'?2:button==='middle'?4:1;
 await mouse('mousePressed',x,y,button,{clickCount:1});buttons=0;
 await mouse('mouseReleased',x,y,button,{clickCount:1});
}
const keyCodes={Enter:13,Tab:9,Backspace:8,Delete:46,Escape:27,ArrowLeft:37,ArrowUp:38,ArrowRight:39,ArrowDown:40,Home:36,End:35,PageUp:33,PageDown:34,Space:32};
async function press(text){
 const parts=text.split('+'),key=parts.pop();let modifiers=0;
 for(const p of parts){if(p==='Alt')modifiers|=1;else if(p==='Control')modifiers|=2;else if(p==='Meta')modifiers|=4;else if(p==='Shift')modifiers|=8;else throw new Error('不支持该快捷键');}
 const actual=key==='Space'?' ':key;
 const code=keyCodes[key] || (actual.length===1?actual.toUpperCase().charCodeAt(0):0);
 if(!code)throw new Error('不支持该按键');
 const params={key:actual,windowsVirtualKeyCode:code,nativeVirtualKeyCode:code,modifiers};
 await cdp('Input.dispatchKeyEvent',{type:'rawKeyDown',...params});
 if(key==='Enter')await cdp('Input.dispatchKeyEvent',{type:'char',text:'\r',unmodifiedText:'\r',...params});
 else if(actual.length===1 && !(modifiers&6))await cdp('Input.dispatchKeyEvent',{type:'char',text:actual,...params});
 await cdp('Input.dispatchKeyEvent',{type:'keyUp',...params});
}
async function release(){
 for(const button of ['left','right','middle']){buttons=0;await mouse('mouseReleased',0,0,button,{clickCount:1});}
}
async function navigate(value){
 httpURL(value);
 if(tabId===null){const tab=await chrome.tabs.create({url:value,active:true});
  const end=Date.now()+20000;
  while(Date.now()<end){const ready=(await browserTabs()).find(t=>t.id===tab.id);if(eligible(ready)){await target(tab.id);break;}await new Promise(r=>setTimeout(r,100));}
  if(tabId===null)throw new Error('新网页创建超时');
 }else await chrome.tabs.update(tabId,{url:value});
 const end=Date.now()+20000;
 while(Date.now()<end){const tab=await chrome.tabs.get(tabId);if(tab.status==='complete'){await currentURL();return;}await new Promise(r=>setTimeout(r,100));}
 throw new Error('页面加载超时');
}
async function manual(i){
 if(i.action==='open')return navigate(i.text);
 if(i.action==='type')return cdp('Input.insertText',{text:i.text});
 if(i.action==='press')return press(i.text);
 const {x=0,y=0,button='left'}=i;
 if(i.action==='move')return mouse('mouseMoved',x,y);
 if(i.action==='down'){buttons=button==='right'?2:button==='middle'?4:1;return mouse('mousePressed',x,y,button,{clickCount:1});}
 if(i.action==='up'){buttons=0;return mouse('mouseReleased',x,y,button,{clickCount:1});}
 if(i.action==='click')return click(x,y,button);
 if(i.action==='wheel'||i.action==='scroll')return mouse('mouseWheel',i.action==='scroll'?width/2:x,i.action==='scroll'?height/2:y,'none',{deltaX:Math.max(-2000,Math.min(2000,i.dx||0)),deltaY:Math.max(-2000,Math.min(2000,i.action==='scroll'?y:i.y2||0))});
 if(i.action==='drag'){await manual({...i,action:'down'});try{for(let n=1;n<=20;n++)await mouse('mouseMoved',x+(i.x2-x)*n/20,y+(i.y2-y)*n/20);}finally{await manual({...i,action:'up',x:i.x2,y:i.y2});}return;}
 throw new Error('未知手动操作');
}
async function command(m){
 if(m.action==='list_tabs'){const tabs=(await browserTabs()).filter(eligible);return {title:'Chrome 网页列表',tabs:tabs.map(t=>({id:t.id,title:t.title||'',url:t.url,controlled:t.id===tabId}))};}
 if(m.action==='switch_tab'){await target(Number(m.ref));return read();}
 if(!['open','stream_stop','release'].includes(m.action)&&!(m.action==='manual'&&m.input?.action==='open'))await ensureTarget();
 if(m.action==='open'){await navigate(m.url);return read();}
 if(m.action==='manual'&&m.input?.action==='open'){await navigate(m.input.text);return {};}
 if(m.action==='stream_stop'){streaming=false;if(attached)await cdp('Page.stopScreencast');return {};}
 if(m.action==='release'){if(attached)await release();return {};}
 await currentURL();
 if(m.action==='screenshot'){const size=await metrics();const r=await cdp('Page.captureScreenshot',{format:'jpeg',quality:70,captureBeyondViewport:false});return {...r,...size};}
 if(m.action==='stream_start'){streaming=true;await metrics();try{await cdp('Page.startScreencast',{format:'jpeg',quality:70,everyNthFrame:1});}catch(error){if(!String(error.message).includes('Screencast is already active'))throw error;}const r=await cdp('Page.captureScreenshot',{format:'jpeg',quality:70,captureBeyondViewport:false});send({type:'frame',data:r.data,width,height,url});return {};}
 if(m.action==='stream_stop'){streaming=false;if(attached)await cdp('Page.stopScreencast');return {};}
 if(m.action==='release'){if(attached)await release();return {};}
 if(m.action==='manual'){await manual(m.input);return {};}
 if(m.action==='open')await navigate(m.url);
 else if(m.action==='click'||m.action==='click_text'){const p=await point(m.ref,m.action==='click_text'?m.text:null);await click(p.x,p.y);}
 else if(m.action==='hover'){const p=await point(m.ref);await cdp('Input.dispatchMouseEvent',{type:'mouseMoved',x:p.x,y:p.y});}
 else if(m.action==='fill'){const p=await point(m.ref);if(!['INPUT','TEXTAREA'].includes(p.tag)&&!(await evaluate(`document.querySelector('[data-muse-ref="${Number(m.ref)}"]').isContentEditable`)))throw new Error('该元素不可填写');await click(p.x,p.y);await evaluate(`(()=>{const e=document.querySelector('[data-muse-ref="${Number(m.ref)}"]');if(e.select)e.select();else{const r=document.createRange();r.selectNodeContents(e);const s=getSelection();s.removeAllRanges();s.addRange(r);}})()`);await cdp('Input.insertText',{text:m.text});}
 else if(m.action==='select'){await point(m.ref);await evaluate(`(()=>{const e=document.querySelector('[data-muse-ref="${Number(m.ref)}"]');if(e.tagName!=='SELECT')throw new Error('不是选择框');const o=[...e.options].find(o=>o.label===${JSON.stringify(m.text)});if(!o)throw new Error('选项不存在');e.value=o.value;e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));})()`);}
 else if(m.action==='press'){if(m.ref>=0){const p=await point(m.ref);await click(p.x,p.y);}await press(m.text);}
 else if(m.action==='scroll')await manual({action:'scroll',y:m.amount});
 else if(m.action==='wait')await new Promise(r=>setTimeout(r,Math.max(0,Math.min(3000,m.amount))));
 else if(m.action!=='read')throw new Error('未知操作');
 return read();
}
chrome.debugger.onEvent.addListener(async(source,method,params)=>{
 if(source.tabId!==tabId||method!=='Page.screencastFrame')return;
 try{await cdp('Page.screencastFrameAck',{sessionId:params.sessionId});if(Date.now()-lastFrame<50)return;lastFrame=Date.now();if(Date.now()-metricsAt>1000)await metrics();send({type:'frame',data:params.data,width,height,url});}catch{}
});
async function disconnect(reason='manual_disconnect'){
 send({type:'disconnect',reason});
 status='已断开';stopped=true;clearTimeout(reconnect);clearInterval(heartbeat);credentials=null;attached=false;streaming=false;
 if(['manual_disconnect','debugger_detached','auth_rejected'].includes(reason))await chrome.storage.local.set({autoConnect:false});
 const old=tabId;tabId=null;const ws=socket;socket=null;ws?.close();
 if(old!==null)try{await chrome.debugger.detach({tabId:old});}catch{}
 status='已断开';
}
chrome.debugger.onDetach.addListener((source,reason)=>{
 if(source.tabId!==tabId)return;
 tabId=null;attached=false;metricsAt=0;buttons=0;
 if(reason==='target_closed'){status='网页已关闭，下次操作自动连接其他网页';return;}
 disconnect('debugger_detached').then(()=>{status='Chrome 调试控制已停止；点击连接恢复';});
});
async function connectSocket(){
 return new Promise((resolve,reject)=>{
  if(stopped||!credentials){reject(new Error('浏览器自动连接已停止，请点击扩展中的连接'));return;}
  clearTimeout(reconnect);
  const auth=credentials;
  const u=new URL(auth.server);u.protocol=u.protocol==='https:'?'wss:':'ws:';u.pathname='/extension/ws';u.search='';u.hash='';
  const ws=new WebSocket(u);socket=ws;let ready=false;
  const timeout=setTimeout(()=>{ws.close(1000,'handshake_timeout');reject(new Error('连接超时，请确认工作台已启动'));},8000);
  ws.onopen=()=>{if(stopped||socket!==ws){ws.close();return;}ws.send(JSON.stringify({type:'hello'}));};
  ws.onmessage=e=>{
   if(stopped||socket!==ws)return;
   let m;try{m=JSON.parse(e.data);}catch{return;}
   if(m.type==='ready'){ready=true;clearTimeout(timeout);status='已连接，网页自动控制';resolve();clearInterval(heartbeat);heartbeat=setInterval(()=>send({type:'ping'}),15000);}
   if(m.type==='command')chain=chain.catch(()=>{}).then(async()=>{if(stopped||socket!==ws)return;try{const result=await command(m);sendOn(ws,{type:'result',id:m.id,result});}catch(error){sendOn(ws,{type:'result',id:m.id,error:error.message});}});
  };
  ws.onclose=e=>{clearTimeout(timeout);if(socket!==ws)return;clearInterval(heartbeat);status=e.code===1008?'连接被拒绝，请检查本机地址':e.code===1013?'工作台已有控制连接，正在重试':'连接已断开，正在重连';if(!ready)reject(new Error(status));if(e.code===1008){disconnect('auth_rejected').then(()=>{status='工作台拒绝连接，请检查本机地址后重新连接';});return;}if(!stopped)reconnect=setTimeout(()=>connectSocket().catch(()=>{}),3000);};
  ws.onerror=()=>{status='无法连接工作台';};
 });
}
chrome.runtime.onMessage.addListener((m,sender,respond)=>{
 if(sender.id!==chrome.runtime.id)return;
 (async()=>{
  if(m.type==='status')return {status};
  if(m.type==='disconnect'){await disconnect();return {status};}
  if(m.type!=='connect')throw new Error('未知请求');
  const server=httpURL(m.server);if(server.pathname!=='/'||server.search||server.hash||server.username||server.password)throw new Error('请填写工作台根地址');
  if(!['127.0.0.1','localhost','[::1]'].includes(server.hostname))throw new Error('免口令连接仅支持本机工作台');
  await disconnect('reconnect');await chain.catch(()=>{});
  try{if(m.tabId!==undefined&&eligible((await browserTabs()).find(t=>t.id===m.tabId)))await target(m.tabId);credentials={server:server.origin};stopped=false;await chrome.storage.local.set({server:server.origin,autoConnect:true});await connectSocket();return {status};}
  catch(e){await disconnect();throw e;}
 })().then(respond,e=>respond({error:e.message}));return true;
});

// The worker starts on extension load and browser startup. User stop stays persistent.
chrome.runtime.onStartup.addListener(()=>{if(stopped)resume().catch(e=>{status=e.message;});});
resume().catch(e=>{status=e.message;});
