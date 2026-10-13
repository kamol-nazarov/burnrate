/* In-app attention only. The backend owns eligibility, counts, thresholds and clocks. */
(() => {
  function startAttention(actions = {}) {
    const node = (tag, text, cls) => { const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n; };
    const button = (text, fn, cls='attention-action') => {const n=node('button',text,cls);n.type='button';n.addEventListener('click',fn);return n;};
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
    const compact=value=>{const n=Number(value);if(!Number.isFinite(n))return String(value??"");const abs=Math.abs(n);for(const [size,suffix] of [[1e12,"T"],[1e9,"B"],[1e6,"M"],[1e3,"K"]]){if(abs<size)continue;const scaled=n/size;const digits=abs>=size*100?0:abs>=size*10?1:2;return scaled.toFixed(digits).replace(/\.0+$/,"").replace(/(\.\d)0$/,"$1")+suffix;}return n.toLocaleString("en-US");};
    const familyLabel=family=>({pricing:"Pricing",quota:"Quota",source:"Source"}[family]||"Signal");
    const tone=item=>item.status==="closed"||!(item.severity>0)?"quiet":item.severity===2?"high":"warning";
    const chip=item=>item.status==="awaiting"?"Waiting":item.status==="closed"?"Closed":item.severity===2?"High":item.severity===1?"Warning":"Below";
    function toolKey(item){const prefixed=String(item?.title||"").match(/^([a-z0-9]+):/i);const fromPrefix={cursor:"cursor",claude:"claude-code",anthropic:"claude-code",codex:"codex",openai:"codex",grok:"grok",xai:"grok",supergrok:"grok",openrouter:"openrouter",zai:"zai",opencode:"zai",zcode:"zai",antigravity:"antigravity"};if(prefixed&&fromPrefix[prefixed[1].toLowerCase()])return fromPrefix[prefixed[1].toLowerCase()];const e=item?.evidence,bits=[];if(e&&typeof e==="object"){if(e.source)bits.push(e.source);if(e.provider)bits.push(e.provider);if(e.model)bits.push(e.model);if(Array.isArray(e.sources))bits.push(...e.sources);}bits.push(item?.title||"");const blob=bits.join(" ").toLowerCase();if(blob.includes("claude")||blob.includes("anthropic"))return "claude-code";if(blob.includes("cursor"))return "cursor";if(blob.includes("codex")||blob.includes("openai"))return "codex";if(blob.includes("grok")||blob.includes("xai"))return "grok";if(blob.includes("opencode")||blob.includes("zcode")||/\bzai\b/.test(blob))return "zai";if(blob.includes("antigravity"))return "antigravity";if(blob.includes("openrouter"))return "openrouter";return "";}
    function metricLine(item){const e=item.evidence,raw=item.summary||"";let line=raw;if(e&&typeof e==="object"&&item.family==="pricing"&&e.records!=null&&e.tokens!=null&&Number.isFinite(+e.records)&&Number.isFinite(+e.tokens))line=`${(+e.records).toLocaleString("en-US")} records · ${compact(e.tokens)} tokens${e.countsComplete===false?" · Partial scan":""}`;else if(e&&typeof e==="object"&&item.family==="quota"&&e.usedPercent!=null)line=[e.usedPercent+"% used",e.pool?prettyWord(e.pool):"",e.window?prettyWord(e.window):""].filter(v=>v!=null&&v!=="").join(" · ");if(!line||line===item.reason||line===item.title)return "";return line;}
    const prettyWord=word=>{const known={xhigh:"xHigh",fast:"Fast",low:"Low",high:"High",default:"Default",unknown:"Unknown",grok:"Grok",supergrok:"SuperGrok",haiku:"Haiku",sonnet:"Sonnet",opus:"Opus",mini:"Mini",pro:"Pro",flash:"Flash",gpt:"GPT",sol:"Sol",codex:"Codex",cursor:"Cursor",claude:"Claude",openai:"OpenAI",openrouter:"OpenRouter",ox:"Ox",stealth:"Stealth",alpha:"Alpha",thinking:"Thinking",medium:"Medium"};const lower=String(word).toLowerCase();if(known[lower])return known[lower];if(/\d/.test(word))return word;if(/^[a-z]+$/i.test(word)&&word.length<=14)return word.charAt(0).toUpperCase()+word.slice(1).toLowerCase();return word;};
    const foldVersions=words=>{const out=[];for(let i=0;i<words.length;i++){const next=words[i+1];if(/^\d+$/.test(words[i])&&/^\d{1,2}$/.test(next||"")){out.push(words[i]+"."+next);i++;}else out.push(words[i]);}return out;};
    function displayTitle(raw){const original=String(raw||"");const text=original.replace(/\s+pricing$/i,"").replace(/\s+quota$/i,"").trim();if(!/[:_/-]/.test(text))return /^[a-z]/.test(original)?original.charAt(0).toUpperCase()+original.slice(1):original;const prefix=text.includes(":")?text.slice(0,text.indexOf(":")).toLowerCase():"";const tail=text.includes(":")?text.slice(text.lastIndexOf(":")+1):text;let words=foldVersions(tail.split(/[-_/\s]+/).filter(Boolean));if(!words.length)return original;if(words.length>1&&words[0].toLowerCase()===prefix)words=words.slice(1);const vague=new Set(["default","fast","low","high","mini","pro","unknown"]);const pretty=words.map(prettyWord).join(" ");if(words.length===1&&vague.has(words[0].toLowerCase())&&prefix){const head=prefix.split(/[-_\s]+/).filter(Boolean).pop();if(head&&head!==words[0].toLowerCase())return prettyWord(head)+" "+pretty;}return pretty||original;}
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
    function accept(next){if(!data||next.revision>=data.revision)data={...data,...next};nav.textContent=data.badgeLabel;nav.dataset.tone=data.badgeCount>0?"warning":data.evaluation?.status==="ok"?"quiet":"danger";nav.setAttribute('aria-label',`Attention: ${data.badgeCount} items; ${data.evaluation.status}`);}
    async function refresh(force=false) {
      if(document.hidden||busy||!force&&Date.now()-last<30000)return;
      last=Date.now();const owned=++epoch;controller?.abort();controller=new AbortController();
      if(panel.open&&!data)status.textContent='Loading attention…';
      try{const next=await request(null,controller.signal,panel.open);if(owned!==epoch)return;accept(next);if(panel.open)paint();}
      catch(error){if(owned!==epoch||error.name==='AbortError')return;nav.textContent='Attention ?';nav.dataset.tone="danger";if(panel.open){status.textContent='Attention unavailable';message.replaceChildren(node('span',error.message),button('Retry',()=>refresh(true)));}}
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
      current.dataset.count=String(data.current.length);history.dataset.count=String((data.history||[]).length);
      const active=document.activeElement,focus=active?.getAttribute?.('data-attention');
      const rows=data[tab]||[];const children=[];
      for(const item of rows){
        const card=node('article',null,'attention-card is-'+tone(item));
        const top=node('div',null,'attention-card-top');
        const key=toolKey(item);
        if(key&&window.BurnrateMarks)top.append(window.BurnrateMarks.mark(key));
        top.append(node('span',familyLabel(item.family),'attention-kicker'),node('span',chip(item),'attention-chip'));
        const rawTitle=String(item.title??"");
        const shown=displayTitle(rawTitle);
        const heading=node('h3',shown,'attention-title'+(shown===rawTitle&&/[:/_]/.test(rawTitle)?" is-key":""));
        if(shown!==rawTitle)heading.title=rawTitle;
        const parts=[top,heading];
        if(shown!==rawTitle)parts.push(node('span',rawTitle,'sr-only'));
        if(item.reason)parts.push(node('p',item.reason,'attention-reason'));
        const metricText=metricLine(item);
        if(metricText){const prose=item.family==="source"||metricText.length>48&&!/·/.test(metricText);const metric=node('p',metricText,'attention-metric'+(prose?' is-prose':''));if(item.summary)metric.title=item.summary;parts.push(metric);}
        const stamps=item.timestamps||[];
        if(stamps.length){const times=node('div',null,'attention-times');for(const time of stamps){const row=node('div',null,'attention-time');row.append(node('span',time.label),node('time',date(time.at)));times.append(row);}parts.push(times);}
        if(item.stateLabel)parts.push(node('span',item.stateLabel,'sr-only'));
        const controls=node('div',null,'attention-actions');
        const add=(label,fn,cls)=>{const b=button(label,fn,cls);b.setAttribute('data-attention',item.id+label);controls.append(b);};
        for(const control of item.controls||[])if(['acknowledge','snooze','unsnooze'].includes(control.operation))add(control.label,()=>write(control.operation,item,control.hours),control.operation==='acknowledge'?'attention-action is-primary':'attention-action');
        if(Object.hasOwn(actions,item.actionType))add(item.actionLabel,event=>{if(item.actionType!=='connections')panel.close();actions[item.actionType](item,event.currentTarget);},'attention-action attention-jump');
        if(controls.childNodes?.length||controls.children?.length)parts.push(controls);
        card.append(...parts);children.push(card);
      }
      if(!rows.length)children.push(node('p',tab==='history'?'No closed episodes.':evaluation.status==='ok'?'No incidents; unavailable sources are not assessed.':'Evidence unavailable.','attention-empty'));
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
