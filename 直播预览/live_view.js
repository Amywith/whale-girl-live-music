/* Switching views keeps the existing player, queue and model session alive. */
(()=>{
 // A still-running older backend keeps its original UI until a manual restart.
 const clock=document.getElementById('beijingClock');
 const timeFormat=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
 const dateFormat=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',weekday:'long'});
 let clockAnchor=null;
 const dial=document.getElementById('clockDial');
 const timeParts=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
 function renderHands(now){
  if(!dial)return;
  const parts=timeParts.formatToParts(now),pick=t=>Number((parts.find(p=>p.type===t)||{}).value||0);
  const h=pick('hour')%12,m=pick('minute'),s=pick('second');
  dial.style.setProperty('--h',(h*30+m*.5)+'deg');
  dial.style.setProperty('--m',(m*6+s*.1)+'deg');
  dial.style.setProperty('--s',(s*6)+'deg');
 }
 function renderClock(){
  if(!clock||document.hidden)return;
  const now=new Date(clockAnchor?clockAnchor.unixMs+performance.now()-clockAnchor.mono:Date.now());
  clock.textContent=timeFormat.format(now);clock.dateTime=now.toISOString();renderHands(now);
  const dateEl=document.getElementById('clockDate');
  if(dateEl){const parts=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'numeric',day:'numeric',weekday:'short'}).formatToParts(now);const g=t=>(parts.find(x=>x.type===t)||{}).value||'';dateEl.textContent=g('month')+'月'+g('day')+'日 '+g('weekday');}
  clock.title=dateFormat.format(now)+' · 北京时间 UTC+8';
 }
 async function syncClock(){
  if(document.hidden)return;
  const began=performance.now();
  try{
   const response=await fetch('/api/time',{cache:'no-store'});
   if(!response.ok)return;
   const data=await response.json(),end=performance.now();
   if(Number.isFinite(data.unix_ms))clockAnchor={unixMs:data.unix_ms+(end-began)/2,mono:end};
   renderClock();
  }catch{/* Keep ticking locally if the backend is temporarily restarting. */}
 }
 renderClock();syncClock();setInterval(renderClock,1000);setInterval(syncClock,60000);
 document.addEventListener('visibilitychange',()=>{if(!document.hidden){renderClock();syncClock();}});
 const control=document.createElement('a');control.className='view-link';control.id='audienceView';control.href='/?view=live';control.textContent='查看直播画面';control.title='切换到纯净直播画面；按 Esc 返回后台';
 const tools=document.createElement('div');tools.className='header-tools';
 const header=document.querySelector('header');header.append(tools);tools.append(header.querySelector('small'),control);
 function applyStageTheme(visual){
  const theme=visual?.time_theme;if(!theme)return;
  const day=theme.effective==='day';document.querySelector('.stage').dataset.theme=theme.effective;
  const period=document.getElementById('businessPeriod');if(period)period.textContent=theme.label;
  const paperFooter=document.getElementById('shopPaperFooter');if(paperFooter)paperFooter.textContent=day?'今天，也要好好吃饭。':'今晚，也要好好吃饭。';
 }
 document.addEventListener('whale-visual',event=>applyStageTheme(event.detail));
 function setView(enabled,replace=false){
  const chat=document.getElementById('chat'),follow=chat?chat.scrollHeight-chat.scrollTop-chat.clientHeight<30:false;
  document.documentElement.classList.toggle('audience-view',enabled);
  const avatar=new URLSearchParams(location.search).get('view')==='avatar'&&!replace;
  document.documentElement.classList.toggle('avatar-view',avatar);
  document.documentElement.classList.toggle('chroma-view',avatar&&new URLSearchParams(location.search).get('bg')==='green');
  document.title='大肥鱼 · '+(enabled?'直播间':'直播控制台');
  const bubble=document.getElementById('bubble');if(enabled&&bubble.textContent.includes('在右边发条弹幕'))bubble.textContent='本鲸开工啦，陪你聊会儿～';
  if(replace){const url=new URL(location.href);if(enabled)url.searchParams.set('view','live');else url.searchParams.delete('view');history.replaceState(null,'',url);}
  if(follow)requestAnimationFrame(()=>{chat.scrollTop=chat.scrollHeight;});
  if(enabled)requestAnimationFrame(()=>{for(const feed of document.querySelectorAll('.conversation-list,.chat'))feed.scrollTop=feed.scrollHeight;});
 }
 control.addEventListener('click',event=>{event.preventDefault();setView(true,true);});
 document.addEventListener('keydown',event=>{if(event.key==='Escape'&&document.documentElement.classList.contains('audience-view')){setView(false,true);control.focus();}});
 addEventListener('popstate',()=>setView(['live','avatar'].includes(new URLSearchParams(location.search).get('view'))));
 setView(['live','avatar'].includes(new URLSearchParams(location.search).get('view')));
 if(document.documentElement.classList.contains('audience-view'))document.getElementById('bubble').textContent='本鲸开工啦，陪你聊会儿～';
})();
