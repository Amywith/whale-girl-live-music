/* Every window renders the same server state; only the audio lease owner speaks. */
(()=>{
 const cleanReply=value=>String(value||'').replace(/&lt;/gi,'<').replace(/&gt;/gi,'>').replace(/\\([<>/])/g,'$1').replace(/<(watch|visual)\b[^>]*>[\s\S]*?(?:<\/\1\s*>|$)/gi,'').replace(/<\/(watch|visual)\s*>/gi,'').trim();
 class FeedView{
  constructor(root,conversation){
   this.root=root;this.conversation=conversation;this.signature=null;this.content=root;
   if(!root)return;
   if(root.classList.contains('ticker-feed')){
    this.track=document.createElement('div');this.track.className='incoming-track';
    this.content=document.createElement('div');this.content.className='incoming-cycle';this.track.append(this.content);root.replaceChildren(this.track);
    this.offset=0;this.lastWritten=0;this.lastFrame=0;this.cycleWidth=0;
    this.observer=new ResizeObserver(()=>this.measure());this.observer.observe(root);this.observer.observe(this.content);
    const frame=time=>{this.scroll(time);requestAnimationFrame(frame);};requestAnimationFrame(frame);
   }
  }
  measure(){
   this.track?.querySelector('.incoming-copy')?.remove();this.cycleWidth=this.content.getBoundingClientRect().width;
   if(this.track&&this.cycleWidth>this.root.clientWidth+1){const copy=this.content.cloneNode(true);copy.classList.add('incoming-copy');copy.setAttribute('aria-hidden','true');this.track.append(copy);}
   else if(this.track){this.root.scrollLeft=0;this.offset=0;this.lastWritten=0;}
  }
  scroll(time){
   const dt=this.lastFrame?Math.min((time-this.lastFrame)/1000,.1):0;this.lastFrame=time;
   if(this.cycleWidth<=this.root.clientWidth+1||window.matchMedia?.('(prefers-reduced-motion: reduce)').matches)return;
   if(Math.abs(this.root.scrollLeft-this.lastWritten)>1)this.offset=this.root.scrollLeft;
   this.offset=(this.offset+dt*26)%this.cycleWidth;this.root.scrollLeft=this.offset;this.lastWritten=this.root.scrollLeft;
  }
  update(items){
   if(!this.root)return;
   items=items.map(item=>item.role==='assistant'?{...item,content:cleanReply(item.content)}:item);
   if(this.conversation)items=items.filter(item=>!(item.type==='video'&&item.role==='user'&&item.content.startsWith('正在看：')));
   if(this.track)items=items.slice(-8);
   const signature=JSON.stringify(items);if(signature===this.signature)return;
   const first=this.signature===null,follow=first||document.documentElement.classList.contains('audience-view')||this.root.scrollHeight-this.root.scrollTop-this.root.clientHeight<35;
   const top=this.root.scrollTop,oldHeight=this.root.scrollHeight;this.signature=signature;
   const oldNodes=new Map(Array.from(this.content.children).map(node=>[node.dataset.id,node]));
   const nodes=items.map(item=>{
    const encoded=JSON.stringify(item);let node=oldNodes.get(item.id);
    if(node?.dataset.signature===encoded)return node;
    node=document.createElement('div');node.dataset.id=item.id;node.dataset.signature=encoded;
    node.className=this.conversation?'conversation-row '+item.role:'message'+(item.type==='gift'?' gift':'');
    const head=document.createElement('div');head.className='message-head';
    const name=document.createElement('b');name.textContent=item.role==='assistant'?'肥波':item.type==='video'?'画面观察':item.name;head.append(name);
    if(!this.conversation){const time=document.createElement('time');time.textContent=new Date(item.time*1000).toLocaleTimeString('zh-CN',{hour12:false});head.append(time);}
    const content=document.createElement('div');content.className='message-content';content.textContent=item.content;
    node.append(head,content);return node;
   });
   if(!nodes.length){const empty=document.createElement('div');empty.className='empty-feed';empty.textContent=this.conversation?'还没有对话，等你来聊聊':'弹幕和礼物会出现在这里';nodes.push(empty);}
   this.content.replaceChildren(...nodes);
   if(this.track){
    this.measure();this.offset=Math.max(0,this.cycleWidth-this.root.clientWidth);this.root.scrollLeft=this.offset;this.lastWritten=this.root.scrollLeft;
   }else this.root.scrollTop=follow?this.root.scrollHeight:Math.max(0,top+Math.min(0,this.root.scrollHeight-oldHeight));
  }
 }
 class PresentationView{
  constructor(conversation,incoming,bubble){this.conversation=new FeedView(conversation,true);this.incoming=new FeedView(incoming,false);this.bubble=bubble;this.epoch=null;this.revision=-1;}
  update(state){
   if(this.epoch===state.epoch&&state.revision<=this.revision)return;
   this.epoch=state.epoch;this.revision=state.revision;
   this.conversation.update(state.conversation||[]);this.incoming.update(state.incoming||[]);
   this.bubble.textContent=cleanReply(state.current?.text)||'本鲸开工啦，陪你聊会儿～';
   this.bubble.dataset.captionEmpty=state.current?.text?'false':'true';
  }
 }
 window.PresentationView=PresentationView;
})();
