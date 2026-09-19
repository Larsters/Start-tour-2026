const fs=require('fs');const {JSDOM,VirtualConsole}=require('jsdom');
const html=fs.readFileSync(__dirname+'/../../web/index.html','utf8');
const fx=JSON.parse(fs.readFileSync(__dirname+"/fixture.json",'utf8'));
const errors=[];const vc=new VirtualConsole();vc.on('jsdomError',e=>errors.push('jsdomError: '+e.message));vc.on('error',(...a)=>errors.push('console.error: '+a.map(String).join(' ')));
const dom=new JSDOM(html,{runScripts:'outside-only',pretendToBeVisual:true,url:'http://localhost:8080/app',virtualConsole:vc});
const w=dom.window;
// clamp timers so animations finish fast
const origST=w.setTimeout;w.setTimeout=(fn,ms,...a)=>origST(fn,Math.min(ms||0,4),...a);
let served=false;
w.fetch=async(url,opts)=>{const u=String(url);const json=x=>({ok:true,status:200,json:async()=>x});
  if(u.includes('/health'))return json(fx.health);if(u.includes('/customers'))return json(fx.customers);
  if(u.includes('/cards'))return json(fx.cards);
  if(u.includes('/events')){if(served)return json({events:[],latest:99});served=true;return json(fx.events)}
  if(u.includes('/budget'))return {ok:false,status:404,json:async()=>({})};
  return json({})};
// run the page's script now
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
try{w.eval(script)}catch(e){errors.push('eval: '+e.message)}
const $=s=>w.document.querySelector(s),$$=s=>[...w.document.querySelectorAll(s)];
const sleep=ms=>new Promise(r=>origST(r,ms));
(async()=>{
  await sleep(1500);
  const rep=[];
  rep.push(['customer selected',$('#customer').value]);
  rep.push(['known shops row',$('#known').textContent.slice(0,80)]);
  rep.push(['contract state',$('#tab-meta').textContent]);
  rep.push(['history count badge',$('#core-hist').textContent, 'shown='+$('#core-hist').classList.contains('show')]);
  rep.push(['history items',$$('.hist-item').length, $$('.hist-item .badge').map(b=>b.textContent).join(',')]);
  rep.push(['wheel nodes after last decision',$$('.criterion').length, $$('.criterion').map(n=>n.textContent.trim()+':'+[...n.classList].filter(c=>['pass','warn','fail'].includes(c))).join(' | ')]);
  rep.push(['core',$('#core-title').textContent,'/',$('#core-result').textContent]);
  rep.push(['feedback chips',$$('.feedback-chip').length]);
  rep.push(['cards in chat',$$('.card').map(c=>c.className+(c.querySelector('.actions')?'[actions:'+$$('.actions button',c).length+']':'')).join(' ; ')]);
  // open history, replay the approve entry (oldest decision)
  $('#core').click();rep.push(['history open',$('#history').classList.contains('open')]);
  const approve=$$('.hist-item').find(h=>h.querySelector('.badge').textContent==='approve');approve.click();
  await sleep(1500);
  rep.push(['after replay: verdict',$('#verdict').textContent.slice(0,40),'| core',$('#core-title').textContent,'| nodes',$$('.criterion').length,'| approved halo',$('#wheel').classList.contains('approved')]);
  // click a node → reasoning in core
  const node=$$('.criterion')[0];node.click();rep.push(['node click →',$('#core-title').textContent,'/',$('#core-detail').textContent.slice(0,60)]);
  // replay compile entry
  $('#core').click();$$('.hist-item').find(h=>h.querySelector('.badge').textContent==='compile').click();await sleep(300);
  rep.push(['compile replay →',$('#core-title').textContent]);
  for(const r of rep)console.log(' -',r.join(' '));
  console.log(errors.length?'\nERRORS:\n'+errors.join('\n'):'\nno JS errors');
  process.exit(errors.length?1:0);
})();
