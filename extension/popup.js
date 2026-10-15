const $ = id => document.getElementById(id);
chrome.storage.local.get('server').then(s => { if(s.server) $('server').value=s.server; });
chrome.runtime.sendMessage({type:'status'}).then(s => {$('status').textContent=s.status;});
$('connect').onclick=async()=>{
  $('status').textContent='正在连接…';
  try {
    const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
    const result=await chrome.runtime.sendMessage({type:'connect',tabId:tab.id,server:$('server').value});
    $('status').textContent=result.error || result.status;
  } catch(e){$('status').textContent=e.message;}
};
$('disconnect').onclick=async()=>{const r=await chrome.runtime.sendMessage({type:'disconnect'});$('status').textContent=r.status;};
