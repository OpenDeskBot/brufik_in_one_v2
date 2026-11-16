(async function(){
'use strict';
const ui=window.DeskbotUI,version=new URL(document.currentScript.src).searchParams.get('v')||'';
let views=null,controller=null,serial=0,displayed=location.pathname+location.search;
function templates(){
 if(!views){
  const abort=new AbortController(),timeout=setTimeout(()=>abort.abort(),20000);
  views=ui.nativeFetch('/static/views.json?v='+version,{signal:abort.signal}).then(r=>{if(!r.ok)throw new Error('页面资源加载失败');return r.json();})
   .catch(error=>{views=null;throw new Error(error.name==='AbortError'?'页面资源读取超时，请重试':error.message);}).finally(()=>clearTimeout(timeout));
 }
 return views;
}
async function navigate(raw,{device,replace=false,approved=false,initial=false,pop=false}={}){
 const oldY=window.scrollY,url=new URL(raw,location.href);if(!initial&&!approved&&!ui.canLeave())return false;
 const turn=++serial;controller?.abort();controller=new AbortController();const requestController=controller;let timedOut=false;const deadline=setTimeout(()=>{timedOut=true;requestController.abort();},25000);const timer=setTimeout(()=>{if(turn===serial)ui.notice('正在打开页面…');},180);
 try{
  const headers={Accept:'application/json'};if(!initial || device!==undefined)headers['X-Deskbot-Device']=(device??ui.device)||'__none__';
  const [response,bundle]=await Promise.all([ui.nativeFetch('/api/pages'+url.pathname+url.search,{headers,credentials:'same-origin',signal:controller.signal}),templates()]);
  if(turn!==serial)return false;
  if(response.status===401){location.assign('/login?next='+encodeURIComponent(url.pathname+url.search));return false;}
  if(initial && response.status===403 && device!==undefined){try{sessionStorage.removeItem('deskbot.current-device');}catch(_){}return navigate(raw,{initial:true,approved:true});}
  const payload=await response.json();if(payload.redirect)return navigate(payload.redirect,{replace:true,approved:true});
  if(!response.ok)throw new Error(payload.message||payload.error||'页面暂时无法加载');
  const html=DeskbotPages.render(bundle,payload);if(turn!==serial)return false;
  if(!initial&&!html.includes('class="topbar"')){ui.clean();if(device!==undefined){try{sessionStorage.setItem('deskbot.current-device',device);}catch(_){}}location.assign(url.href);return true;}
  if(!await ui.render(html,payload,device,()=>turn===serial) || turn!==serial)return false;
  if(!initial&&!pop){history.replaceState({...history.state,y:oldY},'',displayed);}
  if(!initial&&!pop)history[replace?'replaceState':'pushState']({deskbot:true},'',url.pathname+url.search+url.hash);
  else if(initial)history.replaceState({deskbot:true},'',location.pathname+location.search);
  displayed=url.pathname+url.search+url.hash;
  document.querySelector('main h1')?.setAttribute('tabindex','-1');document.querySelector('main h1')?.focus({preventScroll:true});window.scrollTo(0,pop?(history.state?.y||0):0);return true;
 }catch(error){if(turn!==serial || (error.name==='AbortError'&&!timedOut))return false;const text=timedOut?'页面读取超时，请重试':error.message||'连接失败，请重试';ui.notice(text,()=>navigate(raw,{device,replace,approved:true,initial}));const status=document.getElementById('page-status');if(status)status.textContent='页面暂时无法加载，请点击重试';return false;}
 finally{clearTimeout(timer);clearTimeout(deadline);}
}
ui.navigate=navigate;
document.addEventListener('click',event=>{
 const a=event.target.closest('a[href]');if(!a||event.defaultPrevented||event.button!==0||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey||a.target||a.hasAttribute('download'))return;
 const url=new URL(a.href,location.href);if(url.origin!==location.origin||(url.hash&&url.pathname===location.pathname)||!document.querySelector('.shell>.topbar'))return;
 if(/^\/(?:static|api|app\/api)\//.test(url.pathname))return;event.preventDefault();void navigate(url.href);
});
window.addEventListener('popstate',()=>{void navigate(location.href,{pop:true}).then(ok=>{if(!ok)history.pushState({deskbot:true},'',displayed);});});
let selected;try{if(/^\/(login|register)$/.test(location.pathname))sessionStorage.removeItem('deskbot.current-device');else{const stored=sessionStorage.getItem('deskbot.current-device');if(stored!==null)selected=stored;}}catch(_){}
await navigate(location.href,{initial:true,approved:true,device:selected});
})();
