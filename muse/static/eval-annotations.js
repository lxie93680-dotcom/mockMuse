// Human relevance judgments: defaults are blank, never pre-confirm generated labels.
let annotationState=null,annotationId=null,annotationDirty=false,annotationFilter='all';
const annotationNames={unreviewed:'待标注',draft:'草稿',confirmed:'已确认'};
async function openAnnotations(){
 try{annotationState=await api('evals/tool-search/annotations');annotationFilter='all';annotationId=annotationState.cases.find(c=>annotationState.annotations[c.id]?.status!=='confirmed')?.id||annotationState.cases[0]?.id;annotationDirty=false;$('detailTitle').textContent='工具搜索 · 人工标注';drawAnnotations();if(!$('detail').open)$('detail').showModal();}catch(e){error(e);}
}
function annotationLeave(){return !annotationDirty||window.confirm('当前修改尚未保存，确定放弃修改吗？');}
function drawAnnotations(){
 const host=$('detailBody');host.replaceChildren();const data=annotationState;
 const confirmed=data.cases.filter(c=>data.annotations[c.id]?.status==='confirmed').length;
 host.append(el('p',`已确认 ${confirmed} / ${data.total}。请选择所有能满足查询意图的工具；不用参考搜索排名。草稿不参与人工标注评测。`));
 host.append(el('small','确认后可用 --labels human 重跑搜索评测；历史自动标注成绩保持原样。'));
 const filters=el('div',undefined,'filters'),select=el('select');select.setAttribute('aria-label','标注状态筛选');
 for(const [value,label] of [['all','全部查询'],...Object.entries(annotationNames)]){const o=el('option',label);o.value=value;select.append(o);}select.value=annotationFilter;
 select.onchange=()=>{if(!annotationLeave()){select.value=annotationFilter;return;}annotationDirty=false;annotationFilter=select.value;annotationId=filteredAnnotations()[0]?.id;drawAnnotations();};filters.append(select);host.append(filters);
 const layout=el('div',undefined,'annotation-layout'),list=el('aside',undefined,'annotation-list'),editor=el('section');
 for(const c of filteredAnnotations()){const status=data.annotations[c.id]?.status||'unreviewed';const button=el('button',c.query+' · '+annotationNames[status],c.id===annotationId?'active':'');button.onclick=()=>{if(annotationLeave()){annotationDirty=false;annotationId=c.id;drawAnnotations();}};list.append(button);}
 layout.append(list,editor);host.append(layout);const c=data.cases.find(c=>c.id===annotationId);
 if(!c){editor.append(el('p','当前筛选下没有查询'));return;}
 const saved=data.annotations[c.id];editor.append(el('h3',c.query),el('small',c.id+' · '+annotationNames[saved?.status||'unreviewed']));
 const message=el('p');message.setAttribute('role','status');editor.append(message);
 const noneLabel=el('label',undefined,'annotation-none'),none=el('input');none.type='checkbox';none.id='annotationNoMatch';none.checked=!!saved?.no_match;noneLabel.append(none,document.createTextNode(' 此工具库中没有合适工具（明确的负样本）'));editor.append(noneLabel);
 const search=el('input');search.type='search';search.placeholder='筛选全部工具的名称或描述';search.setAttribute('aria-label','筛选标注工具');editor.append(search);
 const tools=el('div',undefined,'annotation-tools');editor.append(tools);const options=[];
 for(const tool of [...data.tools].sort((a,b)=>a.name.localeCompare(b.name))){const item=el('div',undefined,'annotation-tool'),label=el('label'),input=el('input');input.type='checkbox';input.value=tool.name;input.checked=saved?.relevant.includes(tool.name)||false;input.disabled=none.checked;label.append(input,el('strong',tool.name));item.append(label,el('small',tool.source_id),el('p',tool.description),block('参数定义',tool.parameters));input.onchange=()=>{annotationDirty=true;};options.push({item,input,tool});tools.append(item);}
 none.onchange=()=>{annotationDirty=true;for(const {input} of options){input.disabled=none.checked;if(none.checked)input.checked=false;}};
 search.oninput=()=>{const q=search.value.toLowerCase();for(const {item,tool} of options)item.hidden=!(tool.name+' '+tool.description).toLowerCase().includes(q);};
 const note=el('textarea');note.rows=3;note.maxLength=4000;note.placeholder='标注理由或疑问（可选）';note.setAttribute('aria-label','标注备注');note.value=saved?.note||'';note.oninput=()=>{annotationDirty=true;};editor.append(note);
 const reference=block('查看原始自动标注（仅供参考，不代表本人确认）',c.relevant);editor.append(reference);
 const actions=el('div',undefined,'filters'),draft=el('button','保存草稿'),confirm=el('button','确认并下一条'),previous=el('button','上一条'),next=el('button','下一条');
 const visible=filteredAnnotations(),position=visible.findIndex(v=>v.id===c.id);
 previous.disabled=position<=0;next.disabled=position>=visible.length-1;
 const move=delta=>{if(annotationLeave()){annotationDirty=false;annotationId=visible[position+delta]?.id;drawAnnotations();}};previous.onclick=()=>move(-1);next.onclick=()=>move(1);
 async function save(status){
   draft.disabled=confirm.disabled=true;message.textContent='保存中…';
   try{const result=await api('evals/tool-search/annotations/'+encodeURIComponent(c.id),{dataset_hash:data.dataset_hash,base_revision:saved?.revision||0,status,relevant:options.filter(v=>v.input.checked).map(v=>v.tool.name),no_match:none.checked,note:note.value});
     data.annotations[c.id]=result.annotation;annotationDirty=false;
     if(status==='confirmed')annotationId=visible[position+1]?.id||visible[position-1]?.id||c.id;
     drawAnnotations();const notice=el('p',status==='confirmed'?'标注已确认并保存':'草稿已保存');notice.setAttribute('role','status');$('detailBody').prepend(notice);
   }catch(e){message.textContent=e.message;draft.disabled=confirm.disabled=false;}
 }
 draft.onclick=()=>save('draft');confirm.onclick=()=>save('confirmed');actions.append(previous,next,draft,confirm);editor.append(actions);
}
function filteredAnnotations(){return annotationState.cases.filter(c=>annotationFilter==='all'||(annotationState.annotations[c.id]?.status||'unreviewed')===annotationFilter);}
$('detail').addEventListener('cancel',e=>{if(!annotationLeave())e.preventDefault();});
window.addEventListener('beforeunload',e=>{if(annotationDirty){e.preventDefault();e.returnValue='';}});

$('detail').addEventListener('close',()=>{annotationDirty=false;annotationState=null;annotationId=null;});
