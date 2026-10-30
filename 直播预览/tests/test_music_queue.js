// Real DOM fixture for the published rolling board; no music or model requests.
const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const {chromium}=require(path.resolve(__dirname,'../../参考项目/dyhub/node_modules/playwright-core'));
test('Rolling board shows all waiting songs and preserves rows across polling',async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try{
  const page=await browser.newPage();
  await page.route('**/*',route=>route.abort());
  await page.setContent('<h2 id="heading"></h2><div id="root"></div>');
  await page.addScriptTag({content:fs.readFileSync('music_queue.js','utf8')});
  await page.evaluate(()=>{
   window.view=new MusicQueueView(document.getElementById('root'),document.getElementById('heading'));
   window.payload={queue:Array.from({length:12},(_,i)=>({id:String(i),track:{id:String(i),title:'歌曲'+i,artist:'歌手'+i},nickname:'观众'+i})),
    player:{current:{title:'正在放的歌',artist:'当前歌手'},next:{id:'outside',title:'网易云实际下一首',artist:'原歌单歌手'}},limits:{max_pending:12,queue_auto_scroll:false}};
   view.update(payload);
  });
  assert.equal(await page.locator('.music-roll-cycle .music-song').count(),12);
  assert.equal(await page.locator('#heading').innerText(),'12 首');
  assert.equal(await page.locator('.music-song-title').last().innerText(),'歌曲11');
  assert.equal(await page.locator('.music-song-rank').last().innerText(),'12');
  assert.ok((await page.locator('.music-next').innerText()).includes('网易云实际下一首'));
  assert.equal(await page.evaluate(()=>{const original=view.cycle.firstElementChild;view.update(payload);return original===view.cycle.firstElementChild;}),true);
  await page.evaluate(()=>view.update({...payload,queue:[]}));
  assert.equal(await page.locator('#heading').innerText(),'0 首');
  assert.ok((await page.locator('.music-empty').innerText()).includes('下一首好歌'));
 }finally{await browser.close();}
});
