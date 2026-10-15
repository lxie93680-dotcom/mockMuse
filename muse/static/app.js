const $ = id => document.getElementById(id);
let viewportWidth=1280, viewportHeight=800;
let handoffRequested=null;
let current = null, socket = null, takeover = false, reconnect = null;
async function api(path, body) {
  const response = await fetch('/api/' + path, {method:body === undefined?'GET':'POST',headers:{'Content-Type':'application/json'},body:body === undefined?undefined:JSON.stringify(body)});
  const result = await response.json();
  if(response.status===401 && path!=='login'){if(!$('login').open)$('login').showModal();throw new Error('请先连接工作台');}
  if(!response.ok) throw new Error(result.detail || '请求失败');
  return result;
}
function notice(error){$('notice').textContent=error.message || String(error);$('controlStatus').textContent=error.message || String(error);}
function renderFrame(s){
  if(s.width>0 && s.height>0){viewportWidth=s.width;viewportHeight=s.height;}
  if(s.browser_connected===false){$('screen').hidden=true;$('empty').hidden=false;}
  $('url').textContent=s.url || '等待浏览器启动';
  if(s.frame && $('screen').dataset.frame!==s.frame){$('screen').src='data:image/jpeg;base64,'+s.frame;$('screen').dataset.frame=s.frame;$('screen').hidden=false;$('empty').hidden=true;$('frameTime').textContent='实时画面 · '+new Date().toLocaleTimeString();}
}
function render(s){
  const changed=current && current.conversation_id!==s.conversation_id;
  renderDebug(s.debug||{enabled:false,calls:[]});
  renderChildProgress(s);
  current=s; const running=s.status==='running';
  $('browserDriverLabel').textContent=s.browser_backend==='playwright'?(s.browser_driver?.connection_mode==='extension'?'Playwright MCP · 普通 Chrome':'Playwright MCP · 专用 Chrome'):'Chrome 扩展控制';
  $('browserConnection').textContent=s.browser_backend==='playwright'?(s.browser_driver?.ready?'Playwright MCP 已就绪':'Playwright MCP 未就绪'):(s.browser_connected?'Chrome 扩展已连接':'Chrome 扩展未连接');
  $('browserConnection').title=s.browser_connected?'按任务自动控制 Chrome 网页':s.frame_error||'Chrome 连接器尚未连接，请确认扩展已更新并启用自动连接';
  $('focusComposer').disabled=running;
  if(changed){$('prompt').value='';$('notice').textContent='';}
  const sessionSignature=JSON.stringify([s.conversation_id,s.conversations,running]);
  if($('conversationList').dataset.signature!==sessionSignature){
    $('conversationList').replaceChildren();
    for(const session of s.conversations||[]){
      const button=document.createElement('button');button.className='conversation-item'+(session.id===s.conversation_id?' active':'');
      button.textContent=session.title;button.title=session.title;button.disabled=running;
      button.onclick=async()=>{try{releaseTakeover();render(await api('conversations/'+session.id+'/activate',{}));}catch(error){notice(error);}};
      $('conversationList').append(button);
    }
    $('conversationList').dataset.signature=sessionSignature;
  }
  const phases={preparing_context:'准备上下文',thinking:'准备上下文',executing_tool:'工具执行中',replying:'等待模型回复',checking_result:'检查中'};
  $('humanHandoff').hidden=s.status!=='needs_human';
  if(s.status==='needs_human'){
    $('humanReason').textContent='需要你操作：'+(s.human_handoff?.reason||'请手动处理当前网页');
    $('notice').textContent=$('humanReason').textContent;
    if(!takeover&&handoffRequested!==s.task_id&&socket?.readyState===WebSocket.OPEN){handoffRequested=s.task_id;send({type:'takeover',active:true});}
  }else handoffRequested=null;
  $('status').textContent=running?(phases[s.phase]||'正在执行'):({needs_human:'等待你操作',idle:'待命',completed:'已完成',stopped:'已停止',error:'执行失败',interrupted:'任务已中断'})[s.status]||s.status;
  if(!running && s.status==='stopped' && s.messages?.at(-1)?.text?.startsWith('模型无法使用：'))$('status').textContent='模型无法使用';
  $('status').title=running?(s.phase_detail||''):'';
  $('phaseDetail').textContent=running?(s.phase_detail||''):'';
  $('send').disabled=running; $('demo').disabled=running;
  $('model').textContent=s.model_ready?s.model:'尚未配置密钥';
  const mcp=Object.entries(s.mcp||{}).filter(([name,value])=>value.enabled);
  $('toolStatus').textContent=mcp.length?'本地 MCP · '+mcp.map(([name,value])=>(name==='filesystem'?'文件':'终端/文档')+' '+(value.ready?value.tools.length+'项':value.error||'未就绪')).join(' · '):'内置工具';
  $('provider').value=s.provider; $('provider').disabled=running;
  for(const option of $('provider').options){const p=s.providers.find(p=>p.id===option.value);if(p)option.textContent=p.name;}
  if(running && takeover)releaseTakeover();
  const signature=JSON.stringify(s.messages);
  if($('messages').dataset.signature!==signature){
    $('messages').replaceChildren();
    for(const item of s.messages){const div=document.createElement('div');div.className='message '+item.role;div.textContent=item.text;if(/^\/api\/images\/[0-9a-f]{32}\.jpg$/.test(item.image_url||'')){const link=document.createElement('a');link.href=item.image_url;link.target='_blank';link.rel='noopener';link.className='chat-image-link';const img=document.createElement('img');img.src=item.image_url;img.alt='浏览器截图';img.className='chat-image';img.onload=()=>{$('messages').scrollTop=$('messages').scrollHeight;};link.append(img);div.append(link);}$('messages').append(div);}
    if(!s.messages.length){const welcome=document.createElement('div');welcome.className='welcome';const title=document.createElement('h2');title.textContent='开始一个新任务';const description=document.createElement('p');description.textContent='这是独立的新会话，可以操作浏览器、文件或终端。';welcome.append(title,description);$('messages').append(welcome);}
    $('messages').dataset.signature=signature; $('messages').scrollTop=$('messages').scrollHeight;
  }
  renderFrame(s);
  if(s.frame_error)$('frameTime').textContent=s.frame_error;
  const logs=JSON.stringify(s.logs);
  if($('logs').dataset.signature!==logs){$('logs').replaceChildren();for(const item of [...s.logs].reverse()){const li=document.createElement('li');const time=document.createElement('time');time.textContent=item.time;const content=document.createElement('div');content.textContent=item.title;if(item.detail){const detail=document.createElement('small');detail.textContent=item.detail;content.append(detail);}li.append(time,content);$('logs').append(li);}$('logs').dataset.signature=logs;}
}
function updateTakeover(){$('manual').hidden=!takeover;$('viewport').classList.toggle('takeover',takeover);$('stop').textContent=takeover?'已接管 · 点击重新连接':'停止并接管';$('controlStatus').textContent=takeover?'已接管：点击画面后直接打字，支持拖动和滚轮':'未接管：请点击“停止并接管”后操作画面';}
function send(message){if(!socket || socket.readyState!==WebSocket.OPEN){notice(new Error('连接已断开，请重新连接'));return false;}socket.send(JSON.stringify(message));return true;}
function releaseTakeover(){if(takeover)send({type:'takeover',active:false});takeover=false;updateTakeover();}
function input(body){if(!takeover)return;if(send({type:'input',input:body}))$('controlStatus').textContent='正在控制浏览器 · 可以直接打字';}
function connect(){
  clearTimeout(reconnect);
  if(socket){socket.onclose=null;socket.close();}
  const ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws');socket=ws;
  ws.onopen=()=>{$('connection').textContent='已连接执行端';$('dot').className='connected';};
  ws.onmessage=e=>{const message=JSON.parse(e.data);if(message.type==='frame'){renderFrame(message);return;}if(message.type==='control'){takeover=message.active;updateTakeover();if(takeover)$('controlStatus').textContent='已接管：点击画面后直接打字，支持拖动和滚轮';return;}if(message.type==='error'){notice(new Error(message.message));return;}render(message);};
  ws.onclose=async e=>{if(socket!==ws)return;takeover=false;handoffRequested=null;updateTakeover();$('connection').textContent='连接已断开';$('dot').className='';if(e.code===1008){if(!$('login').open)$('login').showModal();return;}try{render(await api('state'));}catch(error){if($('login').open)return;}reconnect=setTimeout(connect,2000);};
}
$('login').addEventListener('cancel',e=>e.preventDefault());
$('loginForm').onsubmit=async e=>{e.preventDefault();try{await api('login',{token:$('token').value});$('token').value='';$('loginError').textContent='';$('login').close();render(await api('state'));connect();}catch(error){$('loginError').textContent=error.message;}};
$('taskForm').onsubmit=async e=>{e.preventDefault();if(!$('prompt').value.trim())return;try{releaseTakeover();$('notice').textContent='';await api('tasks',{prompt:$('prompt').value,demo:false});$('prompt').value='';}catch(error){notice(error);}};
$('prompt').addEventListener('keydown',e=>{
  if(e.key!=='Enter' || e.shiftKey || e.isComposing || e.keyCode===229)return;
  e.preventDefault();
  if(!$('send').disabled && $('prompt').value.trim())$('taskForm').requestSubmit();
});
$('sample').onclick=()=>{$('prompt').value='打开 https://example.com，读取并用中文总结页面内容。';$('prompt').focus();};
$('demo').onclick=async()=>{try{releaseTakeover();$('notice').textContent='';await api('tasks',{prompt:'浏览器演示',demo:true});}catch(error){notice(error);}};
$('stop').onclick=async()=>{try{await api('stop',{});send({type:'takeover',active:true});}catch(error){notice(error);}};
$('exit').onclick=releaseTakeover;
$('resumeHuman').onclick=async()=>{try{$('resumeHuman').disabled=true;await api('resume',{});releaseTakeover();}catch(error){notice(error);}finally{$('resumeHuman').disabled=false;}};
const screen=$('screen'), keyboard=$('remoteKeyboard');
let pointerHeld=false, lastMove=0;
const buttons={0:'left',1:'middle',2:'right'};
function screenPoint(e){const r=screen.getBoundingClientRect();return {x:(e.clientX-r.left)*viewportWidth/r.width,y:(e.clientY-r.top)*viewportHeight/r.height};}
screen.ondragstart=e=>e.preventDefault();
screen.oncontextmenu=e=>{if(takeover)e.preventDefault();};
screen.onpointerdown=e=>{if(!takeover)return;e.preventDefault();pointerHeld=true;screen.setPointerCapture(e.pointerId);keyboard.focus({preventScroll:true});input({action:'down',...screenPoint(e),button:buttons[e.button]||'left'});};
screen.onpointermove=e=>{if(!takeover)return;const now=performance.now();if(now-lastMove<25)return;lastMove=now;input({action:'move',...screenPoint(e)});};
screen.onpointerup=e=>{if(!takeover||!pointerHeld)return;e.preventDefault();pointerHeld=false;input({action:'up',...screenPoint(e),button:buttons[e.button]||'left'});};
screen.onpointercancel=e=>{if(pointerHeld)input({action:'up',...screenPoint(e),button:'left'});pointerHeld=false;};
screen.addEventListener('wheel',e=>{if(!takeover)return;e.preventDefault();input({action:'wheel',...screenPoint(e),dx:e.deltaX,y2:e.deltaY});},{passive:false});
const keyNames={ArrowLeft:'ArrowLeft',ArrowRight:'ArrowRight',ArrowUp:'ArrowUp',ArrowDown:'ArrowDown',Escape:'Escape',Backspace:'Backspace',Delete:'Delete',Enter:'Enter',Tab:'Tab',Home:'Home',End:'End',PageUp:'PageUp',PageDown:'PageDown'};
keyboard.onkeydown=e=>{
  if(!takeover || e.isComposing)return;
  if(['Shift','Control','Alt','Meta'].includes(e.key))return;
  if(keyNames[e.key] || e.ctrlKey || e.metaKey || e.altKey){
    e.preventDefault();const parts=[];if(e.ctrlKey)parts.push('Control');if(e.metaKey)parts.push('Meta');if(e.altKey)parts.push('Alt');if(e.shiftKey)parts.push('Shift');parts.push(keyNames[e.key]||e.key);input({action:'press',text:parts.join('+')});
  }
};
keyboard.oninput=e=>{if(!takeover||e.isComposing)return;if(keyboard.value){input({action:'type',text:keyboard.value});keyboard.value='';}};
keyboard.oncompositionend=()=>{if(takeover&&keyboard.value){input({action:'type',text:keyboard.value});keyboard.value='';}};
$('manualForm').onsubmit=e=>{e.preventDefault();input({action:'type',text:$('manualText').value});$('manualText').value='';keyboard.focus({preventScroll:true});};
$('navigateForm').onsubmit=e=>{e.preventDefault();let url=$('manualUrl').value.trim();if(!url)return;if(!/^https?:\/\//i.test(url))url='https://'+url;input({action:'open',text:url});};
$('enter').onclick=()=>input({action:'press',text:'Enter'});
$('up').onclick=()=>input({action:'scroll',y:-600});$('down').onclick=()=>input({action:'scroll',y:600});
api('state').then(s=>{render(s);connect();}).catch(()=>{});
$('provider').onchange=async()=>{try{await api('provider',{provider:$('provider').value});render(await api('state'));$('notice').textContent='模型已切换；浏览器状态保留，模型对话上下文已重置。';}catch(error){if(current)$('provider').value=current.provider;notice(error);}};

async function newConversation(){try{releaseTakeover();render(await api('conversations',{}));$('prompt').focus();}catch(error){notice(error);}}
$('focusComposer').onclick=newConversation;
document.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key==='k'){e.preventDefault();newConversation();}});

$('sendScreenshot').onclick=async()=>{try{await api('screenshot',{});render(await api('state'));}catch(error){notice(error);}};

let debugSignature='';
function jsonBlock(label,value){const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent=label;const pre=document.createElement('pre');pre.textContent=JSON.stringify(value,null,2);details.append(summary,pre);return details;}
function debugText(content){
  if(typeof content==='string')return content;
  if(Array.isArray(content))return content.map(p=>typeof p.text==='string'?p.text:p.type==='input_image'?'[图片]':JSON.stringify(p)).join('\n');
  return JSON.stringify(content,null,2);
}
function renderDebug(debug){
  $('debugPanel').hidden=!debug.enabled;$('debugToggle').classList.toggle('enabled',debug.enabled);$('debugToggle').textContent=debug.enabled?'Debug ●':'Debug';
  const signature=JSON.stringify([debug.calls,debug.events]);if(signature===debugSignature)return;debugSignature=signature;
  const open=new Set([...$('debugCalls').querySelectorAll('details[open]')].map(d=>d.dataset.key));
  $('debugCalls').replaceChildren();
  [...debug.calls].reverse().forEach((call,index)=>{
    const card=document.createElement('article');card.className='debug-call'+(call.status==='error'?' debug-error':'');const heading=document.createElement('h3');heading.textContent='调用 '+(debug.calls.length-index)+' · '+({running:'等待响应',completed:'已返回',error:'失败'})[call.status]+(call.duration!==undefined?' · '+call.duration+'s':'');card.append(heading);
    if(call.error)card.append(jsonBlock('错误详情',call.error));
    if(call.http_url){const meta=document.createElement('small');meta.textContent=call.http_method+' '+call.http_url+' · '+(call.display_limits||'');card.append(meta);}
    const input=call.request?.messages;const messages=Array.isArray(input)?input:typeof input==='string'?[{role:'user',content:input}]:[];
    const userMessages=messages.filter(m=>m.role==='user');const latest=userMessages.at(-1);
    if(latest){const div=document.createElement('div');div.className='debug-message';const role=document.createElement('b');role.textContent='USER · 本轮输入';const text=document.createElement('pre');text.textContent=debugText(latest.content);div.append(role,text);card.append(div);}
    const wire=call.response?.choices?.[0]?.message;
    const outputs=wire?[{role:'assistant',content:wire.content},...(wire.tool_calls||[]).map(t=>({type:'function_call',name:t.function.name,arguments:t.function.arguments}))]:(call.response?.output||[]);
    for(const item of outputs){if(item.role!=='assistant'&&item.type!=='function_call')continue;const div=document.createElement('div');div.className='debug-message';const role=document.createElement('b');role.textContent=item.type==='function_call'?'ASSISTANT · 工具调用':'ASSISTANT · 模型回复';const text=document.createElement('pre');text.textContent=item.type==='function_call'?item.name+'\n'+item.arguments:debugText(item.content);div.append(role,text);card.append(div);}
    if(messages.length>1){const history=jsonBlock('历史消息（'+(messages.length-1)+'条）',messages.slice(0,-1));history.dataset.key=call.id+'history';history.open=open.has(history.dataset.key);card.append(history);}
    for(const [label,value,key] of [['实际 API 请求体 JSON',call.request_source==='http_json'?call.request:'未捕获实际HTTP请求体；SDK参数不作为实际请求体展示', 'request'],['API 返回 JSON',call.response,'response']]){const details=jsonBlock(label,value);details.dataset.key=call.id+key;details.open=open.has(details.dataset.key);card.append(details);}
    $('debugCalls').append(card);
  });
  for(const event of [...(debug.events||[])].reverse()){
    const card=document.createElement('article');card.className='debug-call'+(event.error?' debug-error':'');
    const heading=document.createElement('h3');heading.textContent=(event.error?'错误 · ':'')+event.source+' · '+new Date(event.time*1000).toLocaleTimeString();
    const body=document.createElement('pre');body.textContent=JSON.stringify(event.detail,null,2);card.append(heading,body);$('debugCalls').append(card);
  }
  if(!debug.calls.length&&!(debug.events||[]).length){const empty=document.createElement('p');empty.className='debug-hint';empty.textContent='等待模型调用。现在发送一个任务即可查看。';$('debugCalls').append(empty);}
}
$('debugToggle').onclick=async()=>{try{renderDebug(await api('debug',{enabled:$('debugPanel').hidden}));}catch(e){notice(e);}};
$('debugClose').onclick=async()=>{try{renderDebug(await api('debug',{enabled:false}));}catch(e){notice(e);}};
$('debugClear').onclick=async()=>{try{renderDebug(await api('debug/clear',{}));}catch(e){notice(e);}};

function renderChildProgress(s){
  const panel=$('childProgress');if(!panel)return;
  const entries=(s.activities||[]).filter(a=>!s.task_id||a.parent_task_id===s.task_id).slice(-6);
  panel.hidden=!entries.length;
  const signature=JSON.stringify(entries)+Math.floor(Date.now()/1000);
  if(panel.dataset.signature===signature)return;
  panel.dataset.signature=signature;panel.replaceChildren();
  const phases={preparing_context:'准备上下文',thinking:'准备上下文',replying:'等待模型回复',executing_tool:'工具执行中',checking_result:'检查中'};
  const states={completed:'完成',error:'错误',interrupted:'打断'};
  for(const item of entries){
    const card=document.createElement('article');card.className='child-card '+item.status;
    const head=document.createElement('div');head.className='child-card-head';
    const label=document.createElement('strong');label.textContent=item.purpose==='compression'?'上下文压缩':'子 Agent · '+item.title;
    const badge=document.createElement('span');badge.className='child-state';
    badge.textContent=item.status==='running'?(item.purpose==='compression'?'压缩中 · ':'')+(phases[item.phase]||'进行中'):(states[item.status]||item.status);
    head.append(label,badge);
    const detail=document.createElement('div');detail.className='child-card-detail';
    const seconds=Math.max(0,Math.floor(((item.ended_at||Date.now()/1000)-item.started_at)));
    const tools=(item.active_tools||[]).join('、');
    detail.textContent=(tools?'工具：'+tools+' · ':'')+(item.detail||'')+' · '+seconds+'秒';
    card.append(head,detail);panel.append(card);
  }
}
setInterval(()=>{if(current)renderChildProgress(current);},1000);

// Drag only the title bar; JSON selection and header buttons keep their behavior.
(()=>{
  const panel=$('debugPanel'),handle=panel.querySelector('.debug-head');
  let drag=null;
  function place(left,top){
    const box=panel.getBoundingClientRect();
    const margin=8;
    panel.style.left=Math.max(margin,Math.min(left,window.innerWidth-box.width-margin))+'px';
    panel.style.top=Math.max(margin,Math.min(top,window.innerHeight-box.height-margin))+'px';
    panel.style.right='auto';panel.style.bottom='auto';
  }
  handle.addEventListener('pointerdown',event=>{
    if(event.button!==0||event.target.closest('button'))return;
    const box=panel.getBoundingClientRect();
    drag={id:event.pointerId,x:event.clientX,y:event.clientY,left:box.left,top:box.top};
    handle.setPointerCapture(event.pointerId);handle.classList.add('dragging');event.preventDefault();
  });
  handle.addEventListener('pointermove',event=>{
    if(!drag||event.pointerId!==drag.id)return;
    place(drag.left+event.clientX-drag.x,drag.top+event.clientY-drag.y);
  });
  function end(event){
    if(!drag||event.pointerId!==drag.id)return;
    drag=null;handle.classList.remove('dragging');
    if(handle.hasPointerCapture(event.pointerId))handle.releasePointerCapture(event.pointerId);
  }
  handle.addEventListener('pointerup',end);handle.addEventListener('pointercancel',end);
  handle.addEventListener('lostpointercapture',()=>{drag=null;handle.classList.remove('dragging');});
  window.addEventListener('resize',()=>{
    if(panel.hidden||!panel.style.left)return;
    const box=panel.getBoundingClientRect();place(box.left,box.top);
  });
})();

$('debugMinimize').onclick=()=>{
  const panel=$('debugPanel');
  const minimized=panel.classList.toggle('minimized');
  $('debugMinimize').textContent=minimized?'恢复':'最小化';
  $('debugMinimize').setAttribute('aria-expanded',String(!minimized));
  $('debugMinimize').setAttribute('aria-label',minimized?'恢复调试窗口':'最小化调试窗口');
  // Re-clamp a restored panel, including after a viewport resize while minimized.
  window.dispatchEvent(new Event('resize'));
};
