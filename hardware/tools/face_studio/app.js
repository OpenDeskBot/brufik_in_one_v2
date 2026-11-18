'use strict';
const $=id=>document.getElementById(id); let savedDoc=FACE_ORIGINAL, draft=FACE_REVISED, originals=savedDoc.emotions, revised=draft.emotions; let sourceRevision=STUDIO.revision, dirty=false, libraryKind='emotions', entryPose=null, lastPose=null;
const META={
 idle:['state','轻轻看向一边，再眨一下眼，安静地陪着你。','增加稳定停留，眨眼短促；循环首尾恢复同一姿态。'],
 happy:['emotion','眼睛笑弯了，嘴角也跟着舒展开。','小幅上扬后落稳，保留温暖腮红，结束自然恢复。'],
 shy:['emotion','悄悄移开目光，有一点不好意思。','侧看、收眼和腮红分阶段发生，避免整张脸一直抖动。'],
 angry:['emotion','皱一下眉，认真表达自己的不满。','缩短持续时间，降低力度；眉毛与眼睛一起表达。'],
 surprised:['emotion','先愣一下，再睁大眼睛：诶？','加入短促准备、大眼睛和圆口，最后缓缓回神。'],
 sad:['emotion','眼神低下来，心情也安静了一点。','移除持续流泪，使用下垂眉眼表达轻度难过。'],
 sleep:['state','闭上眼睛，Zzz 随着呼吸轻轻飘起来。','三个 Z 由小到大向上飘动，逐渐变亮再淡出，循环入梦。'],
 thinking:['state','目光微微向上，正在认真想。','合并原来的“稍等一下”，以微微上看的眼神和三点动画表达思考与等待。'],
 listening:['state','眼睛睁开一点，把注意力留给你。','移除耳边装饰圈，用注视、轻微下沉和眨眼回应。'],
 wake:['response','先偷偷睁一只眼，再打个哈欠，精神起来。','单眼试探、眨眼、哈欠舒展，再睁大眼睛，带着笑意醒来。'],
 greeting:['response','带着笑意，向你眨一下眼睛。','复用开心与单眼眨动，让招呼轻快而完整。'],
 curious:['emotion','一只眼睛睁大一点，想再了解一些。','不对称眼睛旁亮起一个问号，好奇地停留一下再恢复。'],
 got_it:['response','眼睛一亮：懂啦。','眼角的小灯泡亮起、轻闪，再发出暖黄色光芒：灵光一现。'],
 oops:['response','眉毛垮下来：哎呀，糟糕了。','恢复普通的小扁嘴，用挤眼、八字眉和滑落的汗珠表达糟糕。'],
 wink:['response','一个短短的眨眼，带一点俏皮。','单眼闭合后明确停留，再睁开和恢复。'],
};
const meta=name=>libraryKind==='phonemes'?['mouth','按音素改变嘴型，眼神和装饰继续保持。','保存音素关键帧；发音时由设备按下发时长播放和插值。']:META[name]||['emotion','新设计的表情。','调整各帧的图形与时间，预览后保存。'];
const category={mouth:'口型音素',state:'日常状态',emotion:'情绪表达',response:'互动回应'};
let selected='idle',playing=true,elapsed=0,last=performance.now(),compare=false,filter='all';
const total=e=>e.frames.reduce((a,f)=>a+f.ms,0);
const rgb=c=>[((c>>11)&31)*255/31,((c>>5)&63)*255/63,(c&31)*255/31];
function paint(canvas,els){const ctx=canvas.getContext('2d');ctx.setTransform(canvas.width/284,0,0,canvas.height/240,0,0);ctx.fillStyle='#000';ctx.fillRect(0,0,284,240);for(const layer of ['bg','nose','mouth','eye_l','eye_r','extra'])for(const p of els[layer]||[]){ctx.fillStyle=ctx.strokeStyle=p._color||`rgb(${rgb(p.c??65535).join(',')})`;ctx.lineWidth=1;let x=p.x||0,y=p.y||0,w=p.w||0,h=p.h||0,r=p.r??p.radius??0;ctx.beginPath();switch(p.shape){case 'ellipse':case 'ellipse_fill':{const rw=Math.max(0,p.w??p.rw??0),rh=Math.max(0,p.h??p.rh??0);if(!rw||!rh)continue;ctx.ellipse(x,y,rw,rh,0,0,Math.PI*2);p.shape==='ellipse'?ctx.stroke():ctx.fill();break;}case 'circle':case 'circle_outline':if(r<=0)continue;ctx.arc(x,y,r,0,Math.PI*2);p.shape==='circle'?ctx.fill():ctx.stroke();break;case 'line':ctx.moveTo(p.x1||0,p.y1||0);ctx.lineTo(p.x2||0,p.y2||0);ctx.stroke();break;case 'text':ctx.font=`${8*(p.size||1)}px monospace`;ctx.textBaseline='top';ctx.fillText(p.text||'',x,y);break;case 'hline':ctx.moveTo(x,y);ctx.lineTo(x+w,y);ctx.stroke();break;case 'vline':ctx.moveTo(x,y);ctx.lineTo(x,y+h);ctx.stroke();break;case 'round_rect':case 'round_rect_outline':case 'rect':case 'rect_outline':if(w<=0||h<=0)continue;ctx.roundRect(x,y,w,h,Math.max(0,Math.min(r,w/2,h/2)));p.shape.endsWith('outline')?ctx.stroke():ctx.fill();break;}}}
const norm=p=>({...p,x:p.x||0,y:p.y||0,w:p.w??p.rw??0,h:p.h??p.rh??0,r:p.r??p.radius??0,x1:p.x1||0,y1:p.y1||0,x2:p.x2||0,y2:p.y2||0,c:p.c??65535,size:p.size??1,text:p.text??''});
const round=x=>Math.sign(x)*Math.round(Math.abs(x));
function colorLerp(a,b,t){return (round(((a>>11)&31)*(1-t)+((b>>11)&31)*t)<<11)|(round(((a>>5)&63)*(1-t)+((b>>5)&63)*t)<<5)|round((a&31)*(1-t)+(b&31)*t);}
function blendPrimitive(old,p,t){
 old=old&&norm(old);p=p&&norm(p);
 if(!old)return {...p,c:colorLerp(0,p.c,t)};
 if(!p)return {...old,c:colorLerp(old.c,0,t)};
 if(t<=0)return old;if(t>=1)return p;
 if(old.shape!==p.shape||old.text!==p.text||old.size!==p.size){const q=t<.5?old:p;return {...q,c:t<.5?colorLerp(q.c,0,t*2):colorLerp(0,q.c,(t-.5)*2)};}
 const n={...p,c:colorLerp(old.c,p.c,t)};for(const k of ['x','y','w','h','r','x1','y1','x2','y2'])n[k]=round(old[k]*(1-t)+p[k]*t);return n;
}
function blendLayers(prev,curr,t){const out={};for(const layer of ['bg','nose','mouth','eye_l','eye_r','extra']){const a=prev?.[layer]||[],b=curr[layer]||[];if(!b.length){out[layer]=a;continue;}if(t>=1){out[layer]=b.map(norm);continue;}out[layer]=Array.from({length:Math.max(a.length,b.length)},(_,i)=>blendPrimitive(a[i],b[i],t));}return out;}
function sample(scene,time,start=null){
 let offset=0,prev=start||{};
 // A mouth-only preview starts with the saved idle face if no face has been shown yet.
 if(libraryKind==='phonemes'&&!start)prev=(draft.emotions.find(e=>e.name==='idle')||draft.emotions[0]).frames[0].elements;
 for(let i=0;i<scene.frames.length;i++){
  const f=scene.frames[i],els=libraryKind==='phonemes'?{mouth:f.elements.mouth}:f.elements;
  if(time<offset+f.ms||i===scene.frames.length-1)return {els:blendLayers(prev,els,Math.max(0,Math.min(1,(time-offset)/f.ms))),index:i};
  prev=blendLayers(prev,els,1);offset+=f.ms;
 }
}

function draw(){const e=revised.find(e=>e.name===selected),dur=total(e);const pose=sample(e,elapsed,entryPose);lastPose=pose.els;paint($('revised'),pose.els);if(compare){const old=originals.find(e=>e.name===selected);if(old)paint($('original'),sample(old,elapsed/dur*total(old)).els);else paint($('original'),{});}$('clock').textContent=`${(elapsed/1000).toFixed(2)} / ${(dur/1000).toFixed(2)} s`;$('timeline').value=elapsed/dur*1000;$('frame').textContent=`第 ${pose.index+1} / ${e.frames.length} 帧`;}
function select(name){if(dirty&&!applyScene(false))return;entryPose=lastPose?structuredClone(lastPose):null;selected=name;elapsed=0;last=performance.now();playing=true;const e=revised.find(e=>e.name===name),m=meta(name);$('sceneEditor').value=JSON.stringify(e,null,2);dirty=false;$('title').textContent=e.title;$('name').textContent=name;$('category').textContent=category[m[0]];$('description').textContent=m[1];$('changes').textContent=m[2];$('duration').textContent=(total(e)/1000).toFixed(2)+' s';$('count').textContent=e.frames.length;$('play').textContent='Ⅱ 暂停';$('play').setAttribute('aria-label','暂停动画');document.querySelectorAll('.card').forEach(c=>c.setAttribute('aria-pressed',c.dataset.name===name));draw();}
function cards(){const root=$('cards');root.replaceChildren();for(const e of revised){if(filter!=='all'&&meta(e.name)[0]!==filter)continue;const b=document.createElement('button');b.className='card';b.dataset.name=e.name;b.setAttribute('aria-pressed',e.name===selected);b.setAttribute('aria-label','播放'+e.title);const c=document.createElement('canvas');c.width=568;c.height=480;c.setAttribute('aria-hidden','true');b.appendChild(c);const row=document.createElement('div');row.className='card-bottom';const title=document.createElement('strong'),id=document.createElement('small');title.textContent=e.title;id.textContent=e.name;row.append(title,id);b.appendChild(row);b.onclick=()=>select(e.name);root.appendChild(b);paint(c,sample(e,total(e)*.45).els);}}
$('play').onclick=()=>{playing=!playing;if(playing&&elapsed>=total(revised.find(e=>e.name===selected)))elapsed=0;last=performance.now();$('play').textContent=playing?'Ⅱ 暂停':'▶ 播放';$('play').setAttribute('aria-label',playing?'暂停动画':'播放动画');};$('restart').onclick=()=>select(selected);
$('compare').onclick=()=>{compare=!compare;$('compare').setAttribute('aria-pressed',compare);$('oldFigure').hidden=!compare;$('oldFigure').querySelector('figcaption').textContent='已保存 · 同比例进度';draw();};
$('timeline').oninput=()=>{playing=false;elapsed=Number($('timeline').value)/1000*total(revised.find(e=>e.name===selected));$('play').textContent='▶ 播放';$('play').setAttribute('aria-label','播放动画');draw();};
document.querySelectorAll('[data-filter]').forEach(b=>b.onclick=()=>{filter=b.dataset.filter;document.querySelectorAll('[data-filter]').forEach(x=>x.classList.toggle('active',x===b));cards();});
function tick(now){let dt=Math.min(now-last,100);last=now;if(playing){const dur=total(revised.find(e=>e.name===selected));elapsed+=dt*Number($('speed').value);if(elapsed>=dur){if($('loop').checked){entryPose=sample(revised.find(e=>e.name===selected),dur,entryPose).els;elapsed%=dur;}else{elapsed=dur;playing=false;$('play').textContent='▶ 播放';$('play').setAttribute('aria-label','播放动画');}}draw();}requestAnimationFrame(tick);}cards();select(revised.some(e=>e.name==='idle')?'idle':revised[0].name);if(matchMedia('(prefers-reduced-motion: reduce)').matches){playing=false;$('play').textContent='▶ 播放';$('play').setAttribute('aria-label','播放动画');}requestAnimationFrame(tick);

function status(message,error=false){$('saveStatus').textContent=message;$('saveStatus').style.color=error?'#b64030':'';}
function applyScene(announce=true){
 try { const e=JSON.parse($('sceneEditor').value);
  validateScene(e);
  if(revised.some(x=>x.name===e.name&&x.name!==selected))throw Error('表情 ID 已存在。');
  const index=revised.findIndex(x=>x.name===selected);revised[index]=e;dirty=false;selected=e.name;
  cards();select(selected);if(announce)status('已应用到预览，尚未保存。');return true;
 } catch(error){status(error.message,true);return false;}
}
$('sceneEditor').oninput=()=>{dirty=true;status('有未应用的修改。');};
$('applyScene').onclick=()=>applyScene();
$('saveLibrary').onclick=async()=>{
 if(dirty&&!applyScene(false))return;
 const button=$('saveLibrary');button.disabled=true;status('正在检查并保存…');
 try{const response=await fetch('/save',{method:'POST',headers:{'Content-Type':'application/json','X-Studio-Token':STUDIO.token},body:JSON.stringify({document:draft,revision:sourceRevision})});const result=await response.json();if(!response.ok)throw Error(result.error);
 sourceRevision=result.revision;savedDoc=structuredClone(draft);originals=savedDoc[libraryKind];$('resourceVersion').textContent='资源版本：'+result.version;
 status('已保存并生成固件资源。编译烧录后，设备即可使用这一版表情。');
 }catch(error){status(error.message,true);}finally{button.disabled=false;}
};
$('downloadDraft').onclick=()=>{if(dirty&&!applyScene(false))return;const a=document.createElement('a');const url=URL.createObjectURL(new Blob([JSON.stringify(draft,null,2)],{type:'application/json'}));a.href=url;a.download='deskbot-face.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
$('newScene').onclick=()=>{if(dirty&&!applyScene(false))return;let i=1;while(revised.some(e=>e.name==='custom_'+i))i++;const e=structuredClone(revised.find(x=>x.name==='idle')||revised[0]);e.name='custom_'+i;e.title='新表情 '+i;e.alias=[];revised.push(e);filter='all';document.querySelectorAll('[data-filter]').forEach(x=>x.classList.toggle('active',x.dataset.filter==='all'));cards();select(e.name);status('已新建表情，编辑数据后保存。');};
$('importLibrary').onchange=async event=>{try{const file=event.target.files[0];if(!file)return;const doc=JSON.parse(await file.text());if(!Array.isArray(doc.emotions)||!doc.emotions.length)throw Error('缺少 emotions 表情列表。');if(!Array.isArray(doc.phonemes))throw Error('请导入完整表情库，保留 phonemes 口型数据。');doc.emotions.forEach(e=>validateScene(e,false));doc.phonemes.forEach(e=>validateScene(e,true));if(new Set(doc.emotions.map(e=>e.name)).size!==doc.emotions.length)throw Error('表情 ID 重复。');draft=doc;revised=draft[libraryKind];dirty=false;cards();select(revised[0].name);status('已导入预览，保存后替换源文件；保存时会检查固件限制。');}catch(error){status(error.message,true);}};
$('resourceVersion').textContent='资源版本：'+STUDIO.version;

function validateScene(e,mouth=libraryKind==='phonemes'){
 if(!e||typeof e.name!=='string'||!(mouth?/^[A-Za-z_üÜ][A-Za-z0-9_üÜ]{0,14}$/:/^[a-z][a-z0-9_]{0,47}$/).test(e.name)||!Array.isArray(e.frames)||!e.frames.length||e.frames.length>64)throw Error('请检查表情 ID 和帧列表（1–64 帧）。');
 const shapes=['rect','rect_outline','circle','circle_outline','line','ellipse','ellipse_fill','round_rect','round_rect_outline','text'];
 for(const f of e.frames){
  if(!f||!Number.isInteger(f.ms)||f.ms<40||f.ms>30000||!f.elements||typeof f.elements!=='object')throw Error('每帧需要有效的 elements 和 ms（至少 40 毫秒）。');
  for(const layer of (mouth?['mouth']:['nose','mouth','eye_l','eye_r','extra']))if(!Array.isArray(f.elements[layer])||!f.elements[layer].length)throw Error('每帧必须保留五个图层，用黑色或零尺寸图形表示隐藏。');
  for(const ps of Object.values(f.elements)){
   if(!Array.isArray(ps)||ps.length>16)throw Error('每个图层最多 16 个图形。');
   for(const p of ps){if(!p||!shapes.includes(p.shape))throw Error('包含设备不支持的图形。');for(const [k,v] of Object.entries(p)){if(!['shape','text'].includes(k)&&(!Number.isInteger(v)||Math.abs(v)>65535))throw Error('图形参数需要使用整数。');}}
  }
 }
 if(total(e)>10000)throw Error('单个表情最长 10 秒。');
}

$('libraryKind').onchange=event=>{
 if(dirty&&!applyScene(false)){event.target.value=libraryKind;return;}
 libraryKind=event.target.value;revised=draft[libraryKind];originals=savedDoc[libraryKind];filter='all';
 document.querySelectorAll('[data-filter]').forEach(x=>x.classList.toggle('active',x.dataset.filter==='all'));
 document.querySelector('.filters').hidden=libraryKind==='phonemes';$('newScene').textContent=libraryKind==='phonemes'?'＋ 新建口型':'＋ 新建表情';document.querySelector('.library h2').textContent=libraryKind==='phonemes'?'每个音素，都有自己的口型。':'今天是什么心情？';cards();select(revised[0].name);
 status(libraryKind==='phonemes'?'口型仅覆盖嘴部；保存会同时更新表情与音素资源。':'切换表情会从当前显示姿态继续插值。');
};
