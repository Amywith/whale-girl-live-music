/* Douyin/music management. The audience stage never receives credentials. */
(() => {
 const panel=document.querySelector('.panel');
 const box=document.createElement('section');box.className='controls integrations';
 box.innerHTML=`<h2>抖音接入</h2><p class="note">DyHub 已集成，点「连接抖音」会自动启动。先在直播伴侣开播，再填自己的直播间网页链接。<a href="https://github.com/ymstar/dyhub/blob/main/docs/cookie-guide.md" target="_blank" rel="noopener">登录状态说明</a></p>
 <label for="douyinRoom">抖音房间号或直播间链接</label><input id="douyinRoom" placeholder="https://live.douyin.com/数字房间号">
 <details><summary>接收真实礼物：设置抖音网页登录状态</summary><p class="note">在浏览器登录抖音直播网页，F12 → Network，刷新后选择 live.douyin.com 请求，在 Request Headers 复制完整 Cookie，仅粘贴到下面。只验证弹幕可先留空；伴侣登录不会自动带到采集器。此输入不保存到磁盘，也不要发到聊天中。更新后重新连接。</p><input id="douyinCookie" type="password" autocomplete="off" placeholder="登录 Cookie，仅传给本机采集进程"></details>
 <div class="buttons"><button id="douyinConnect">连接抖音</button><button id="douyinDisconnect">断开抖音</button></div><p class="note" id="douyinState">未连接</p>
 <h2>礼物测试</h2><p class="note">普通弹幕择取回复，礼物优先；逐条播完再处理下一条。真实价格读取礼物事件；模拟价格用于测试，可手动修改。按钻石分档，不估算人民币。</p>
 <button id="retryReplies" hidden>重试播放</button><p class="note" id="studioState">发弹幕或送礼物后直接回复</p>
 <label for="giftUser">模拟送礼人</label><input id="giftUser" value="小水"><label for="giftName">礼物名称</label><input id="giftName" value="喵喵爪">
 <div class="inline-fields"><div><label for="giftPrice">单价（钻石）</label><input id="giftPrice" type="number" min="0" placeholder="未知可留空"></div><div><label for="giftCount">数量</label><input id="giftCount" type="number" min="1" value="1"></div></div>
 <button id="simulateGift">提交模拟礼物</button><p class="note" id="giftTiers"></p><p class="note" id="meowAsset"></p>
 <details><summary>最近事件</summary><div id="eventLog"></div></details>
 <p class="note" id="musicState" role="status" hidden></p><p id="integrationNotice" class="note" role="status"></p>`;
 panel.append(box);
 const style=document.createElement('style');style.textContent='.integrations h2{font-size:18px;margin:28px 0 8px}.integrations h2:first-child{margin-top:0}.integrations details{margin:15px 0;color:#a9c1de;font-size:13px}.integrations summary{cursor:pointer}.inline-fields{display:flex;gap:12px}.inline-fields>div{flex:1;min-width:0}.integration-row{padding:10px 0;border-bottom:1px solid #2b3d57;font-size:13px;line-height:1.7}.integration-row button{padding:5px 10px;font-size:12px;margin:5px 6px 0 0}#eventLog{max-height:260px;overflow:auto;font-size:12px;line-height:1.8}.integrations button:disabled{opacity:.5;cursor:wait}.pvz-loop-panel details{margin:12px 0}.pvz-loop-panel pre{max-height:220px;overflow:auto;font-size:12px;line-height:1.7;color:#9fc7ea;background:#0a1526;border:1px solid #2d405d;border-radius:8px;padding:10px;margin:8px 0 0;white-space:pre-wrap}';document.head.append(style);
 let playing=false,polling=false,claiming=false,viewPolling=false,ack=null,ackFailed=false;
 const clientId=crypto.randomUUID();
 const voicePlayer=new VoicePlayer(audio);
 const themePanel=document.createElement('div');themePanel.className='controls stage-theme-panel';
 themePanel.innerHTML='<b>营业场景</b><button id="stageThemeToggle" class="stage-theme-toggle" role="switch" aria-label="切换到夜晚营业" aria-checked="false" disabled><span>☀ 白天营业</span><span>☾ 夜晚营业</span></button><p id="stageThemeState" class="note">正在读取场景…</p><button id="stageThemeAuto" class="stage-theme-auto">跟随北京时间</button>';
 modelPanel.after(themePanel);
 // ── 角色语音音量：实时增益，不必重启 ──
 const voicePanel=document.createElement('div');voicePanel.className='controls voice-panel';
 voicePanel.innerHTML='<b>🔊 角色语音音量</b><p class="note">直播里大肥鱼说话的声音大小，改完立即生效。游戏声音请用系统“音量混合器”单独调低。</p><label for="voiceGain">语音增益 <span id="voiceGainVal">1.4×</span></label><input id="voiceGain" type="range" min="0.6" max="2.0" step="0.1" value="1.4"><p class="note">1.0=原声；调太高可能破音，1.3~1.6 比较稳。</p>';
 themePanel.after(voicePanel);
 const voiceGain=$('voiceGain'),voiceGainVal=$('voiceGainVal');
 voiceGain.oninput=()=>{const v=parseFloat(voiceGain.value);voiceGainVal.textContent=v.toFixed(1)+'×';voicePlayer.setGain(v);};
 let effectiveTheme='day';
 function renderTheme(visual){
  const theme=visual?.time_theme;if(!theme)return;effectiveTheme=theme.effective;
  $('stageThemeToggle').disabled=false;$('stageThemeToggle').setAttribute('aria-checked',String(effectiveTheme==='night'));
  $('stageThemeToggle').setAttribute('aria-label',effectiveTheme==='day'?'切换到夜晚营业':'切换到白天营业');
  $('stageThemeState').textContent=(theme.mode==='auto'?'跟随北京时间':'手动选择')+' · '+(effectiveTheme==='day'?'浅色桌布':'深色桌布');
 }
 async function changeTheme(mode){
  $('stageThemeToggle').disabled=true;$('stageThemeAuto').disabled=true;
  try{const visual=await post('/api/studio/theme',{mode});renderTheme(visual);document.dispatchEvent(new CustomEvent('whale-visual',{detail:visual}));}
  catch(error){notice(error.message,true);}
  finally{$('stageThemeToggle').disabled=false;$('stageThemeAuto').disabled=false;}
 }
 $('stageThemeToggle').onclick=()=>changeTheme(effectiveTheme==='day'?'night':'day');
 $('stageThemeAuto').onclick=()=>changeTheme('auto');
 // ── PvZ 循环自玩：开游戏 + 挂循环看门狗（本地策略，零成本挂机） ──
 const pvzPanel=document.createElement('div');pvzPanel.className='controls pvz-loop-panel';
 pvzPanel.innerHTML='<b>🎮 PvZ 循环自玩</b><p class="note" id="pvzLoopState">正在读取…</p><div class="buttons"><button id="pvzLoopStart">▶ 启动循环</button><button id="pvzLoopStop">■ 停止循环</button><button id="pvzLoopClose" title="停止看门狗并关闭游戏">■ 停止并关游戏</button></div><p class="note">启动后自动开游戏（若未开）并挂循环看门狗：自动进关、本地策略自己玩、打完自动续关，无限循环不调模型。</p><details><summary>看门狗日志</summary><pre id="pvzLoopLog"></pre></details>';
 themePanel.after(pvzPanel);
 function renderPvz(s){
  const map={idle:'未运行',launching:'启动中…',running:'循环运行中',error:'出错'};
  const el=$('pvzLoopState');
  el.textContent='● '+(map[s.state]||s.state)+(s.message?' · '+s.message:'')+(s.game_ready?' · 游戏已就绪':' · 游戏未启动');
  el.style.color=s.state==='error'?'#ffb3ae':s.state==='running'?'#9fe6b0':'#a9c9ec';
  $('pvzLoopStart').disabled=s.state==='launching';
  $('pvzLoopStop').disabled=s.state==='idle';
  $('pvzLoopClose').disabled=s.state==='idle';
  $('pvzLoopLog').textContent=(s.log||[]).join('\n')||'（暂无日志）';
 }
 let pvzPolling=false;
 async function pvzPoll(){if(pvzPolling)return;pvzPolling=true;try{const r=await fetch('/api/pvz/loop/status');if(r.ok)renderPvz(await r.json());}catch(_){}finally{pvzPolling=false;}}
 $('pvzLoopStart').onclick=async()=>{const b=$('pvzLoopStart');b.disabled=true;$('pvzLoopState').textContent='● 启动中… 游戏若未开会先开游戏（约 20-60 秒）';try{await post('/api/pvz/loop/start',{});}catch(e){notice(e.message,true);}finally{b.disabled=false;}pvzPoll();};
 $('pvzLoopStop').onclick=async()=>{try{const d=await post('/api/pvz/loop/stop',{});$('pvzLoopState').textContent='● '+d.message;}catch(e){notice(e.message,true);}pvzPoll();};
 $('pvzLoopClose').onclick=async()=>{if(!confirm('停止循环并关闭 PvZ 游戏？'))return;try{const d=await post('/api/pvz/loop/stop',{close_game:true});$('pvzLoopState').textContent='● '+d.message;}catch(e){notice(e.message,true);}pvzPoll();};
 setInterval(pvzPoll,5000);pvzPoll();
 let playbackEpoch=null;
 let serverAudioWarning=false;
 function syncPlaybackEpoch(epoch){
  if(!Number.isInteger(epoch))return true;
  if(playbackEpoch!==null&&epoch<playbackEpoch)return false;
  if(playbackEpoch!==null&&epoch>playbackEpoch){voicePlayer.cancel();ack=null;ackFailed=false;playing=false;busy(false);}
  playbackEpoch=epoch;return true;
 }
 let hasGesture=!!navigator.userActivation?.hasBeenActive;
 document.addEventListener('pointerdown',event=>{if(event.isTrusted){hasGesture=true;voicePlayer.unlock();}},{capture:true});
 document.addEventListener('keydown',event=>{if(event.isTrusted){hasGesture=true;voicePlayer.unlock();}},{capture:true});
  // 浏览器自动播放策略：页面未获得点击前不许出声。观众视角看不到后台提示，这里补一个可见入口。
  let everPlayed=false;
  const audioPrompt=document.createElement('div');
  audioPrompt.className='audio-unlock';audioPrompt.textContent='';
  document.querySelector('.stage').append(audioPrompt);
  const autoplayAllowed=()=>{try{return !!(navigator.getAutoplayPolicy&&navigator.getAutoplayPolicy('mediaelement')==='allowed');}catch(_){return false;}};
  const syncAudioPrompt=()=>{audioPrompt.hidden=true;};
  document.addEventListener('pointerdown',event=>{
   if(!event.isTrusted)return;
   hasGesture=true;voicePlayer.unlock();audioPrompt.hidden=true;
   if(voicePlayer.blocked||serverAudioWarning){serverAudioWarning=false;void post('/api/studio/retry',{}).then(()=>claim()).catch(()=>{});}
  },{capture:true});
  setInterval(syncAudioPrompt,800);syncAudioPrompt();
 const notice=(text,error=false)=>{const n=$('integrationNotice');n.textContent=text;n.style.color=error?'#ffb3ae':'#a9c9ec';};
 async function action(path,data={}){try{const d=await post(path,data);notice(d.message||'已完成');await refresh();return d;}catch(e){notice(e.message,true);throw e;}}
 function bind(id,path,data){$(id).onclick=async()=>{const button=$(id);button.disabled=true;try{await action(path,typeof data==='function'?data():data||{});}catch(_){}finally{button.disabled=false;}};}
 bind('douyinConnect','/api/douyin/connect',()=>({room_id:$('douyinRoom').value,cookie:$('douyinCookie').value}));
 $('douyinConnect').addEventListener('click',()=>{setTimeout(()=>{$('douyinCookie').value='';},0);});
 bind('douyinDisconnect','/api/douyin/disconnect');
 bind('retryReplies','/api/studio/retry');
 $('retryReplies').textContent='恢复／重试语音';$('status').after($('retryReplies'));
 const event=(type,data,nickname='测试观众')=>({id:crypto.randomUUID(),platform:'douyin',roomId:'local',type,ts:Date.now(),user:{id:'local:'+nickname,nickname},data});
 let simulationPrices={},priceSignature=null;
 function fillGiftPrice(){const price=simulationPrices[$('giftName').value.trim()];$('giftPrice').value=price===undefined?'':String(price);}
 $('giftName').addEventListener('input',fillGiftPrice);
 function renderGiftPrices(prices){const signature=JSON.stringify(prices||{});if(signature!==priceSignature){simulationPrices=prices||{};priceSignature=signature;fillGiftPrice();}}
 function giftData(){return event('gift',{giftId:'simulation:'+$('giftName').value,giftName:$('giftName').value,diamondCount:$('giftPrice').value===''?null:Number($('giftPrice').value),repeatCount:Number($('giftCount').value),repeatEnd:true,combo:false},$('giftUser').value||'测试观众');}
 $('simulateGift').onclick=async()=>{const b=$('simulateGift');b.disabled=true;try{const result=await submitStudioEvent(giftData());notice(result.message);await refresh();}catch(e){notice(e.message,true);}finally{b.disabled=false;}};
 $('gift').textContent='🎁 模拟礼物（使用下方配置）';$('gift').onclick=()=>$('simulateGift').click();
 document.querySelector('label[for="comment"]').textContent='模拟观众弹幕（聊天、点歌都在这里说）';
 function row(text){const el=document.createElement('div');el.className='integration-row';el.textContent=text;return el;}
 const musicQueueView=new MusicQueueView($('songs'),document.getElementById('queueCount'));
 const presentationView=new PresentationView($('conversation'),$('chat'),$('bubble'));
 async function refreshPresentation(){if(viewPolling)return;viewPolling=true;try{const r=await fetch('/api/studio/presentation');if(!r.ok)throw Error('画面同步失败');const d=await r.json();presentationView.update(d.presentation);if(d.visual)document.dispatchEvent(new CustomEvent('whale-visual',{detail:d.visual}));}catch(e){notice(e.message,true);}finally{viewPolling=false;}}
 $('clear').onclick=async()=>{try{presentationView.update(await post('/api/studio/presentation/clear',{}));notice('所有画面的显示记录已清空');}catch(e){notice(e.message,true);}};
 $('clearQueues').onclick=async()=>{const button=$('clearQueues');button.disabled=true;try{const result=await post('/api/studio/queues/clear',{});syncPlaybackEpoch(result.playback_epoch);notice(result.message);await refresh();await refreshPresentation();}catch(e){notice(e.message,true);}finally{button.disabled=false;}};
 $('resetStats').onclick=async()=>{if(!confirm('清空全部收入、支出、礼物数量和token统计？聊天、点歌及模型额度保持不变。清空前会自动备份。'))return;const button=$('resetStats');button.disabled=true;try{await action('/api/studio/stats/reset');}catch(_){}finally{button.disabled=false;}};
 function renderMusic(m){
  $('musicState').hidden=!m.error;$('musicState').textContent=m.error?'点歌暂未完成：'+m.error:'';
  musicQueueView.update(m);
 }
 function renderIncome(income,douyin={},policy={},expenses={}){
  if(!income)return;
  // Show test gifts in the unconnected preview; never merge them into live totals.
  const preview=!douyin.room_id&&!income.today.live.gift_count;
  const today=income.today[preview?'simulation':'live'];
  const rate=policy.yuan_per_diamond??0.05;
  const yuan=value=>(value*rate).toFixed(2);
  const incomeTodayEl=$('incomeToday');if(incomeTodayEl){incomeTodayEl.textContent=yuan(today.coins);incomeTodayEl.title=income.day+' 礼物总值 '+today.coins.toLocaleString()+' 钻石 × '+rate+' 元；预计到账，实际以平台结算为准';}
  const spent=expenses.estimated_yuan||0;
  const expenseTodayEl=$('expenseToday');if(expenseTodayEl)expenseTodayEl.textContent='¥'+spent.toFixed(3);
  const incomeNetEl=$('incomeNet');if(incomeNetEl)incomeNetEl.textContent='¥'+(today.coins*rate-spent).toFixed(2);
  const token=$('tokenToday');if(token)token.textContent=(expenses.tokens||0).toLocaleString();
  const foot=$('ledgerFoot');if(foot)foot.textContent=(today.coins||0).toLocaleString()+' 音浪 · '+(today.gift_count||0)+' 份礼物';
 }
 function renderFeeders(list){
  const ol=document.getElementById('feedRank');if(!ol)return;
  const items=(list||[]).filter(e=>e&&e.coins>0).slice(0,6);
  if(!items.length){ol.innerHTML='<li class="dim">还没有投喂，快来当榜一</li>';return;}
  ol.innerHTML='';
  items.forEach((e,i)=>{
   const li=document.createElement('li');
   const rank=document.createElement('span');rank.className='rank r'+(i+1);rank.textContent=String(i+1);
   const name=document.createElement('b');name.textContent=e.name||'匿名观众';
   const coins=document.createElement('em');coins.textContent=(e.coins||0).toLocaleString()+' 音浪';
   li.append(rank,name,coins);ol.append(li);
  });
 }
 async function refresh(){
  if(polling)return;polling=true;
  try{
   const r=await fetch('/api/studio/status');if(!r.ok)throw Error('后台状态读取失败');const d=await r.json();
   if(!syncPlaybackEpoch(d.studio.playback_epoch))return;
   renderGiftPrices(d.studio.simulation_gift_prices);renderIncome(d.studio.income,d.douyin,d.visual?.income,d.expenses);
   if(d.models)renderProvider(d.models);if(d.policy)renderPolicy(d.policy);renderTheme(d.visual);
   renderFeeders(d.studio.feeders);
   serverAudioWarning=!!d.studio.audio_warning;
    $('retryReplies').hidden=!(d.studio.playback_blocked||d.studio.error||d.studio.audio_warning||d.studio.voice_retries);if(!playing)busy(d.studio.processing);
   if(d.studio.audio_warning)notice(d.studio.audio_warning+'；文字回复继续，可点击“恢复／重试语音”',true);
   $('voiceEnabled').checked=d.studio.voice;
   $('studioState').textContent=(d.studio.playback_blocked?'语音播放中断，请点击重试播放':d.studio.chat_selection==='window_random'?'真实弹幕：最近 '+d.studio.chat_window_size+' 人随机选一条，礼物优先':'弹幕按队列回复')+' · 待处理 '+d.studio.pending+' 条 · 合并中的礼物 '+d.studio.combos+' 组'+(d.studio.error?' · '+d.studio.error:'');
   const counts=d.douyin.event_counts||{};
   $('douyinState').textContent='状态：'+d.douyin.state+(d.douyin.room_id?' · 房间 '+d.douyin.room_id:'')+(d.douyin.login_configured===false?' · 未设置网页登录，可能漏掉礼物':d.douyin.login_configured===true?' · 已设置登录 Cookie（有效性待实收验证）':'')+' · 收到弹幕 '+(counts.chat||0)+' / 礼物 '+(counts.gift||0)+' / 进场 '+(counts.member||0)+(d.douyin.error?' · '+d.douyin.error:'');
   const feedConn=$('feedConnection');if(feedConn)feedConn.textContent=d.douyin.room_id?'抖音 · '+d.douyin.room_id:'未连接抖音';
   document.querySelector('header small').textContent='本地管理 / 抖音 '+d.douyin.state;
   $('giftTiers').textContent='感谢分档（整组总价）：'+d.studio.tiers.map(t=>t.min_diamonds+'钻石起：'+t.name).join('；')+'。可在 '+(d.studio.config_file||'直播规则.yaml')+' 调整。';
   $('meowAsset').textContent=d.studio.meow_clip?'喵喵爪：晓伊感谢＋本地喵叫音效':'喵喵爪：晓伊念完整文案；表哥原声音效尚未放入。';
   $('eventLog').replaceChildren();d.studio.history.slice().reverse().forEach(e=>{const el=document.createElement('div');el.textContent=new Date(e.time*1000).toLocaleTimeString()+' '+e.text;$('eventLog').append(el);});renderMusic({...d.music,command:d.studio.music_command});
  }catch(e){notice(e.message,true);}finally{polling=false;}
 }

 async function playResponse(d){const epoch=playbackEpoch;playing=true;busy(true);ack=d.id;ackFailed=false;try{clearTimeout(timer);showReply(d);for(const part of d.audio_parts||[d.audio].filter(Boolean)){if(epoch!==playbackEpoch)return;await voicePlayer.play(part);}everPlayed=true;audioPrompt.hidden=true;if(d.audio_error)notice(d.audio_error,true);}catch(e){if(e.cancelled||epoch!==playbackEpoch)return;ackFailed=true;statusBox.textContent='● 语音暂不可用，文字回复继续';notice(e.message+'；可点击“恢复／重试语音”',true);}finally{if(epoch===playbackEpoch){playing=false;busy(false);}}}
 async function claim(voice){if(claiming)return;claiming=true;try{const sentAck=!playing?ack:null;const request={client_id:clientId,ack:sentAck,failed:ackFailed,audio_ready:!voicePlayer.blocked,failure_reason:ackFailed?voicePlayer.lastError:'',role:document.documentElement.classList.contains('audience-view')&&(hasGesture||autoplayAllowed())&&!voicePlayer.blocked?'live':'preview'};if(typeof voice==='boolean')request.voice=voice;const result=await post('/api/studio/playback',request);if(!syncPlaybackEpoch(result.playback_epoch))return;if(sentAck){ack=null;ackFailed=false;}if(result.response&&!playing){void refreshPresentation();void playResponse(result.response);}}catch(e){notice(e.message,true);}finally{claiming=false;}}
 document.querySelector('.panel>p').textContent='以直播画面为准。左侧聊天簿显示已回应的对话，底部滚动显示弹幕与礼物；预览与直播页同步。';
 const notes=document.querySelectorAll('.panel>p.note');if(notes.length>1)notes[1].textContent='聊天、点歌都直接发弹幕。点歌工具自动准备网易云；抖音直播时填写房间号即可。大肥鱼已接入真实 Live2D，可通过弹幕与礼物改变。';
 $('voiceEnabled').onchange=()=>{void claim($('voiceEnabled').checked);};
 document.addEventListener('studio-event-submitted',()=>{void refreshPresentation();void claim();});
 refresh();refreshPresentation();claim();
 /* 轮询放 Web Worker：Chrome 会把后台标签页的主线程定时器压到每分钟一次，
    导致回复生成好了也要等近一分钟才被取走播放；Worker 定时器不受此节流。 */
 (function(){
   let n=0;
   const tick=()=>{n++;claim();if(n%2===0)void refreshPresentation();if(n%8===0)void refresh();};
   try{
     const src='let t=null;onmessage=e=>{if(e.data===1&&!t){t=setInterval(()=>postMessage(1),250);}};';
     const worker=new Worker(URL.createObjectURL(new Blob([src],{type:'text/javascript'})));
     worker.onmessage=()=>tick();
     worker.postMessage(1);
   }catch(_){setInterval(tick,250);}
 })();
})();
