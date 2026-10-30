const statusBox = document.getElementById('status');
const controls = document.querySelector('.controls');
const modelPanel = document.createElement('div');
modelPanel.className='controls';
modelPanel.innerHTML='<b>模型切换</b><button id="providerToggle" type="button" role="switch" aria-label="切换到 DeepSeek" aria-checked="false" disabled><span class="toggle-thumb"></span><span class="toggle-label">Codex</span><span class="toggle-label">DeepSeek</span></button><p id="modelState" class="note">读取配置中…</p>';
const switchStyle=document.createElement('style');
switchStyle.textContent=`#providerToggle{position:relative;isolation:isolate;display:flex;width:100%;height:54px;padding:5px;margin:18px 0 10px;border:1px solid #3b5273;border-radius:30px;background:#0a1526;align-items:center;color:#8fa3c9;transition:opacity .2s}#providerToggle:hover{filter:none;border-color:#88bafa}#providerToggle:focus-visible{outline:3px solid #a8d5ff;outline-offset:4px}#providerToggle:disabled{cursor:wait;opacity:.5}.toggle-thumb{position:absolute;z-index:-1;left:5px;top:5px;bottom:5px;width:calc(50% - 5px);border-radius:24px;background:linear-gradient(120deg,#4779d9,#5c92ec);box-shadow:0 3px 14px #2265c044;transition:transform .22s ease,background .22s ease}#providerToggle[aria-checked="true"] .toggle-thumb{transform:translateX(100%);background:linear-gradient(120deg,#168b99,#31adba)}.toggle-label{flex:1;font-weight:600;transition:color .22s}#providerToggle[aria-checked="false"] .toggle-label:nth-child(2),#providerToggle[aria-checked="true"] .toggle-label:nth-child(3){color:white}@media(prefers-reduced-motion:reduce){.toggle-thumb,.toggle-label{transition:none}}`;
document.head.append(switchStyle);
controls.before(modelPanel);
const quotaInfo=document.createElement('p');quotaInfo.className='note';quotaInfo.id='quotaState';modelPanel.append(quotaInfo);
function renderPolicy(policy){
 const window=policy.windows?.five_hour;let text=policy.reason||'正在读取额度';
 if(window){text+=' · 五小时剩余 '+window.remaining_percent+'%';if(window.resets_at){const minutes=Math.max(0,Math.ceil((window.resets_at*1000-Date.now())/60000));text+=' · '+Math.floor(minutes/60)+'小时'+minutes%60+'分钟后重置';}}
 quotaInfo.textContent=text+(policy.error?' · '+policy.error:'');
}
const tools=document.createElement('div');
tools.innerHTML='<label><input type="checkbox" id="voiceEnabled" checked style="width:auto"> 播放晓伊原声</label><button id="resetSession">清空对话上下文</button><p id="metrics" class="note">等待测试</p><audio id="replyAudio" controls style="width:100%"></audio><label>最近测试对比</label><div id="comparisons" style="overflow-x:auto"></div>';
controls.append(tools);
const audio=document.getElementById('replyAudio');
let inFlight=false,ticker,started,selectedProvider='',modelName='',availableProviders=[];
const measurements=[];
const providerToggle=document.getElementById('providerToggle');
function busy(value){inFlight=value;providerToggle.disabled=value||!selectedProvider;document.getElementById('resetSession').disabled=value;}
function renderProvider(data){selectedProvider=data.provider;modelName=data.model;if(Array.isArray(data.providers))availableProviders=data.providers.filter(p=>p.configured).map(p=>p.id);const canSwitch=availableProviders.length>1;document.getElementById('modelState').textContent='当前：'+modelName+(canSwitch?'':' · 唯一可用');providerToggle.setAttribute('aria-checked',String(selectedProvider!=='codex'));providerToggle.setAttribute('aria-label',canSwitch?'切换模型供应商，当前 '+selectedProvider:'当前唯一已配置的供应商 '+selectedProvider);providerToggle.disabled=inFlight||!canSwitch;}
async function post(path,data){const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const result=await response.json();if(!response.ok)throw Error(result.error||'请求失败');return result;}
providerToggle.onclick=async()=>{if(inFlight||!selectedProvider)return;const order=availableProviders.length>1?availableProviders:['codex','deepseek','workbuddy'];const next=order[(order.indexOf(selectedProvider)+1)%order.length]||'codex';if(next===selectedProvider)return;busy(true);try{const data=await post('/api/provider',{provider:next});renderProvider(data);audio.pause();statusBox.textContent='● 已切换到 '+data.model+' · 保留近期聊天';}catch(err){statusBox.textContent='切换失败：'+err.message;}finally{busy(false);}};
const seconds=ms=>ms==null?'—':(ms/1000).toFixed(2)+'s';
function record(data){measurements.unshift(data);measurements.splice(8);const table=document.createElement('table');table.style.cssText='width:100%;font-size:11px;border-spacing:8px;text-align:left';const head=table.createTHead().insertRow();['模型','首字','文本','语音','总计'].forEach(s=>{const th=document.createElement('th');th.textContent=s;head.append(th);});const body=table.createTBody();measurements.forEach(d=>{const row=body.insertRow();[d.model,seconds(d.first_text_ms),seconds(d.llm_ms),seconds(d.tts_ms),seconds(d.total_ms)].forEach(s=>{row.insertCell().textContent=s;});});document.getElementById('comparisons').replaceChildren(table);}
async function submitStudioEvent(event){
 const result=await post('/api/studio/event',{...event,voice:document.getElementById('voiceEnabled').checked});
 const isGift=event.type==='gift';
 statusBox.textContent='● '+(result.message||'已收到');
 document.dispatchEvent(new CustomEvent('studio-event-submitted'));
 return result;
}
async function ask(text){
 if(!text.trim()){statusBox.textContent='请输入弹幕';return;}
 if(text.length>500){statusBox.textContent='弹幕最多500字';return;}
 try{await submitStudioEvent({id:crypto.randomUUID(),platform:'douyin',roomId:'local',type:'chat',ts:Date.now(),user:{id:'local:测试观众',nickname:'测试观众'},data:{content:text.trim()}});}
 catch(err){statusBox.textContent='错误：'+err.message;}
}
function showReply(data){
 statusBox.textContent='● '+data.model+' 回复完成'+(data.audio_error?' · '+data.audio_error:'');
 let toolResult=document.getElementById('musicToolResult');
 if(!toolResult){toolResult=document.createElement('p');toolResult.id='musicToolResult';toolResult.className='note';document.getElementById('metrics').after(toolResult);}
 toolResult.textContent=(data.tool_calls||[]).map(call=>{const r=call.result||{},t=r.track||{};if(r.status==='queued')return '点歌：已加入队列《'+t.title+'》 · '+t.artist;if(r.status==='withdrawn')return '点歌：已撤回《'+t.title+'》';const label=({get_music_queue:'查询队列',get_today_stats:'查询收支',search_recent_chat:'查询聊天'})[call.tool]||'点歌';return label+'：'+(r.message||'工具未完成');}).join('；');
 if(data.event.type==='chat'){
  document.getElementById('metrics').textContent=`首字 ${seconds(data.first_text_ms)} · 文本 ${seconds(data.llm_ms)} · 语音 ${seconds(data.tts_ms)} · 总计 ${seconds(data.total_ms)}`+(data.codex_pid?' · PID '+data.codex_pid:'')+(data.usage?' · 用量 '+JSON.stringify(data.usage):'');record(data);
 }
}

document.getElementById('send').onclick=()=>{const el=document.getElementById('comment');const v=el.value.trim();if(!v)return;ask(v);el.blur();};

document.getElementById('resetSession').onclick=async()=>{if(inFlight)return;busy(true);try{await post('/api/reset',{});statusBox.textContent='两个模型的对话上下文已清空';}catch(err){statusBox.textContent=err.message;}finally{busy(false);}};
document.querySelectorAll('.note').forEach(p=>{if(p.textContent.includes('当前未接入'))p.textContent='已接入 Codex 与 DeepSeek Flash，可随时切换；晓伊原声共用。网易云点歌与真实 Live2D 已接入。首字为服务端测量值，当前网页仍等待文字和语音全部完成后显示。';});

const restoreDaily=document.getElementById('idle').onclick;document.getElementById('idle').onclick=async()=>{restoreDaily();try{const visual=await post('/api/studio/visual-reset',{});document.dispatchEvent(new CustomEvent('whale-visual',{detail:visual}));}catch(error){statusBox.textContent=error.message;}};

fetch('/api/status').then(r=>r.json()).then(d=>{renderProvider(d);if(d.policy)renderPolicy(d.policy);statusBox.textContent='● '+d.model+' 已就绪 · 晓伊原声';}).catch(()=>statusBox.textContent='后端未连接');
