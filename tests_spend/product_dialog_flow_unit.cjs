// Production product.js with fake DOM/fetch boundaries. No browser or server.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(id='') { this.id=id; this.value=''; this.hidden=false; this.open=false; this.disabled=false; this.checked=false; this.dataset={}; this.style={}; this.options=[]; this.handlers={}; this.attrs={}; this.nodes=new Map(); this.items=new Map(); this.classList={toggle(){}}; this.textContent=''; this.isConnected=true; }
  addEventListener(name,fn) { (this.handlers[name] ||= []).push(fn); }
  dispatch(name) { for(const fn of this.handlers[name] || []) fn({currentTarget:this,target:this,preventDefault(){}}); }
  click() { this.onclick?.({currentTarget:this,target:this}); this.dispatch('click'); }
  focus() { document.activeElement=this; }
  showModal() { this.open=true; }
  close() { this.open=false; if(!this.deferClose)this.dispatch('close'); }
  reportValidity() { return !this.required || !!this.value; }
  setAttribute(name,value) { this.attrs[name]=value; }
  getAttribute(name) { return this.attrs[name] ?? null; }
  removeAttribute(name) { delete this.attrs[name]; }
  querySelector(selector) { if(!this.nodes.has(selector)) { const node=new Element(selector); if(selector==='.integration-hint')node.hidden=true; this.nodes.set(selector,node); } return this.nodes.get(selector); }
  querySelectorAll(selector) {
    if(selector==='li') return [0,1,2].map(n=>this.querySelector('li'+n));
    if(selector==='[data-cadence]') return ['monthly','quarterly','annual'].map(value=>{const node=this.querySelector('cadence'+value);node.dataset.cadence=value;return node;});
    if(selector==='[data-plan-op]') return ['schedule','end','correct'].map(value=>{const node=this.querySelector('op'+value);node.dataset.planOp=value;return node;});
    if(selector==='button' && this.id==='plan-tool-cards')return [...this.items.values()];
    if(selector==='button'){const found=[];const visit=n=>{if(n?.tagName==='BUTTON')found.push(n);for(const c of n?.children||[])visit(c);};for(const c of this.children||[])visit(c);return found;}
    return [];
  }
  replaceChildren(...children) { const detach=node=>{if(typeof node==='object'){node.isConnected=false;for(const child of node.children || [])detach(child);}};for(const child of this.children || [])detach(child);this.children=[];this.append(...children);this.options=children; if(!children.some(child=>child.value===this.value))this.value=children[0]?.value || ''; }
  append(...children) { this.children ||= []; for(const child of children){if(typeof child==='object')child.parent=this;this.children.push(child);} }
  before() {}
  closest(selector) { for(let node=this;node;node=node.parent){if(selector==='dialog' && ['plan-manager','harness-manager','attention-panel'].includes(node.id))return node;if(selector==='[hidden]' && node.hidden)return node;}return null; }
}
const nodes=new Map();
const html=fs.readFileSync(require.resolve('../spend_web/index.html'),'utf8');
const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(match=>match[1]);
assert.equal(ids.length,new Set(ids).size,'markup IDs must be unique');
assert.doesNotMatch(html,/<nav class="product-actions"/);
assert.ok(!ids.includes('reload-plans'));
assert.doesNotMatch(html,/<div id="subs-details"[^>]*hidden/);
const apiSource=fs.readFileSync(require.resolve('../spend_app/api.py'),'utf8');
for(const [asset,old] of [['spend.css','42'],['product.js','1']]){
  const marker=html.match(new RegExp('/'+asset.replace('.','\\.')+'\\?v=(\\d+)'))[1];
  assert.notEqual(marker,old);
  assert.ok(apiSource.includes(`("${asset}", "${marker}")`),'HTML and content-hash marker must agree');
}
const newStyles=fs.readFileSync(require.resolve('../frontend_src/spend.css'),'utf8').split('/* Source and subscription entry points:')[1];
const palette=new Set(['--surface','--soft','--raised','--border','--border-strong','--hair','--text','--secondary','--muted','--dim','--accent','--green','--amber','--danger','--warn-bg','--warn-border','--good-bg','--good-border','--font-mono','--font-sans','--danger-bg','--danger-border','--dialog-shadow']);
for(const match of newStyles.matchAll(/var\((--[\w-]+)/g))assert.ok(palette.has(match[1]),`Undeclared palette use: ${match[1]}`);
assert.doesNotMatch(newStyles,/#[\da-f]{3,8}\b|rgba?\(/i);
const document={body:new Element(),createTextNode:text=>text,activeElement:null,getElementById(id){assert.ok(ids.includes(id),`Missing markup for ${id}`);if(!nodes.has(id))nodes.set(id,new Element(id));return nodes.get(id);},createElement(tag='div'){const n=new Element();n.tagName=tag.toUpperCase();return n;}};
const el=id=>document.getElementById(id);
document.querySelectorAll=selector=>selector==='dialog[open]' ? [el('plan-manager'),el('harness-manager'),el('attention-panel')].filter(node=>node.open) : [];
el('plan-manager').nodes.set("[tabindex='-1']",el('plan-manager-title'));
el('harness-manager').nodes.set("[tabindex='-1']",el('harness-manager-title'));
for(const id of ['plans-value-view','plan-manager-title','tab-plans-value','tab-plans-list','plan-form'])el(id).parent=el('plan-manager');
for(const id of ids.filter(id=>id.startsWith('attention-')&&id!=='attention-panel'))el(id).parent=el('attention-panel');
for(const selector of ["[tabindex='-1']",'[tabindex="-1"]'])el('attention-panel').nodes.set(selector,el('attention-title'));
for(const key of ['quota','source','pricing','lower','higher'])el('attention-'+key).type=['lower','higher'].includes(key)?'number':'checkbox';
const findButton=(root,label)=>{if(typeof root!=='object')return null;if(root.attrs['aria-label']===label)return root;for(const child of root.children || []){const found=findButton(child,label);if(found)return found;}return null;};
const requests=[];
const window={colorFor:()=> 'var(--accent)',refreshCapacitySourceHints(){},openHarnessUsage:tool=>{window.viewed=tool;},BurnrateConnections:{open(){window.managedOpened=true;}},open(){}};
let sequence=0;
const context={window,document,console,Date,Intl,BigInt,AbortController,Option:function(label,value){this.text=label;this.value=String(value);},crypto:{randomUUID:()=>`00000000-0000-4000-8000-${String(++sequence).padStart(12,"0")}`},localStorage:{setItem(){},getItem(){return null;}},navigator:{clipboard:{writeText:async()=>{}}},
  fetch:(url,options={})=>new Promise(resolve=>requests.push({url,options,resolve})),
  nodeFrom:()=>new Element(),setText:(node,text)=>{node.textContent=text;},setEmpty:(node,html)=>{node.items.clear();node.textContent=html;},
  reconcileChildren:(root,rows,key,create,update)=>{const next=new Map();rows.forEach((row,index)=>{const id=key(row,index);const node=root.items.get(id)||create();update(node,row,index);next.set(id,node);});root.items=next;}};
vm.createContext(context);
window.BurnrateDOM={n:context.nodeFrom,t:context.setText,e:context.setEmpty,r:context.reconcileChildren};
for(const file of ['dialogs.js','product.js'])vm.runInContext(fs.readFileSync(require.resolve('../spend_web/'+file),'utf8'),context);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const reply=(request,data,ok=true,status=200)=>request.resolve({ok,status,json:async()=>data});
const latest=(url,method='GET')=>requests.filter(r=>r.url===url && (r.options.method||'GET')===method).at(-1);
const planData=amount=>({today:'2026-09-07',timezone:'UTC',tools:['codex','opencode'],plans:[{id:1,version:4,end_date:null,terms:[{id:11,name:'Coding plan',tool_key:'codex',amount_usd:amount,monthly_equivalent:amount,cadence:'monthly',start_date:'2026-09-01',end_date:null,status:'active'}]}]});
(async()=>{
  reply(latest('/api/onboarding'),{timezone:'UTC',timezoneSetting:'SPEND_TIMEZONE',sources:[],integrations:[]});await flush();
  el('nav-plans').click();reply(latest('/api/subscriptions'),planData('20'));await flush();
  assert.equal(el('plan-manager').open,true);
  assert.equal(el('plans-value-view').hidden,false);
  assert.equal(el('plan-list-view').hidden,true);
  const valueRequest=latest('/api/subscriptions/value?period=this_month');
  assert.ok(valueRequest,'opening the dialog must request comparison data');
  const report=JSON.parse(fs.readFileSync(require.resolve('./fixtures/plans_value_response.json'),'utf8'));
  reply(valueRequest,report);await flush();
  const rowConnection=findButton(el('plans-value-view'),'Check connection for Coding plan');
  assert.ok(rowConnection,'real comparison row rendered');
  rowConnection.focus();rowConnection.click();
  assert.equal(el('harness-manager').open,true);
  assert.equal(el('plan-manager').open,true,'child help keeps the parent open');
  el('close-harnesses').click();
  assert.equal(document.activeElement,rowConnection,'row help returns focus to the actual opener');
  el('plan-name').value='Draft stays intact';
  const footerConnection=findButton(el('plans-value-view'),'Open connection diagnostics');
  footerConnection.focus();footerConnection.dispatch('click'); // Native keyboard button activation produces click.
  el('harness-manager').dispatch('cancel');el('harness-manager').close();
  assert.equal(document.activeElement,footerConnection,'keyboard footer activation and Escape restore focus');
  assert.equal(el('plan-name').value,'Draft stays intact');
  rowConnection.focus();rowConnection.click();rowConnection.isConnected=false;
  el('close-harnesses').click();
  assert.equal(document.activeElement,el('plan-manager-title'),'disconnected row returns to the parent heading');
  rowConnection.isConnected=true;rowConnection.focus();rowConnection.click();rowConnection.parent.hidden=true;
  el('close-harnesses').click();
  assert.equal(document.activeElement,el('plan-manager-title'),'hidden row returns to the parent heading');
  rowConnection.parent.hidden=false;rowConnection.focus();rowConnection.dispatch('click');
  el('plan-manager').close();el('nav-plans').click();
  el('close-harnesses').click();
  assert.equal(el('plan-manager').open,true,'reopened parent remains open');
  assert.equal(document.activeElement,el('plan-manager-title'),'stale opener cannot take focus after parent reopen');
  assert.ok(requests.every(r=>['/api/onboarding','/api/subscriptions','/api/subscriptions/value?period=this_month','/api/attention?detail=false'].includes(r.url) && (!r.options.method||r.options.method==='GET')),'connection help only reads existing metadata');
  el('tab-plans-list').click();
  assert.equal(el('plans-value-view').hidden,true);
  assert.equal(el('plan-list-view').hidden,false);
  assert.equal(valueRequest.options.signal.aborted,true,'leaving comparisons cancels its request');
  assert.equal(el('plan-monthly-total').textContent,'$20.00');
  el('new-plan').click();
  assert.equal(el('plan-step-1').hidden,false);
  el('plan-name').value='Another plan';el('plan-tool').value='codex';el('plan-form').dispatch('submit');
  assert.equal(el('plan-step-2').hidden,false);
  el('plan-amount').value='29';el('plan-form').dispatch('submit');
  assert.equal(el('plan-step-3').hidden,false);
  el('plan-form').dispatch('submit');
  const first=latest('/api/subscriptions','POST'), firstBody=JSON.parse(first.options.body);
  assert.equal(firstBody.operation,'add');assert.equal(firstBody.values.amount_usd,'29');
  reply(first,{error:'Temporary failure'},false,503);await flush();
  assert.equal(el('plan-amount').value,'29');assert.match(el('plan-message').textContent,/input is preserved/);
  el('plan-form').dispatch('submit');const retry=latest('/api/subscriptions','POST');
  assert.equal(JSON.parse(retry.options.body).request_id,firstBody.request_id);
  reply(retry,{saved:true});await flush();const oldReload=latest('/api/subscriptions');
  el('plan-manager').deferClose=true;el('close-plans').click();
  el('manage-plans').click();const newReload=latest('/api/subscriptions');
  el('plan-manager').dispatch('close');el('plan-manager').deferClose=false;
  assert.equal(newReload.options.signal.aborted,false,'queued old close must not cancel reopened load');
  reply(newReload,planData('30'));await flush();reply(oldReload,planData('99'));await flush();
  assert.equal(el('plan-monthly-total').textContent,'$30.00');assert.doesNotMatch(el('plan-message').textContent,/Saved durably/);
  const row=el('plan-history').items.get(1),actions=row.querySelector('.plan-row-actions');
  actions.querySelectorAll('[data-plan-op]')[2].click();assert.equal(el('plan-step-2').hidden,false);
  el('plan-form').dispatch('submit');el('plan-form').dispatch('submit');const preview=latest('/api/subscriptions','POST');
  assert.equal(JSON.parse(preview.options.body).preview,true);assert.equal(JSON.parse(preview.options.body).term_id,11);
  reply(preview,{from:'2026-09-01',through:'2026-09-07',before_usd:'7',after_usd:'8',preview_token:'approved',note:'Bounded preview'});await flush();
  assert.equal(el('plan-preview').hidden,false);assert.match(el('plan-affected').textContent,/7 days affected/);
  el('plan-confirm').checked=true;el('plan-form').dispatch('submit');const confirmed=latest('/api/subscriptions','POST');
  assert.equal(JSON.parse(confirmed.options.body).confirmed,true);assert.equal(JSON.parse(confirmed.options.body).preview_token,'approved');
  reply(confirmed,{error:'Version changed'},false,409);await flush();assert.match(el('plan-message').textContent,/Conflict:/);
  el('plan-back').click();el('plan-back').click();
  actions.querySelectorAll('[data-plan-op]')[1].click();
  assert.equal(el('plan-step-3').hidden,false);assert.equal(el('plan-step-2').hidden,true);assert.equal(el('plan-start-label').hidden,true);
  el('plan-end').value='2026-09-30';el('plan-form').dispatch('submit');const ending=latest('/api/subscriptions','POST');
  assert.deepEqual(JSON.parse(ending.options.body),{operation:'end',plan_id:1,expected_version:4,end_date:'2026-09-30',request_id:JSON.parse(ending.options.body).request_id});
  reply(ending,{error:'Temporary failure'},false,503);await flush();el('plan-back').click();
  actions.querySelectorAll('[data-plan-op]')[0].click();assert.equal(el('plan-step-2').hidden,false);assert.equal(el('plan-start').value,'2026-09-07');
  el('plan-form').dispatch('submit');assert.equal(el('plan-end-label').hidden,true);el('plan-form').dispatch('submit');const scheduled=latest('/api/subscriptions','POST');
  assert.equal(JSON.parse(scheduled.options.body).operation,'schedule');assert.deepEqual(JSON.parse(scheduled.options.body).values,{amount_usd:'30',cadence:'monthly'});
  reply(scheduled,{error:'Temporary failure'},false,503);await flush();
  el('close-plans').click();assert.equal(document.activeElement,el('manage-plans'));
  el('nav-connect').click();const oldScan=latest('/api/onboarding');el('rescan-harnesses').click();const newScan=latest('/api/onboarding');
  const source={source:'codex_local',state:'healthy',path:'~/.codex/sessions',freshnessSeconds:60,reason:null,nextAction:'No action',experimental:false,documentation:'https://example.test/docs',measurements:[],pricingMissing:[]};
  reply(newScan,{sources:[source],integrations:[{setting:'OPENAI_ADMIN_KEY',configured:true,host:'api.openai.com',experimental:false}],documentation:source.documentation});await flush();
  reply(oldScan,{sources:[],integrations:[]});await flush();assert.equal(el('nav-connect').querySelector('span').textContent,'1 harnesses');
  const integration=el('integration-rows').items.get('OPENAI_ADMIN_KEY'), requestCount=requests.length;
  integration.querySelector('.config-indicator').click();assert.equal(integration.querySelector('.integration-hint').hidden,false);assert.equal(requests.length,requestCount);
  el('close-harnesses').click();assert.equal(document.activeElement,el('nav-connect'));
  el('capacity-connect').click();reply(latest('/api/onboarding'),{},false,503);await flush();assert.equal(el('harness-content').hidden,true);assert.match(el('harness-message').textContent,/Setup metadata is unavailable/);
  window.renderSourceGuidance({sources:[source]});assert.equal(el('harness-content').hidden,true,'health polling must not dismiss an onboarding failure');
  el('close-harnesses').click();assert.equal(document.activeElement,el('capacity-connect'));
  el('nav-connect').click();reply(latest('/api/onboarding'),{sources:[],integrations:[]});await flush();assert.match(el('harness-rows').textContent,/No local source metadata/);
  el('nav-connect').isConnected=false;el('close-harnesses').click();assert.equal(document.activeElement,el('nav-connect'));el('nav-connect').isConnected=true;
  const attention=JSON.parse(fs.readFileSync(require.resolve('./fixtures/attention_response.json'),'utf8'));
  reply(latest('/api/attention?detail=false'),attention);await flush();
  el('nav-attention').click();const obsolete=latest('/api/attention');
  el('attention-close').click();el('nav-attention').click();const reopened=latest('/api/attention');
  reply(obsolete,{...attention,revision:999,badgeCount:99});await flush();
  reply(reopened,attention);await flush();
  assert.equal(el('nav-attention').textContent,'Attention 1','obsolete response cannot replace badge');
  const text=n=>[n.textContent||'',...(n.children||[]).map(c=>typeof c==='string'?c:text(c))].join(' ');
  const control=label=>el('attention-list').querySelectorAll('button').find(n=>n.textContent===label);
  assert.match(text(el('attention-list')),/Acknowledged/);assert.match(text(el('attention-list')),/Snooze until/);
  const help=control('View connection status');assert.ok(help);help.focus();help.dispatch('click');
  assert.equal(el('harness-manager').open,true);assert.equal(el('attention-panel').open,true);
  el('close-harnesses').click();assert.equal(document.activeElement,help,'real attention opener returns from real connection handler');
  help.focus();help.click();help.isConnected=false;el('close-harnesses').click();
  assert.equal(document.activeElement,el('attention-title'),'disconnected attention opener stays in the active panel');
  const ack=control('Acknowledge');ack.click();const firstAck=latest('/api/attention','POST');
  reply(firstAck,{error:'Temporary write failure'},false,503);await flush();
  assert.equal(el('nav-attention').textContent,'Attention 1');assert.match(el('attention-message').textContent,/Changes kept/);
  ack.click();const secondAck=latest('/api/attention','POST');assert.equal(secondAck.options.body,firstAck.options.body,'uncertain write keeps its retry identity');
  const updated={...attention,revision:attention.revision+1,badgeCount:0,badgeLabel:'Attention 0',current:attention.current.map(item=>item.family==='source'?{...item,acknowledged:true,acknowledgedAt:attention.asOf,needsAttention:false,controls:item.controls.filter(c=>c.operation!=='acknowledge')}:item)};
  reply(secondAck,updated);await flush();assert.equal(el('nav-attention').textContent,'Attention 0');
  el('attention-lower').value='85';el('attention-lower').dispatch('input');el('attention-history').click();
  reply(latest('/api/attention'),updated);await flush();
  assert.match(text(el('attention-list')),/later usable import/);assert.equal(el('attention-lower').value,'85','tab refresh preserves preference draft');
  el('attention-preferences').dispatch('submit');const pref=latest('/api/attention','POST');
  assert.equal(JSON.parse(pref.options.body).preferences.lower,85);reply(pref,{error:'Invalid thresholds'},false,422);await flush();
  assert.equal(el('attention-lower').value,'85');assert.match(el('attention-message').textContent,/Changes kept/);
  document.hidden=true;const count=requests.length;window.BurnrateAttention.refresh(true);assert.equal(requests.length,count);document.hidden=false;
  el('attention-current').click();const late=latest('/api/attention');el('attention-close').click();
  reply(late,{...updated,revision:1000,badgeCount:100});await flush();assert.equal(el('nav-attention').textContent,'Attention 0');
  assert.equal(document.activeElement,el('nav-attention'));
  el('nav-attention').click();const hostile=structuredClone(updated);
  hostile.revision+=1;hostile.current[0].title='<img src=x onerror=evil()>';hostile.current[0].actionType='constructor';
  reply(latest('/api/attention'),hostile);await flush();
  assert.ok(text(el('attention-list')).includes('<img src=x onerror=evil()>'),'hostile text stays literal');
  assert.ok(!el('attention-list').querySelectorAll('button').some(n=>n.textContent===hostile.current[0].actionLabel),'unknown action type cannot invoke a prototype method');
  el('nav-attention').hidden=true;el('attention-close').click();assert.equal(document.activeElement,el('home-button'));el('nav-attention').hidden=false;
  el('nav-attention').click();reply(latest('/api/attention'),{...updated,revision:updated.revision+2,current:[],history:[],badgeCount:0,evaluation:{...updated.evaluation,status:'error',label:'Evaluation error. Last success:'}});await flush();
  assert.match(el('attention-status').textContent,/Evaluation error/);assert.match(text(el('attention-list')),/Evidence unavailable/);
  el('attention-history').click();reply(latest('/api/attention'),{...updated,revision:updated.revision+3,current:[],history:[],badgeCount:0});await flush();
  assert.match(text(el('attention-list')),/No closed episodes/);
  el('attention-close').click();el('nav-attention').click();reply(latest('/api/attention'),{error:'Unavailable'},false,503);await flush();
  assert.equal(el('nav-attention').textContent,'Attention ?');assert.match(el('attention-status').textContent,/Attention unavailable/);
  el('attention-close').click();
  console.log('Product and Attention real-handler fake-DOM flow tests passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
