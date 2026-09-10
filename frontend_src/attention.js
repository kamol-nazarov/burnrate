/* In-app attention only. The backend owns eligibility, counts, thresholds and clocks. */
(() => {
  function startAttention(actions = {}) {
    const node = (tag, text, cls) => { const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n; };
    const button = (text, fn) => {const n=node('button',text,'ghost-button');n.type='button';n.addEventListener('click',fn);return n;};
    const el=id=>document.getElementById(id),nav=el('nav-attention'),panel=el('attention-panel'),title=el('attention-title');
    nav.addEventListener('click',event=>open(event.currentTarget));el('attention-close').addEventListener('click',()=>panel.close());
    const current=el('attention-current'),history=el('attention-history'),status=el('attention-status'),message=el('attention-message'),list=el('attention-list');
    current.addEventListener('click',()=>select('current'));history.addEventListener('click',()=>select('history'));
    const form=el('attention-preferences');
    const inputs={};
    for(const key of ['quota','source','pricing','lower','higher']) {
      const input=el('attention-'+key);input.addEventListener('input',()=>{dirty=true;});inputs[key]=input;
    }
    form.addEventListener('submit',e=>{e.preventDefault();write('preferences');});
    let data=null,tab='current',epoch=0,controller=null,last=0,opener=null,busy=false,dirty=false,retry=null;
    const date=value=>value?new Intl.DateTimeFormat('en-US',{dateStyle:'medium',timeStyle:'short',timeZone:data?.timezone||'UTC'}).format(new Date(value)):'Unavailable';
    function restore() {
      const active=[...document.querySelectorAll('dialog[open]')].at(-1),owner=opener?.closest?.('dialog');
      if(opener?.isConnected&&!opener.closest('[hidden]')&&(!owner||owner.open)&&(!active||active===owner))opener.focus();
      else (active?.querySelector('[tabindex="-1"]')||active||el('home-button')).focus();
    }
    panel.addEventListener('close',()=>{if(panel.open)return;++epoch;controller?.abort();busy=false;restore();});
    panel.addEventListener('cancel',()=>{++epoch;controller?.abort();});
    function open(target=document.activeElement){opener=target;if(!panel.open)panel.showModal();title.focus();refresh(true);}
    function select(next){tab=next;++epoch;controller?.abort();paint();refresh(true);}
    async function request(body,signal,detail) {
      const response=await fetch('/api/attention'+(!body&&!detail?'?detail=false':''),{cache:'no-store',signal,
        ...(body?{method:'POST',headers:{'Content-Type':'application/json','X-BURNRATE-Request':'1'},body:JSON.stringify(body)}:{})});
      const payload=await response.json();
      if(!response.ok){const error=new Error(payload.error||'Attention unavailable');error.status=response.status;throw error;}
      return payload;
    }
    function accept(next){if(!data||next.revision>=data.revision)data={...data,...next};nav.textContent=data.badgeLabel;nav.setAttribute('aria-label',`Attention: ${data.badgeCount} items; ${data.evaluation.status}`);}
    async function refresh(force=false) {
      if(document.hidden||busy||!force&&Date.now()-last<30000)return;
      last=Date.now();const owned=++epoch;controller?.abort();controller=new AbortController();
      if(panel.open&&!data)status.textContent='Loading attention…';
      try{const next=await request(null,controller.signal,panel.open);if(owned!==epoch)return;accept(next);if(panel.open)paint();}
      catch(error){if(owned!==epoch||error.name==='AbortError')return;nav.textContent='Attention ?';if(panel.open){status.textContent='Attention unavailable';message.replaceChildren(node('span',error.message),button('Retry',()=>refresh(true)));}}
    }
    async function write(operation,item,hours) {
      if(busy||!data)return;
      const values=operation==='preferences'?{preferences:Object.fromEntries(Object.entries(inputs).map(([k,n])=>[k,n.type==='checkbox'?n.checked:Number(n.value)]))}:{id:item.id,...(hours?{hours}:{})};
      const base={operation,expectedRevision:data.revision,...values},key=JSON.stringify({operation,...values});
      if(retry?.key!==key)retry={key,body:{...base,requestId:crypto.randomUUID()}};
      busy=true;const owned=++epoch;controller?.abort();controller=new AbortController();message.textContent='Saving…';
      try{const next=await request(retry.body,controller.signal);if(owned!==epoch||!panel.open)return;accept(next);retry=null;dirty=false;message.textContent='Saved';paint();}
      catch(error){if(owned!==epoch||error.name==='AbortError')return;message.textContent=error.message+' Changes kept.';
        if(error.status===409){retry=null;busy=false;await refresh(true);}}
      finally{if(owned===epoch)busy=false;}
    }
    function paint() {
      if(!data?.current)return;
      const evaluation=data.evaluation;
      status.textContent=`${evaluation.label} ${date(evaluation.evaluatedAt)}`;
      current.setAttribute('aria-pressed',String(tab==='current'));history.setAttribute('aria-pressed',String(tab==='history'));
      const active=document.activeElement,focus=active?.getAttribute?.('data-attention');
      const rows=data[tab]||[];const children=[];
      for(const item of rows){
        const card=node('article',null,'plans-value-row');
        card.append(node('h3',item.title),node('p',item.reason),node('p',item.stateLabel),node('p',item.summary));
        for(const time of item.timestamps)card.append(node('p',`${time.label}: ${date(time.at)}`));
        const controls=node('div',null,'plans-value-row-actions');
        const add=(label,fn)=>{const b=button(label,fn);b.setAttribute('data-attention',item.id+label);controls.append(b);};
        for(const control of item.controls||[])if(['acknowledge','snooze','unsnooze'].includes(control.operation))add(control.label,()=>write(control.operation,item,control.hours));
        if(Object.hasOwn(actions,item.actionType))add(item.actionLabel,event=>{if(item.actionType!=='connections')panel.close();actions[item.actionType](item,event.currentTarget);});
        card.append(controls);children.push(card);
      }
      if(!rows.length)children.push(node('p',tab==='history'?'No closed episodes.':evaluation.status==='ok'?'No incidents; unavailable sources are not assessed.':'Evidence unavailable.'));
      list.replaceChildren(...children);
      if(focus){const target=[...list.querySelectorAll('button')].find(n=>n.getAttribute('data-attention')===focus);(target||title).focus();}
      if(!dirty)for(const [key,input] of Object.entries(inputs)){if(input.type==='checkbox')input.checked=data.preferences[key];else input.value=data.preferences[key];}
    }
    refresh(true);
    return {open,refresh};
  }
  if(typeof module!=='undefined')module.exports={startAttention};
  if(typeof window!=='undefined')window.startAttention=startAttention;
})();
