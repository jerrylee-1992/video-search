const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const root=path.join(__dirname,'..','src','video_search','web');
const html=fs.readFileSync(path.join(root,'index.html'),'utf8');
const inline=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(match=>match[1]).join('\n');
const gate=fs.readFileSync(path.join(root,'request-gate.js'),'utf8');
function harness(url){
  const nodes=new Map(),requests=[];
  function element(){return {textContent:'',innerHTML:'',value:'',dataset:{},children:[],appendChild(child){this.children.push(child)},append(){},replaceChildren(){this.children=[]},addEventListener(){},querySelector(){return element()},pause(){},close(){},showModal(){this.open=true},play(){return Promise.resolve()}}}
  const location=new URL(url);
  const context=vm.createContext({URLSearchParams,URL,console,encodeURIComponent,location,
    history:{replaceState(_a,_b,url){location.href=new URL(url,location).href}},
    document:{querySelector(selector){if(!nodes.has(selector))nodes.set(selector,element());return nodes.get(selector)},querySelectorAll(){return []},createElement:element},
    fetch(url){if(url==='/api/status')return Promise.resolve({ok:true,json:async()=>({videos:{total:2},shots:28,ready_shots:27})});return new Promise((resolve,reject)=>requests.push({url,resolve,reject}))}
  });
  vm.runInContext(gate,context);vm.runInContext(inline,context);
  return {context,nodes,requests,location};
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const complete=(request,query,path)=>request.resolve({ok:true,json:async()=>({query,path,count:0,results:[]})});
(async()=>{
  const h=harness('http://127.0.0.1:8766/search/session/'+'a'.repeat(32)+'?shot=28');
  await tick();assert.equal(h.nodes.get('#status').textContent,'2 个视频 · 27/28 个镜头可搜索');
  const session=h.requests[0];h.context.search('最新查询');complete(h.requests[1],'最新查询');await tick();complete(session,'旧会话');await tick();
  assert.equal(h.nodes.get('#query').value,'最新查询');assert.equal(h.location.pathname,'/');assert.equal(h.location.searchParams.get('q'),'最新查询');assert.equal(h.location.searchParams.has('shot'),false);
  h.context.search('慢A');h.context.search('快B');complete(h.requests[3],'快B');await tick();complete(h.requests[2],'慢A');await tick();assert.equal(h.nodes.get('#query').value,'快B');
  h.context.search('旧失败');h.context.search('新成功');complete(h.requests[5],'新成功');await tick();h.requests[4].reject(new Error('offline'));await tick();assert.equal(h.nodes.get('#query').value,'新成功');assert.ok(!h.nodes.get('#meta').textContent.includes('失败'));
  h.context.search('当前失败');h.requests[6].resolve({ok:false});await tick();assert.ok(h.nodes.get('#meta').textContent.includes('失败'));
  h.context.search('已恢复');complete(h.requests[7],'已恢复');await tick();assert.equal(h.nodes.get('#query').value,'已恢复');
  const refreshed=harness(h.location.href);await tick();assert.equal(refreshed.requests[0].url,'/api/search?q='+encodeURIComponent('已恢复'));
  h.context.search('限定范围','/media/project A');assert.equal(h.requests[8].url,'/api/search?q='+encodeURIComponent('限定范围')+'&path='+encodeURIComponent('/media/project A'));complete(h.requests[8],'限定范围','/media/project A');await tick();assert.equal(h.nodes.get('#path-filter').value,'/media/project A');
  const scoped=harness(h.location.href);await tick();assert.equal(scoped.requests[0].url,'/api/search?q='+encodeURIComponent('限定范围')+'&path='+encodeURIComponent('/media/project A'));
})().catch(error=>{console.error(error);process.exitCode=1});
