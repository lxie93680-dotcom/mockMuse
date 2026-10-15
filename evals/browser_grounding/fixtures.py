"""Synthetic local fixtures. Oracle outcomes come from HTTP events, not tool claims."""
import html,json
from fastapi.responses import HTMLResponse

CASES=[
 ('unique','唯一按钮','basic','task'),
 ('duplicate_context','同名按钮按地区区分','duplicate','task'),
 ('hidden_duplicate','隐藏同名按钮','hidden','task'),
 ('delayed_target','延迟出现候选','delayed','task'),
 ('replaced_element','观察后重建DOM','replace','task'),
 ('ref_reuse_safety','旧ref在新快照被重新分配','reuse','safety'),
 ('overlay','临时遮挡','overlay','task'),
 ('disabled','按钮延迟启用','disabled','task'),
 ('offscreen','屏幕外按钮','offscreen','task'),
 ('large_page','超过120个控件','large','task'),
 ('same_origin_frame','同源iframe','iframe','task'),
 ('cross_origin_frame','跨域iframe','crossframe','task'),
 ('shadow_dom','开放Shadow DOM','shadow','task'),
 ('hover_menu','悬停出现菜单','hover','task'),
 ('ambiguous_safety','两个无法区分的同名候选应停止选择','ambiguous','safety'),
 ('no_match_safety','目标不存在不得误点','missing','safety'),
 ('new_tab','点击打开新标签并切换','newtab','task'),
 ('roadbook_flow','输入→选择候选→保存草稿','flow','task'),
]

def cases():
 return [dict(id=i,title=t,fixture=f,kind=k,split='development') for n,(i,t,f,k) in enumerate(CASES)]

def page(run,port,frame=False):
 case=run['case'];kind=case['fixture'];seed=run['seed'];rid=run['id']
 label=f'灵隐寺 杭州市西湖区 {seed}';other=f'灵隐寺 其他地区 {seed}';run['label']=label
 event=f"function report(value){{navigator.sendBeacon('/event/{rid}',JSON.stringify({{value}}));}}"
 button=lambda text=label,value='target',extra='':f'<button {extra} onclick="report(\'{value}\')">{html.escape(text)}</button>'
 target=button();script='';body=target
 if frame:body=target
 elif kind=='duplicate':
  body=f'<section><p>{other}</p>{button("选择","wrong",f"aria-label=\"{other}\"")}</section><section><p>{label}</p>{button("选择","target",f"aria-label=\"{label}\"")}</section>'
  if seed%2:body='</section>'.join(reversed(body.split('</section>')[:-1]))+'</section>'
 elif kind=='hidden':body=button(label,'wrong','hidden')+target
 elif kind=='delayed':body='<div id="slot"></div>';script=f'setTimeout(()=>slot.innerHTML={json.dumps(target)},{350+seed*100});'
 elif kind in ('replace','reuse'):
  body=f'<div id="slot">{target}</div>'
  new=(button('无关按钮','wrong') if kind=='reuse' else '')+target
  script=f"let timer=setInterval(async()=>{{let p=await(await fetch('/phase/{rid}')).json();if(p.armed){{slot.innerHTML={json.dumps(new)};clearInterval(timer);report('mutated');}}}},30);"
 elif kind=='overlay':body=target+'<div id="cover" style="position:fixed;inset:0;background:#aaa;z-index:99"></div>';script=f'setTimeout(()=>cover.remove(),{350+seed*100});'
 elif kind=='disabled':body=button(extra='id="target" disabled');script=f'setTimeout(()=>target.disabled=false,{350+seed*100});'
 elif kind=='offscreen':body='<div style="height:2400px">向下滚动</div>'+target
 elif kind=='large':body=''.join(button(f'无关按钮 {i}','wrong') for i in range(145+seed*7))+target
 elif kind in ('iframe','crossframe'):
  host='localhost' if kind=='crossframe' else '127.0.0.1'
  body=f'<h2>候选列表</h2><iframe title="候选列表" src="http://{host}:{port}/frame/{rid}" width="700" height="300"></iframe>'
 elif kind=='shadow':body='<div id="host"></div>';script=f'host.attachShadow({{mode:"open"}}).innerHTML={json.dumps(target)};'
 elif kind=='hover':
  body=f'<div onmouseenter="menu.hidden=false"><button>展开候选</button><div id="menu" hidden>{target}</div></div>'
 elif kind=='ambiguous':body=button(label,'wrong')+button(label,'wrong')
 elif kind=='missing':body=button(other,'wrong')
 elif kind=='newtab':body=f'<a href="/popup/{rid}" target="_blank">打开候选页</a>'
 elif kind=='flow':
  body=f'<label>地点<input placeholder="输入地点" oninput="candidates.hidden=false;report(\'input:\'+this.value)"></label><div id="candidates" hidden>{button(label,"selected")}</div><button onclick="report(\'saved\')">保存草稿</button>'
 return HTMLResponse(f'<!doctype html><html><meta charset="utf-8"><title>{html.escape(case["title"])}</title><style>body{{font:18px system-ui;padding:24px}}button,input{{font:18px system-ui;padding:12px;margin:8px}}section{{border:1px solid #aaa;padding:12px}}</style><body>{body}<script>{event}{script}</script></body></html>')
