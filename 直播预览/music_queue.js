/* Polling preserves the rolling position; the copy makes the wrap continuous. */
(()=>{
 const el=(tag,cls,text)=>{const node=document.createElement(tag);node.className=cls;if(text!=null)node.textContent=text;return node;};
 class MusicQueueView{
  constructor(root,heading){
   this.root=root;this.heading=heading;this.key=null;this.policy={};this.lastFrame=0;this.holdUntil=0;this.hover=false;this.cycleHeight=0;this.offset=0;this.lastWritten=0;
   this.now=el('div','music-current');this.now.append(el('span','music-playing-label','当前播放'));
   this.currentTitle=el('strong','music-current-title');this.currentArtist=el('span','music-current-artist');this.now.append(this.currentTitle,this.currentArtist);
   this.eq=el('span','music-eq');for(let i=0;i<4;i++)this.eq.append(document.createElement('i'));this.now.append(this.eq);
   this.next=el('div','music-next');this.notice=el('div','music-schedule-status');
   this.list=el('div','music-pending');this.list.tabIndex=0;this.list.setAttribute('aria-label','点歌滚榜，按先后顺序待播');
   this.track=el('div','music-roll-track');this.cycle=el('div','music-roll-cycle');this.cycle.setAttribute('role','list');this.track.append(this.cycle);this.list.append(this.track);
   this.footer=el('div','music-board-footer','发「点歌 歌名」加入待播');root.replaceChildren(this.now,this.next,this.notice,this.list,this.footer);
   this.list.addEventListener('mouseenter',()=>{this.hover=true;});this.list.addEventListener('mouseleave',()=>{this.hover=false;});
   for(const type of ['wheel','pointerdown','keydown'])this.list.addEventListener(type,()=>{this.holdUntil=performance.now()+8000;});
   this.observer=new ResizeObserver(()=>this.measure());this.observer.observe(this.list);this.observer.observe(this.cycle);
   const frame=time=>{this.scroll(time);requestAnimationFrame(frame);};requestAnimationFrame(frame);
  }
  update(m){
   const queue=m.queue||[],current=(m.player||{}).current;this.policy=m.limits||{};
   this.heading.textContent=queue.length+' 首';
   const empty=document.getElementById('queueEmpty');if(empty)empty.hidden=queue.length>0||!!current;
   this.footer.textContent='发「点歌 歌名」加入待播';
   this.currentTitle.textContent=current?.title||'等待音乐响起';this.currentArtist.textContent=current?.artist||'一起听点喜欢的';
   this.now.title=current?current.title+' · '+current.artist:'当前暂无播放';
   const nativeNext=(m.player||{}).next;
   this.next.textContent=nativeNext?'下一首 · '+nativeNext.title+' / '+nativeNext.artist:'';
   // The first waiting row already names the next song; do not repeat it above.
   this.next.hidden=!nativeNext||(queue.length>0&&String(nativeNext.id)===String(queue[0].track?.id));this.next.title=this.next.textContent;
   const command=m.command,age=command?Date.now()/1000-command.time:Infinity;
   this.notice.classList.toggle('music-command-error',!!command&&command.status!=='queued');
   this.notice.textContent=age<10?(command.status==='queued'?(command.nickname||'观众')+' · 《'+(command.track?.title||'歌曲')+'》已上榜':command.message):m.error?'点歌暂未完成 · 待播保留':queue.length&&m.enabled===false?'待播保留 · 下次点歌继续接管':'';
   this.notice.hidden=!this.notice.textContent;this.notice.title=m.error||'';
   const key=JSON.stringify(queue.map(item=>[item.id,item.track?.title,item.track?.artist,item.nickname]));
   if(key===this.key)return;this.key=key;const oldTop=this.list.scrollTop;this.cycle.replaceChildren();
   if(!queue.length)this.cycle.append(el('div','music-empty','空位留给你的下一首好歌'));
   queue.forEach((item,i)=>{
    const row=el('div','music-song'+(i===0?' music-song-first':''));row.setAttribute('role','listitem');row.dataset.id=item.id;
    const rank=el('span','music-song-rank',String(i+1).padStart(2,'0'));
    const body=el('div','music-song-body'),title=el('div','music-song-title',item.track?.title||'未知歌曲');
    const detail=el('div','music-song-detail');detail.append(el('span','music-song-artist',item.track?.artist||'未知歌手'),el('span','music-song-viewer',(item.nickname||'观众')+' 点播'));
    body.append(title,detail);row.append(rank,body);row.title=title.textContent+' · '+detail.textContent;this.cycle.append(row);
   });
   this.list.scrollTop=oldTop;this.offset=oldTop;this.lastWritten=this.list.scrollTop;this.holdUntil=performance.now()+1500;this.measure();
  }
  measure(){
   this.track.querySelector('.music-roll-copy')?.remove();this.cycleHeight=this.cycle.getBoundingClientRect().height;
   this.overflow=this.cycleHeight>this.list.clientHeight+1;
   if(this.overflow){const copy=this.cycle.cloneNode(true);copy.classList.add('music-roll-copy');copy.setAttribute('aria-hidden','true');copy.removeAttribute('role');this.track.append(copy);}
   else this.list.scrollTop=0;
  }
  scroll(time){
   const dt=this.lastFrame?Math.min((time-this.lastFrame)/1000,.1):0;this.lastFrame=time;
   if(!this.overflow||time<this.holdUntil||!this.policy.queue_auto_scroll||window.matchMedia?.('(prefers-reduced-motion: reduce)').matches)return;
   if(this.hover&&!document.documentElement.classList.contains('audience-view'))return;
   // scrollTop may round subpixel writes. Accumulate fractions independently.
   if(Math.abs(this.list.scrollTop-this.lastWritten)>1)this.offset=this.list.scrollTop;
   this.offset=this.cycleHeight>0?(this.offset+dt*(this.policy.queue_scroll_pixels_per_second??18))%this.cycleHeight:0;
   this.list.scrollTop=this.offset;this.lastWritten=this.list.scrollTop;
  }
 }
 window.MusicQueueView=MusicQueueView;
})();
