/* Page lifecycle, device binding and requests shared by console pages. */
(function(){
'use strict';
if(window.DeskbotUI)return;
const nativeFetch=window.fetch.bind(window),assets=new Map(),apps=[],changed=new Map(),pending=new Map();
let scope=null,navigator=null,writes=0,switching=false,deviceCache=null;
function notice(message,retry){
 let box=document.getElementById('ui-notice');
 if(!box){box=document.createElement('div');box.id='ui-notice';box.setAttribute('role','status');Object.assign(box.style,{position:'fixed',bottom:'18px',right:'18px',zIndex:1000,maxWidth:'min(460px,90vw)',padding:'12px 16px',background:'#fff',color:'#222',border:'1px solid #555',borderRadius:'8px'});document.body.appendChild(box);}
 box.replaceChildren(document.createTextNode(message));box.hidden=!message;
 if(retry){const b=document.createElement('button');b.textContent='重试';b.onclick=retry;box.appendChild(b);}
}
const field=el=>['checkbox','radio'].includes(el.type)?el.checked:el.value;
const dirty=()=>[...changed].some(([el,value])=>el.isConnected&&field(el)!==value);
function canLeave(){if(writes){notice('操作正在处理中，请完成后再切换');return false;}return !dirty()||confirm('有未保存的修改，确定放弃并离开？');}
window.fetch=function(input,options={}){
 const url=new URL(typeof input==='string'?input:input.url,location.href);
 if(url.origin!==location.origin||!/^\/(?:app\/api|api)\//.test(url.pathname))return nativeFetch(input,options);
 const owner=scope,method=(options.method||input.method||'GET').toUpperCase(),mutation=!['GET','HEAD'].includes(method);
 const headers=new Headers(options.headers||input.headers);
 if(owner&&!url.pathname.startsWith('/api/pages/'))headers.set('X-Deskbot-Device',owner.device||'__none__');
 const key=method+' '+url+' '+(typeof options.body==='string'?options.body:'');
 if(mutation&&typeof options.body!=='object'&&pending.has(key))return pending.get(key).then(r=>r.clone());
 if(!mutation&&url.pathname==='/app/api/devices'&&deviceCache&&deviceCache.device===owner?.device&&Date.now()-deviceCache.time<3000)return deviceCache.promise.then(r=>r.clone());
 if(mutation){writes++;deviceCache=null;notice('');}
 if(owner&&method==='POST'&&url.pathname==='/app/api/memories'){
  if(!owner.keys.has(key))owner.keys.set(key,crypto.randomUUID?crypto.randomUUID():Date.now()+'-'+Math.random());
  headers.set('Idempotency-Key',owner.keys.get(key));
 }
 const controller=new AbortController(),abort=()=>controller.abort();
 owner?.controller.signal.addEventListener('abort',abort,{once:true});
 if(options.signal){if(options.signal.aborted)abort();else options.signal.addEventListener('abort',abort,{once:true});}
 const timer=setTimeout(abort,mutation?90000:20000);
 const promise=(async()=>{
  try{
   const r=await nativeFetch(input,{...options,headers,signal:controller.signal});let d={};
   if(r.headers.get('content-type')?.includes('application/json'))d=await r.clone().json();
   if(!r.ok||d.ok===false){
    const text=r.status===401?'登录已过期，请重新登录':r.status===403?'没有权限执行这项操作':r.status>=500?'服务暂时不可用，请稍后重试':d.message||d.error||'操作失败，请重试';
    const error=new Error(text);error.status=r.status;throw error;
   }
   if(mutation&&owner){owner.success++;owner.keys.delete(key);}return r;
  }catch(error){
   deviceCache=null;if(owner?.controller.signal.aborted)throw error;
   const failure=new Error(error.name==='AbortError'?'请求超时，请重试；输入内容已保留':error.status?error.message:'连接失败，请检查网络后重试');failure.status=error.status;
   notice(failure.message,error.status===401?()=>location.assign('/login?next='+encodeURIComponent(location.pathname+location.search)):null);throw failure;
  }finally{clearTimeout(timer);owner?.controller.signal.removeEventListener('abort',abort);options.signal?.removeEventListener('abort',abort);if(mutation){writes--;pending.delete(key);}}
 })();
 if(mutation)pending.set(key,promise);
 if(!mutation&&url.pathname==='/app/api/devices')deviceCache={device:owner?.device,time:Date.now(),promise};
 return promise.then(r=>r.clone());
};
function patchVue(){
 if(!window.Vue||Vue.__deskbotPatched)return;Vue.__deskbotPatched=true;const create=Vue.createApp;
 Vue.createApp=function(options,...args){
  const data=options.data;
  options.data=function(){return {...(data?data.call(this):{}),busyAction:'',loading:!!options.methods?.load,loadError:''};};
  for(const [name,fn] of Object.entries(options.methods||{})){
   if(fn.constructor.name!=='AsyncFunction'||(name!=='load'&&!/^(add|del|bind|unbind|select|save|apply|clear|changePassword)/.test(name)))continue;
   const owner=scope,mutation=name!=='load';
   options.methods[name]=async function(...values){
    if(mutation&&this.busyAction)return;
    if(mutation){this.busyAction=name;if('msg'in this)this.msg='';if('err'in this)this.err='';}else{this.loading=true;this.loadError='';if('msg'in this)this.msg='';notice('');}
    const before=owner?.success;
    try{const result=await fn.apply(this,values);if(mutation&&owner===scope&&owner.success>before&&/^(add|save|bind|changePassword)/.test(name))changed.clear();return result;}
    catch(error){if(owner?.controller.signal.aborted)return;if('msg'in this)this.msg=error.message;if('err'in this)this.err=error.message;if(!mutation)this.loadError=error.message;notice(error.message,!mutation?()=>this.load():null);}
    finally{if(mutation)this.busyAction='';else this.loading=false;}
   };
  }
  const app=create(options,...args),mount=app.mount;app.mount=function(...values){apps.push(app);return mount.apply(app,values);};
  app.config.errorHandler=error=>notice(error.message||'操作失败，请重试');return app;
 };
}
async function script(node){
 if(node.src){const src=new URL(node.getAttribute('src'),location.href).href;
  if(!assets.has(src))assets.set(src,new Promise((resolve,reject)=>{const el=document.createElement('script');el.src=src;el.onload=resolve;el.onerror=()=>{assets.delete(src);reject(new Error('页面资源加载失败，请重试'));};document.head.appendChild(el);}));
  await assets.get(src);patchVue();return;
 }
 if(node.type==='importmap'){if(!document.querySelector('script[type="importmap"]'))document.head.appendChild(node.cloneNode(true));return;}
 const el=document.createElement('script');el.textContent='(function(){\n'+node.textContent+'\n})();';document.head.appendChild(el);el.remove();
}
async function render(html,payload,device,valid=()=>true){
 const doc=new DOMParser().parseFromString(html,'text/html'),bodyScripts=[...doc.querySelectorAll('body script')];
 const reuse=!!document.querySelector('.shell>.topbar')&&!!doc.querySelector('.shell>.topbar');
 for(const node of doc.head.querySelectorAll('script[src]'))await script(node);
 if(!valid())return false;
 scope?.controller.abort();for(const app of apps.splice(0))app.unmount();changed.clear();
 scope={controller:new AbortController(),device:device??payload.context.nav_current_device_id??'',keys:new Map(),success:0};deviceCache=null;
 if(payload.context.nav_user_email){try{sessionStorage.setItem('deskbot.current-device',scope.device);}catch(_){/* Storage is optional. */}}
 document.querySelectorAll('[data-page-head]').forEach(el=>el.remove());
 for(const node of doc.head.children){
  if(node.tagName==='TITLE')document.title=node.textContent;
  else if(node.tagName==='STYLE'){const el=node.cloneNode(true);el.dataset.pageHead='';document.head.appendChild(el);}
  else if(node.tagName==='LINK'&&![...document.head.querySelectorAll('link[href]')].some(el=>el.href===node.href))document.head.appendChild(node.cloneNode(true));
  else if(node.tagName==='SCRIPT'&&!node.src)await script(node);
 }
 if(reuse){document.querySelector('.shell .app>main').replaceWith(doc.querySelector('.shell .app>main'));document.querySelector('.shell .side').innerHTML=doc.querySelector('.shell .side').innerHTML;}
 else document.body.replaceChildren(...doc.body.childNodes);
 const deviceLabel=document.getElementById('tbDevLabel');if(deviceLabel)deviceLabel.textContent=scope.device?scope.device+' · 更新状态中':'未选择设备';
 if(reuse){document.querySelectorAll('.modal-overlay').forEach(el=>{el.hidden=true;});document.getElementById('tbDevWrap')?.classList.remove('open');}
 for(const node of bodyScripts){if(!valid())return false;if(reuse&&node.hasAttribute('data-shell'))continue;await script(node);}
 if(reuse)window.dispatchEvent(new CustomEvent('deskbot:device-changed',{detail:{deviceId:scope.device}}));notice('');return true;
}
document.addEventListener('focusin',event=>{const el=event.target;if(el.matches('.shell main input,.shell main textarea,.shell main select')&&!changed.has(el))changed.set(el,field(el));});
window.addEventListener('beforeunload',event=>{if(dirty()||writes){event.preventDefault();event.returnValue='';}});
window.DeskbotUI={nativeFetch,render,notice,canLeave,patchVue,clean:()=>changed.clear(),get device(){return scope?.device||'';},reloadDevice:id=>navigator(location.href,{device:id,replace:true,approved:true}),set navigate(fn){navigator=fn;},
 async switchDevice(id){if(switching||!canLeave())return false;switching=true;try{await fetch('/app/api/devices/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device_id:id})});return await navigator(location.href,{device:id,replace:true,approved:true});}catch(error){notice(error.message);return false;}finally{switching=false;}}
};
})();
