function formattedBlock(title,data){const d=el('details');d.append(el('summary',title),dataView(data));return d;}
const categoryNames={exact_name:'工具名称',keywords:'关键词',natural_language:'自然语言',paraphrase:'同义表达',cross_language:'跨语言',ambiguous_distractors:'干扰工具',multi_relevant:'多个相关工具',no_match:'无匹配'};
const verdictNames={agree:'认可初评',adjust:'已改分',unscorable:'证据不足'};
function scoreText(value){return value===null||value===undefined?'待评':Number(value).toFixed(Number.isInteger(Number(value))?0:1)+' / 100';}
function dataView(value,depth=0){
 if(value===null||value===undefined)return el('span','—','muted');
 if(typeof value==='string'){
   if(depth<4&&/^[\[{]/.test(value.trim())){try{return dataView(JSON.parse(value),depth+1);}catch{}}
   return el('div',value,'readable-text');
 }
 if(typeof value!=='object')return el('span',String(value));
 if(depth>5)return el('span','展开原始记录查看完整数据','muted');
 if(Array.isArray(value)){
   if(!value.length)return el('span','无','muted');
   if(value.every(v=>v===null||typeof v!=='object')){const list=el('div',undefined,'value-list');for(const v of value.slice(0,100))list.append(el('span',String(v),'value-tag'));return list;}
   const list=el('div');for(const [i,v] of value.slice(0,50).entries()){const item=el('div',undefined,'value-item');item.append(el('small','第 '+(i+1)+' 项'),dataView(v,depth+1));list.append(item);}return list;
 }
 const table=el('table',undefined,'field-table'),body=el('tbody');for(const [key,val] of Object.entries(value).slice(0,60)){const row=el('tr'),cell=el('td');cell.append(dataView(val,depth+1));row.append(el('th',fieldNames[key]||key),cell);body.append(row);}table.append(body);return table;
}
const fieldNames={query:'查询',instruction:'任务要求',prompt:'输入',answer:'回答',relevant:'预期相关工具',retrieved:'实际返回工具',checks:'通过的检查项',observations:'执行观测',parameters:'参数',arguments:'调用参数',result:'结果',value:'返回内容',tool:'工具',name:'名称',status:'状态',error:'错误',failure:'失败原因',operation:'操作',stage:'阶段',source_id:'来源',events:'页面事件',wrong_click:'是否误点',duration_ms:'耗时（毫秒）',action:'动作',text:'内容',description:'说明',success_checks:'验收要求',category:'类别',backend:'运行方式',model_requests:'模型请求数',tool_calls:'工具调用数',temporary_namespace_removed:'临时数据已清理',file_content_exact:'文件逐字一致',passed_checks:'已通过断言',first_relevant_rank:'首个相关工具排名',missing:'未召回工具',fingerprint:'工具指纹'};
function sectionCard(title){const box=el('section',undefined,'review-card');box.append(el('h3',title));return box;}
function renderDataset(cases){
 const wrap=el('div',undefined,'table-wrap'),table=el('table'),head=el('tr');for(const t of ['用例','任务 / 查询','类别','预期结果'])head.append(el('th',t));table.append(head);
 for(const c of cases){const row=el('tr');row.tabIndex=0;row.append(el('td',c.id||c.case_id||'—'),el('td',c.query||c.instruction||c.question||c.title||'—'),el('td',categoryNames[c.category]||c.category||c.kind||'—'));const cell=el('td');cell.append(dataView(c.relevant??c.success_checks??c.expected??c.expected_answer??'查看详情'));row.append(cell);row.onclick=()=>{const detail=el('dialog',undefined,'case-view');detail.append(el('h2',c.title||c.query||c.id||'用例'),dataView(c));const close=el('button','关闭用例');close.onclick=()=>{detail.close();detail.remove();};detail.append(close);detail.addEventListener('close',()=>detail.remove());document.body.append(detail);detail.showModal();};row.onkeydown=e=>{if(e.key==='Enter')row.click();};table.append(row);}wrap.append(table);return wrap;
}
function renderTaskDetail(x,id,index){
 const c=x.case,grade=x.initial_grade,human=x.human_review,host=$('detailBody');
 host.replaceChildren();const top=el('div',undefined,'review-scoreboard');
 for(const [label,value] of [['初评分',scoreText(grade?.score)],['人工复核',human?scoreText(human.score):'未复核'],['运行状态',names[x.status]||x.status]]){const card=el('div',undefined,'score-tile');card.append(el('span',label),el('strong',value));top.append(card);}host.append(top);
 const task=sectionCard('任务与结果');task.append(dataView({任务:c.query||c.instruction||c.title||c.case_id||c.variant_id||'见执行步骤',运行方式:c.backend||c.mode||'—',原始结果:names[x.status]||x.status}));
 if(Array.isArray(c.retrieved)&&Array.isArray(c.relevant)){
  task.append(el('h4','预期相关工具'),dataView(c.relevant));const table=el('table'),head=el('tr');for(const t of ['排名','实际返回工具','与标注相关'])head.append(el('th',t));table.append(head);
  for(const [i,name] of c.retrieved.entries()){const row=el('tr');row.append(el('td',i+1),el('td',name),el('td',c.relevant.includes(name)?'相关':'不相关'));table.append(row);}if(!c.retrieved.length){const row=el('tr'),cell=el('td','未返回任何候选工具','muted');cell.colSpan=3;row.append(cell);table.append(row);}task.append(table);
 }
 const results={};for(const key of ['answer','grade','checks','failure','observations','events','wrong_click','error','compression_error'])if(c[key]!==undefined)results[fieldNames[key]||key]=c[key];if(Object.keys(results).length)task.append(dataView(results));host.append(task);
 const grading=sectionCard('初评分依据');if(grade){grading.append(el('p',grade.rubric),el('p',grade.reason),el('small',grade.method+' · '+grade.rubric_version));if(Object.keys(grade.dimensions||{}).length)grading.append(dataView(grade.dimensions));}else grading.append(el('p','尚无初评分；请刷新或等待服务加载评分接口。'));host.append(grading);
 if(grade){
 const form=sectionCard('人工复核'),select=el('select');select.setAttribute('aria-label','复核结论');for(const [v,label] of Object.entries(verdictNames)){const o=el('option',v==='adjust'?'修改评分':label);o.value=v;select.append(o);}select.value=human?.verdict||(grade.score===null?'unscorable':'agree');
 const score=el('input');score.type='number';score.min=0;score.max=100;score.step=.01;score.setAttribute('aria-label','复核分数');score.value=human?.score??grade.score??'';
 const note=el('textarea');note.rows=3;note.maxLength=4000;note.placeholder='改分或标记证据不足时，请填写理由';note.setAttribute('aria-label','复核理由');note.value=human?.note||'';
 const sync=()=>{score.disabled=select.value!=='adjust';if(select.value==='agree')score.value=grade.score??'';if(select.value==='unscorable')score.value='';};sync();select.onchange=()=>{annotationDirty=true;sync();};score.oninput=note.oninput=()=>{annotationDirty=true;};
 const controls=el('div',undefined,'filters');controls.append(select,score);const save=el('button','保存人工复核'),message=el('p');message.setAttribute('role','status');
 save.onclick=async()=>{save.disabled=true;message.textContent='保存中…';try{
  if(select.value==='adjust'&&score.value==='')throw Error('请填写复核分数');
  const result=await api('evals/runs/'+id+'/cases/'+index+'/review',{artifact_hash:x.artifact_hash,grade_id:grade.grade_id,base_revision:x.human_review?.revision||0,verdict:select.value,score:select.value==='unscorable'?null:Number(score.value),note:note.value});
  x.human_review=result.human_review;annotationDirty=false;renderTaskDetail(x,id,index);$('detailBody').prepend(el('p','复核已保存，初评分与原始运行结果保持不变。','save-success'));if(selectedRun===id)renderRun().catch(error);
 }catch(e){message.textContent=e.message;save.disabled=false;}};
 form.append(el('p','可认可初评、修改分数或标记证据不足。复核单独保存，不会改写原始任务、参考答案及初评分。'),controls,note,save,message);if(human)form.append(el('small','上次复核：'+new Date(human.created_at).toLocaleString()+' · '+verdictNames[human.verdict]));host.append(form);
 }
 const steps=sectionCard('执行过程');steps.append(el('p',x.trace_note));for(const [i,step] of x.trace.entries()){const d=el('details',undefined,'formatted-step');d.append(el('summary',`${i+1}. ${step.operation||step.stage||'步骤'}${step.duration_ms!==undefined?' · '+step.duration_ms+' ms':''}${step.error?' · 执行错误':''}`),dataView(step));steps.append(d);}host.append(steps);
 host.append(block('原始记录（排查用）',c),el('small','来源：'+x.artifact));
}
