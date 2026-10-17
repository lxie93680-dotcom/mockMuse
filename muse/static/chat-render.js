/* Small DOM-only Markdown renderer: external content never becomes HTML. */
(()=>{
const node=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
function inline(parent,text){
  const tokens=/(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\([^\s)]+\))/g;let start=0;
  for(const match of text.matchAll(tokens)){parent.append(document.createTextNode(text.slice(start,match.index)));const value=match[0];
    if(value.startsWith('`'))parent.append(node('code',value.slice(1,-1)));
    else if(value.startsWith('**'))parent.append(node('strong',value.slice(2,-2)));
    else{const parts=/^\[([^\]]+)\]\(([^)]+)\)$/.exec(value);let safe=false;try{safe=['https:','http:'].includes(new URL(parts[2]).protocol);}catch{}if(safe){const link=node('a',parts[1]);link.href=parts[2];link.target='_blank';link.rel='noopener noreferrer';parent.append(link);}else parent.append(document.createTextNode(value));}
    start=match.index+value.length;
  }parent.append(document.createTextNode(text.slice(start)));
}
function markdown(text){
  const host=node('div');host.className='message-body';const lines=String(text||'').split('\n');let i=0;
  const cells=line=>line.trim().replace(/^\||\|$/g,'').split('|').map(v=>v.trim());
  while(i<lines.length){const line=lines[i];if(!line.trim()){i++;continue;}
    if(/^\s*```/.test(line)){const language=line.trim().slice(3);const block=node('div');block.className='code-block';const head=node('div');head.className='code-heading';head.append(node('span',language||'代码'));const copy=node('button','复制');copy.type='button';const content=[];i++;while(i<lines.length&&!/^\s*```/.test(lines[i]))content.push(lines[i++]);if(i<lines.length)i++;copy.onclick=()=>copyText(copy,content.join('\n'));head.append(copy);const pre=node('pre');pre.append(node('code',content.join('\n')));block.append(head,pre);host.append(block);continue;}
    if(i+1<lines.length&&line.includes('|')&&/^\s*\|?\s*:?-{3,}/.test(lines[i+1])&&cells(lines[i+1]).every(v=>/^:?-{3,}:?$/.test(v))){const wrap=node('div');wrap.className='message-table';const table=node('table'),thead=node('thead'),tr=node('tr');for(const cell of cells(line)){const th=node('th');inline(th,cell);tr.append(th);}thead.append(tr);table.append(thead);const body=node('tbody');i+=2;while(i<lines.length&&lines[i].trim()&&lines[i].includes('|')){const row=node('tr');for(const cell of cells(lines[i++])){const td=node('td');inline(td,cell);row.append(td);}body.append(row);}table.append(body);wrap.append(table);host.append(wrap);continue;}
    const heading=/^(#{1,6})\s+(.+)$/.exec(line);if(heading){const h=node('h'+Math.min(heading[1].length+1,6));inline(h,heading[2]);host.append(h);i++;continue;}
    if(/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)){host.append(node('hr'));i++;continue;}
    if(/^\s*>/.test(line)){const quote=node('blockquote');while(i<lines.length&&/^\s*>/.test(lines[i])){const p=node('p');inline(p,lines[i++].replace(/^\s*>\s?/,''));quote.append(p);}host.append(quote);continue;}
    const list=/^\s*(?:([-+*])|(\d+)\.)\s+(.+)$/.exec(line);if(list){const ordered=!!list[2],ul=node(ordered?'ol':'ul');if(ordered)ul.start=Number(list[2]);while(i<lines.length){const item=/^\s*(?:([-+*])|(\d+)\.)\s+(.+)$/.exec(lines[i]);if(!item||!!item[2]!==ordered)break;const li=node('li');inline(li,item[3]);ul.append(li);i++;}host.append(ul);continue;}
    const p=node('p');inline(p,line);i++;while(i<lines.length&&lines[i].trim()&&!/^\s*(?:```|#{1,6}\s|>|[-+*]\s|\d+\.\s)/.test(lines[i])&&!(i+1<lines.length&&lines[i].includes('|')&&/---/.test(lines[i+1]))){p.append(node('br'));inline(p,lines[i++]);}host.append(p);
  }return host;
}
async function copyText(button,text){try{await navigator.clipboard.writeText(text);button.textContent='已复制';}catch{button.textContent='复制失败';}setTimeout(()=>{button.textContent='复制';},1800);}
// Follow is sticky across content growth; only user navigation releases it.
window.createMuseChatFollow=container=>{
  let following=true,frame=0;
  const nearBottom=()=>container.scrollHeight-container.scrollTop-container.clientHeight<60;
  const schedule=()=>{if(!following||frame)return;frame=requestAnimationFrame(()=>{frame=0;if(following)container.scrollTop=container.scrollHeight;});};
  container.addEventListener('scroll',()=>{following=nearBottom();},{passive:true});
  container.addEventListener('wheel',event=>{if(event.deltaY<0)following=false;},{passive:true});
  const resize=new ResizeObserver(schedule);resize.observe(container);
  const mutation=new MutationObserver(records=>{
    for(const record of records)if(record.target===container)for(const removed of record.removedNodes)if(removed.nodeType===1)resize.unobserve(removed);
    for(const child of container.children)resize.observe(child);schedule();
  });
  mutation.observe(container,{childList:true,subtree:true,characterData:true});
  return {update:schedule,reset:()=>{following=true;schedule();}};
};
window.renderMuseMessage=(item,{animate=false,onUpdate=()=>{}}={})=>{
  const role=item.role==='user'?'user':'assistant',text=String(item.text||'');
  const article=node('article');article.className='message '+role;article.setAttribute('aria-label',role==='user'?'你的消息':'Muse 回复');
  if(role==='user'){const content=node('div',text);content.className='message-body';article.append(content);return article;}
  const body=markdown(animate?'':text);article.append(body);
  if(text){
    const actions=node('div');actions.className='message-actions';const copy=node('button','复制');copy.type='button';copy.setAttribute('aria-label','复制 Muse 回复');copy.onclick=()=>copyText(copy,text);actions.append(copy);article.append(actions);
    if(animate&&!matchMedia('(prefers-reduced-motion: reduce)').matches){
      actions.hidden=true;article.setAttribute('aria-busy','true');
      const characters=Array.from(text),speed=Math.max(.07,characters.length/5000);let start=null,last=0;
      const tick=now=>{
        if(!article.isConnected)return;
        if(start===null)start=now;
        const count=Math.min(characters.length,Math.max(1,Math.floor((now-start)*speed)));
        if(count!==last){body.replaceChildren(...markdown(characters.slice(0,count).join('')).childNodes);last=count;onUpdate();}
        if(count<characters.length)requestAnimationFrame(tick);
        else{actions.hidden=false;article.removeAttribute('aria-busy');onUpdate();}
      };requestAnimationFrame(tick);
    }else if(animate)body.replaceChildren(...markdown(text).childNodes);
  }
  return article;
};
window.renderMuseWelcome=()=>{const welcome=node('div');welcome.className='welcome';const mark=node('div','m');mark.className='welcome-mark';const overline=node('span','MUSE');overline.className='welcome-overline';welcome.append(mark,overline,node('h2','有什么想一起完成的？'),node('p','从一个问题、一个链接，或一个想法开始。'));const suggestions=node('div');suggestions.className='welcome-suggestions';for(const [label,prompt] of [['阅读网页','打开 https://example.com，读取并用中文总结页面内容。'],['整理文件','先查看当前工作目录，帮我了解项目结构。'],['连接 MCP','我想接入一个远程 MCP，请告诉我需要提供哪些信息。']]){const button=node('button');button.type='button';button.append(node('span',label),node('span','↗'));button.onclick=()=>{const input=document.getElementById('prompt');input.value=prompt;input.dispatchEvent(new Event('input'));input.focus();};suggestions.append(button);}welcome.append(suggestions);return welcome;};
})();
