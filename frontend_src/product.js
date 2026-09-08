// Pure display calculations; exact decimal fractions, rounded only for display.
(() => {
  const divisors = {monthly:1,quarterly:3,annual:12};
  function decimal(value) {
    const match = /^([+-]?)(\d+)(?:\.(\d*))?(?:e([+-]?\d+))?$/i.exec(String(value ?? ""));
    if (!match || Math.abs(Number(match[4] || 0)) > 30) return null;
    const n = BigInt(match[2]+(match[3] || ""))*(match[1] === "-" ? -1n : 1n), scale = (match[3] || "").length-Number(match[4] || 0);
    return scale >= 0 ? [n,10n**BigInt(scale)] : [n*10n**BigInt(-scale),1n];
  }
  const add = (a,b) => [a[0]*b[1]+b[0]*a[1],a[1]*b[1]];
  const divide = (a,n) => a && n > 0 ? [a[0],a[1]*BigInt(n)] : null;
  function dollars(value) {
    if (!value) return "—";
    const negative = value[0] < 0n, scaled = (negative ? -value[0] : value[0])*100n;
    const cents = scaled/value[1]+(2n*(scaled%value[1]) >= value[1] ? 1n : 0n);
    return (negative ? "−$" : "$")+String(cents/100n)+"."+String(cents%100n).padStart(2,"0");
  }
  const utcDay = value => /^\d{4}-\d{2}-\d{2}$/.test(value || "") ? Date.parse(value+"T00:00:00Z") : NaN;
  const daysInclusive = (start,end) => Math.max(0,Math.round((utcDay(end)-utcDay(start))/86400000)+1) || 0;
  function periodDays(today,cadence) {
    const date = new Date(utcDay(today)); if (!Number.isFinite(date.getTime())) return 0;
    const year=date.getUTCFullYear(),month=date.getUTCMonth(),start=cadence === "annual" ? 0 : cadence === "quarterly" ? Math.floor(month/3)*3 : month;
    return divisors[cadence] ? Math.round((Date.UTC(year,start+divisors[cadence],1)-Date.UTC(year,start,1))/86400000) : 0;
  }
  function currentTotal(plans) {
    const terms=plans.flatMap(p=>p.terms.filter(t=>t.status === "active")); if (!terms.length) return null;
    const values=terms.map(t=>decimal(t.monthly_equivalent)); return values.some(v=>!v) ? null : values.reduce(add,[0n,1n]);
  }
  function priceCards(amount,cadence,today,plans) {
    const value=decimal(amount),monthly=divide(value,divisors[cadence]),total=currentTotal(plans);
    const unknownTotal=!total && plans.some(p=>p.terms.some(t=>t.status==="active"));
    return {daily:dollars(divide(value,periodDays(today,cadence))),monthly:dollars(monthly),after:dollars(monthly && !unknownTotal ? add(total || [0n,1n],monthly) : null)};
  }
  const wizardSteps = operation => operation === "add" ? [1,2,3] : operation === "end" ? [3] : [2,3];
  function buildPlanMutation(op,values,p,t,preview,confirmed) {
    const body={operation:op};
    if(op!=="add")Object.assign(body,{plan_id:p.id,expected_version:p.version});
    if(op==="schedule")Object.assign(body,{effective_date:values.start_date,values:{amount_usd:values.amount_usd,cadence:values.cadence}});
    else if(op==="end")body.end_date=values.end_date;
    else body.values=values;
    if(op==="correct"){
      body.term_id=t?.id;
      if(preview && confirmed)Object.assign(body,{confirmed:true,preview_token:preview.preview_token});
      else body.preview=true;
    }
    return body;
  }
  const sourceTool = source => ({claude_local:"claude-code",openai_admin:"codex",anthropic_admin:"claude-code",cursor_usage_service:"cursor",cursor_csv:"cursor",cursor_admin:"cursor"}[source] || source.replace(/_local$/,""));
  const capacitySourceKey = source => sourceTool(source) === "zcode" ? "opencode" : sourceTool(source);
  const sourceTone = state => ["healthy","partial"].includes(state) ? "good" : ["detected_without_history","stale"].includes(state) ? "warning" : state === "failed" ? "danger" : "quiet";
  function harnessCount(sources) { const count=sources.filter(s=>["healthy","partial"].includes(s.state)).length; return {count,label:count ? `${count} harnesses` : "No harnesses",tone:count ? "good" : sources.some(s=>s.state === "detected_without_history") ? "warning" : "quiet"}; }
  const helpers={decimal,dollars,daysInclusive,periodDays,currentTotal,priceCards,sourceTool,capacitySourceKey,sourceTone,harnessCount,wizardSteps,buildPlanMutation};
  if (typeof module !== "undefined") module.exports=helpers;
  if (typeof window !== "undefined") window.ProductLogic=helpers;
})();
// Optional product dialogs own their requests independently of dashboard polling.
(() => {
  if (typeof document === "undefined") return;
  const {decimal,dollars,daysInclusive,currentTotal,priceCards,sourceTool,sourceTone,harnessCount,wizardSteps,buildPlanMutation}=window.ProductLogic;
  const el = id => document.getElementById(id);
  function restoreOpener(button,fallback) {
    const ownerDialog=button?.closest("dialog");
    if(button?.isConnected && !button.closest("[hidden]") && (!ownerDialog || ownerDialog.open))button.focus();
    else el(fallback).focus();
  }
  const setup = el("setup-guidance");
  const preference = "burnrate:setup:v1";
  let setupData = null;
  let metadataGeneration=0;
  const showSetup = event => openHarnesses(event?.currentTarget || document.activeElement);
  const closeSetup = status => {
    try { localStorage.setItem(preference, status); } catch {}
    setup.hidden = true; el("nav-connect").focus();
  };
  el("setup-help").addEventListener("click", showSetup);
  el("setup-dismiss").addEventListener("click", () => closeSetup("dismissed"));
  el("setup-done").addEventListener("click", () => closeSetup("complete"));

  const initialMetadataGeneration=metadataGeneration;
  fetch("/api/onboarding", {cache:"no-store"}).then(r => {
    if (!r.ok) throw new Error("Setup metadata unavailable");
    return r.json();
  }).then(data => {
    if(initialMetadataGeneration!==metadataGeneration)return;
    setupData = data;
    if (!sourceData.length) acceptSources(data.sources || []);
    el("setup-timezone").textContent = "Timezone: " + data.timezone + ". Configure " + data.timezoneSetting + ".";
    let saved;
    try { saved = localStorage.getItem(preference); } catch {}
    // Setup is explicitly opened; discovery never runs automatically.
  }).catch(() => {
    if(initialMetadataGeneration!==metadataGeneration)return;
    el("setup-timezone").textContent = "Setup metadata is unavailable. The dashboard remains usable; check Diagnostics or reload to retry.";
  });
  window.renderSourceGuidance = payload => {
    const sources = payload.sources || setupData?.sources || [];
    acceptSources(sources);
    if (harnessDialog.open && harnessMetadataReady && harnessDialog.getAttribute("aria-busy") !== "true") renderHarnesses({...setupData,sources});
    const root = el("source-guidance");
    reconcileChildren(root, payload.sources || setupData?.sources || [], source => source.source,
      () => nodeFrom('<section class="panel"><h3></h3><p></p><p></p><p></p><p></p><p></p><p></p><a>Integration documentation</a></section>'),
      (card, source) => {
      setText(card.querySelector("h3"), source.source + " · " + source.state.replaceAll("_"," "));
      const texts = [
        "Latest attempt: " + (source.lastAttempt || "none") + " · Last success: " + (source.lastSuccess || "none"),
        "Measurements: " + (source.measurements.join(", ") || "none observed") + (source.freshnessSeconds == null ? "" : " · Last success " + Math.round(source.freshnessSeconds) + "s ago"),
        source.reason || "", source.nextAction,
        source.pricingMissing.length ? "Dollar value unavailable for: " + source.pricingMissing.join(", ") : "",
        source.experimental ? "Experimental integration; other sources continue independently." : ""
      ];
      card.querySelectorAll("p").forEach((p,index) => { setText(p,texts[index]); p.hidden=!texts[index]; });
      card.querySelector("a").href = source.documentation;
    });
  };

  let sourceData = [];
  const harnessDialog=el("harness-manager"), metadataError="Setup metadata is unavailable. The dashboard remains usable; check Diagnostics or reload to retry.";
  let harnessGeneration=0,harnessController,harnessOpener,harnessScannedAt=null,harnessRestore=true,harnessMetadataReady=false;
  const relative = seconds => seconds == null ? "unavailable" : seconds<60 ? "just now" : seconds<3600 ? Math.floor(seconds/60)+"m ago" : seconds<86400 ? Math.floor(seconds/3600)+"h ago" : Math.floor(seconds/86400)+"d ago";
  function acceptSources(sources) {
    sourceData=sources; const count=harnessCount(sources);
    el("nav-connect").querySelector("span").textContent=count.label; el("nav-connect").dataset.tone=count.tone;
    window.productSourceGuidance=sources; window.refreshCapacitySourceHints?.();
  }
  function renderHarnesses(payload) {
    el("harness-content").hidden=false; el("harness-message").hidden=true;
    el("harness-scanned").textContent="scanned "+relative(harnessScannedAt == null ? null : (Date.now()-harnessScannedAt)/1000)+" · read-only";
    el("harness-docs").href=payload.documentation || payload.sources?.find(s=>s.documentation)?.documentation || "";
    const local=(payload.sources || []).filter(s=>s.source.endsWith("_local") || ["cursor_usage_service","cursor_csv"].includes(s.source));
    reconcileChildren(el("harness-rows"),local,s=>s.source,()=>nodeFrom('<article class="source-setting-row"><i class="settings-dot"></i><div class="source-setting-name"><strong></strong><span class="experimental-pill" hidden>Experimental</span><small class="mono"></small></div><div class="source-setting-status"><b></b><small></small></div><button type="button" class="ghost-button row-secondary"></button></article>'),(node,source)=>{
      node.dataset.tone=sourceTone(source.state); setText(node.querySelector("strong"),sourceTool(source.source));
      node.querySelector(".experimental-pill").hidden=!source.experimental; setText(node.querySelector(".mono"),source.path || "");
      setText(node.querySelector(".source-setting-status b"),source.state.replaceAll("_"," "));
      setText(node.querySelector(".source-setting-status small"),source.state === "healthy" ? "last event "+relative(source.freshnessSeconds) : source.reason || source.nextAction || "");
      const button=node.querySelector("button"),action=["healthy","partial","receiving_usage"].includes(source.state) ? "View usage" : ["detected_without_history","stale","waiting_activity","awaiting_ingest"].includes(source.state) ? "Recheck" : "Docs";
      setText(button,action); button.onclick=()=>{ if (action === "View usage") { harnessRestore=false;harnessDialog.close(); window.openHarnessUsage?.(sourceTool(source.source)); } else if(action === "Recheck") refreshHarnesses(); else if(source.documentation) window.open(source.documentation,"_blank","noopener,noreferrer"); };
    });
    if (!local.length) setEmpty(el("harness-rows"),'<p class="settings-helper">No local source metadata is available.</p>');
    const integrations=payload.integrations || []; el("harness-vault-note").hidden=!integrations.some(i=>i.managed);
    reconcileChildren(el("integration-rows"),integrations,i=>i.setting,()=>nodeFrom('<article class="source-setting-row integration-row"><i class="settings-dot"></i><div class="source-setting-name"><strong></strong><span class="experimental-pill" hidden>Experimental</span><small class="mono"></small></div><div class="source-setting-status"><b></b></div><button class="config-indicator" type="button" aria-expanded="false"><span class="toggle-track" aria-hidden="true"><i></i></span></button><div class="integration-hint" hidden><span></span><button type="button" class="ghost-button row-secondary">Copy</button></div></article>'),(node,item)=>{
      node.dataset.tone=item.configured ? "good" : "quiet"; setText(node.querySelector("strong"),item.setting.replace(/^BURNRATE_ENABLE_/,"").replaceAll("_"," "));
      node.querySelector(".experimental-pill").hidden=!item.experimental; setText(node.querySelector(".mono"),`${item.setting} → ${item.host}`);
      setText(node.querySelector(".source-setting-status b"),item.configured ? "configured" : "not configured");
      const button=node.querySelector(".config-indicator"),hint=node.querySelector(".integration-hint"),instructions=`Set ${item.setting} in .env and restart burnrate serve.`;
      hint.id="integration-hint-"+item.setting;button.setAttribute("aria-controls",hint.id);
      button.setAttribute("aria-label",`${item.setting}: ${item.configured ? "configured" : "not configured"}. Show configuration instructions; read-only.`);
      button.onclick=()=>{hint.hidden=!hint.hidden;button.setAttribute("aria-expanded",String(!hint.hidden));}; setText(hint.querySelector("span"),instructions);
      hint.querySelector("button").onclick=async()=>{try {await navigator.clipboard.writeText(item.setting);setText(hint.querySelector("span"),instructions+" Setting name copied.");}catch{setText(hint.querySelector("span"),instructions+" Copy this setting name manually: "+item.setting);}};
    });
    if (!integrations.length) setEmpty(el("integration-rows"),'<p class="settings-helper">No integration metadata is available.</p>');
  }
  async function refreshHarnesses() {
    const owner=++harnessGeneration; ++metadataGeneration; harnessController?.abort();harnessController=new AbortController();harnessDialog.setAttribute("aria-busy","true");
    try {const response=await fetch("/api/onboarding",{cache:"no-store",signal:harnessController.signal});if(!response.ok)throw new Error();const payload=await response.json();if(owner!==harnessGeneration || !harnessDialog.open)return;harnessMetadataReady=true;harnessScannedAt=Date.now();setupData=payload;acceptSources(payload.sources || []);renderHarnesses(payload);}
    catch(error){if(owner===harnessGeneration && error.name!=="AbortError"){harnessMetadataReady=false;el("harness-content").hidden=true;el("harness-message").hidden=false;el("harness-message").textContent=metadataError;}}
    finally{if(owner===harnessGeneration)harnessDialog.setAttribute("aria-busy","false");}
  }
  function openHarnesses(button=document.activeElement){harnessOpener=button;harnessRestore=true;if(!harnessDialog.open)harnessDialog.showModal();el("harness-manager-title").focus();refreshHarnesses();}
  window.openHarnessManager=openHarnesses;
  ["nav-connect","capacity-connect"].forEach(id=>el(id).addEventListener("click",showSetup));
  el("rescan-harnesses").addEventListener("click",refreshHarnesses);el("close-harnesses").addEventListener("click",()=>harnessDialog.close());
  harnessDialog.addEventListener("close",()=>{++harnessGeneration;harnessController?.abort();harnessDialog.setAttribute("aria-busy","false");if(harnessRestore)restoreOpener(harnessOpener,"nav-connect");});
  el("managed-connections").addEventListener("click",()=>{harnessRestore=false;harnessDialog.close();restoreOpener(harnessOpener,"nav-connect");window.BurnrateConnections.open();});
  el("show-setup-help").addEventListener("click",()=>{setup.hidden=false;harnessOpener=el("setup-done");harnessDialog.close();el("setup-done").focus();});

  const dialog = el("plan-manager"), form = el("plan-form");
  let data = {plans:[]}, generation = 0, controller, pending = false, preview = null;
  let retryKey = "", requestId = "", opener, writeOwner = null;
  let step = 1;
  const expanded = new Set(), histories = new Set();
  const color = key => window.colorFor?.(key) || "var(--dim)";
  const displayTool = key => key === "opencode" ? "Z.AI (OpenCode / ZCode)" : key;
  const steps = () => wizardSteps(el("plan-action").value);
  const plan = () => data.plans.find(p => String(p.id) === el("plan-choice").value);
  const term = () => plan()?.terms.find(t => String(t.id) === el("term-choice").value);
  const message = text => { el("plan-message").textContent = text; };
  function resetPreview() {
    preview = null; el("plan-preview").hidden = true;
    el("plan-confirm-label").hidden = true; el("plan-confirm").checked = false;
  }

  function showList(){el("plan-list-view").hidden=false;form.hidden=true;el("plan-list-view").querySelector("footer").before(el("plan-message"));el("plan-manager-title").focus();}
  function updateWizard(){
    form.querySelector("footer").before(el("plan-message"));
    const op=el("plan-action").value,cadence=el("plan-cadence").value,amount=el("plan-amount").value;
    for(const n of [1,2,3])el("plan-step-"+n).hidden=n!==step;
    el("plan-steps").querySelectorAll("li").forEach((item,index)=>{item.hidden=!steps().includes(index+1);item.dataset.state=index+1===step ? "current" : index+1<step ? "done" : "upcoming";if(index+1===step)item.setAttribute("aria-current","step");else item.removeAttribute("aria-current");});
    const cards=priceCards(amount,cadence,data.today,data.plans);
    el("plan-per-day").textContent=cards.daily;el("plan-equiv").textContent=cards.monthly;el("plan-after").textContent=cards.after;
    el("plan-day-label").textContent="Per day"+(data.today ? " ("+new Intl.DateTimeFormat("en",{month:"short",timeZone:"UTC"}).format(new Date(data.today+"T00:00:00Z"))+")" : "");
    form.querySelectorAll("[data-cadence]").forEach(button=>button.setAttribute("aria-pressed",String(button.dataset.cadence===cadence)));
    el("plan-tool-cards").querySelectorAll("button").forEach(button=>button.setAttribute("aria-pressed",String(button.dataset.tool===el("plan-tool").value)));
    el("plan-start-label").hidden=op==="end";el("plan-end-label").hidden=op==="schedule";el("plan-end-note").hidden=op!=="end";el("plan-review").hidden=op==="end";
    el("plan-review").querySelector("i").style.background=color(el("plan-tool").value);el("plan-review").querySelector("strong span").textContent=el("plan-name").value+" · "+el("plan-tool").value;
    el("plan-end-caption").textContent=op==="end" ? "Last active date" : "Last active date (optional)";
    const end=op==="schedule" ? plan()?.end_date : el("plan-end").value;
    el("plan-review").querySelector("p").textContent=`${dollars(decimal(amount))} every ${{monthly:"month",quarterly:"quarter",annual:"year"}[cadence]}, from ${el("plan-start").value}, ${end ? "through "+end : "ongoing"}. Adds ${cards.monthly}/mo to fixed costs.`;
    el("plan-back").textContent=step===steps()[0] ? "Cancel" : "Back";
    el("save-plan").textContent=step<3 ? "Continue" : op==="schedule" ? "Schedule" : op==="end" ? "Record end" : op==="correct" ? (preview ? "Confirm correction" : "Preview correction") : "Save plan";
    el("save-plan").dataset.operation=op;
  }
  function fields(fill=false){
    const op=el("plan-action").value,p=plan(),chosen=el("term-choice").value;
    el("term-choice").replaceChildren(...(p?.terms || []).map(t=>new Option(t.start_date+" · "+t.cadence,t.id)));
    if([...el("term-choice").options].some(o=>o.value===chosen))el("term-choice").value=chosen;
    const t=op==="correct" ? term() : p?.terms.at(-1);
    if(fill && t && op!=="add"){
      el("plan-name").value=t.name;el("plan-tool").value=t.tool_key;el("plan-amount").value=t.amount_usd;el("plan-cadence").value=t.cadence;
      el("plan-start").value=op==="schedule" ? data.today : t.start_date;el("plan-end").value=op==="end" ? p.end_date || data.today : t.end_date || "";
    }
    el("term-choice-label").hidden=op!=="correct";el("plan-correction-fields").hidden=op!=="correct";
    (op==="correct" ? el("plan-correction-fields") : el("plan-tool-slot")).append(el("plan-tool-fields"));
    el("plan-name").required=["add","correct"].includes(op);el("plan-amount").required=op!=="end";el("plan-start").required=op!=="end";el("plan-end").required=op==="end";
    resetPreview();updateWizard();
  }
  function startWizard(op="add",id="",termId=""){
    if(pending)return;
    el("plan-action").value=op;if(id!=="")el("plan-choice").value=String(id);
    const terms=plan()?.terms || [];el("term-choice").replaceChildren(...terms.map(t=>new Option(t.start_date+" · "+t.cadence,t.id)));el("term-choice").value=String(termId || terms.at(-1)?.id || "");
    if(op==="add"){el("plan-name").value="";el("plan-amount").value="";el("plan-start").value=data.today || "";el("plan-end").value="";el("plan-cadence").value="monthly";}
    step=steps()[0];retryKey="";requestId="";fields(op!=="add");message("");el("plan-list-view").hidden=true;form.hidden=false;el("plan-manager-title").focus();
  }
  function renderPlans(){
    el("plan-monthly-total").textContent=dollars(currentTotal(data.plans));
    reconcileChildren(el("plan-history"),data.plans,p=>p.id,()=>nodeFrom('<article class="plan-list-row"><div class="plan-row-main"><i class="tool-swatch"></i><div><strong></strong><small></small></div><span class="plan-row-price mono"></span><button class="ghost-button row-secondary" type="button">Edit</button></div><div class="term-bar" aria-label="Price history"></div><div class="plan-row-actions" hidden><button type="button" class="ghost-button row-secondary" data-plan-op="schedule">Change price</button><button type="button" class="ghost-button row-secondary" data-plan-op="end">Record end</button><button type="button" class="ghost-button row-secondary" data-history>View history</button><button type="button" class="settings-link row-secondary" data-plan-op="correct">Correct a term…</button></div><div class="term-history" hidden></div></article>'),(node,p)=>{
      const latest=p.terms.at(-1),current=p.terms.find(t=>t.status==="active") || latest;if(!latest)return;
      node.querySelector(".tool-swatch").style.background=color(latest.tool_key);setText(node.querySelector("strong"),latest.name);
      const start=p.terms[0].start_date,end=p.end_date || latest.end_date;
      setText(node.querySelector("small"),(end ? `${start} → ${end}` : `${start} · ongoing`)+(p.terms.length>1 ? ` · price changed ${p.terms.length-1}×` : ""));
      setText(node.querySelector(".plan-row-price"),dollars(decimal(current.amount_usd))+"/"+{monthly:"mo",quarterly:"qtr",annual:"yr"}[current.cadence]);
      const edit=node.querySelector(".plan-row-main button"),actions=node.querySelector(".plan-row-actions"),history=node.querySelector(".term-history");
      actions.hidden=!expanded.has(p.id);history.hidden=!expanded.has(p.id)||!histories.has(p.id);setText(edit,expanded.has(p.id)?"Close":"Edit");edit.setAttribute("aria-expanded",String(expanded.has(p.id)));
      actions.id="plan-actions-"+p.id;edit.setAttribute("aria-controls",actions.id);edit.setAttribute("aria-label",(expanded.has(p.id)?"Close actions for ":"Edit ")+latest.name+" · plan "+p.id);
      edit.onclick=()=>{if(expanded.has(p.id))expanded.delete(p.id);else expanded.add(p.id);renderPlans();};
      actions.querySelectorAll("[data-plan-op]").forEach(button=>{button.onclick=()=>startWizard(button.dataset.planOp,p.id);});
      const historyButton=actions.querySelector("[data-history]");historyButton.setAttribute("aria-expanded",String(histories.has(p.id)));historyButton.onclick=()=>{if(histories.has(p.id))histories.delete(p.id);else histories.add(p.id);renderPlans();};
      reconcileChildren(node.querySelector(".term-bar"),p.terms,t=>t.id,()=>document.createElement("span"),(segment,t)=>{segment.style.background=color(t.tool_key);segment.style.flexGrow=String(daysInclusive(t.start_date,t.end_date || p.end_date || data.today));segment.dataset.active=String(t.status==="active");segment.title=`${t.start_date} → ${t.end_date || p.end_date || "ongoing"}`;});
      reconcileChildren(history,p.terms,t=>t.id,()=>nodeFrom('<div class="term-history-row"><i class="settings-dot"></i><span></span><strong class="mono"></strong><button type="button" class="settings-link row-secondary">Correct</button></div>'),(row,t)=>{row.dataset.tone=t.status==="active"?"good":"quiet";setText(row.querySelector("span"),`${t.start_date} → ${t.end_date || p.end_date || "ongoing"}`);setText(row.querySelector("strong"),dollars(decimal(t.amount_usd))+"/"+{monthly:"mo",quarterly:"qtr",annual:"yr"}[t.cadence]);row.querySelector("button").onclick=()=>startWizard("correct",p.id,t.id);});
    });
    if(!data.plans.length)setEmpty(el("plan-history"),'<p class="settings-helper">No configured plans. Adding one is optional.</p>');
    reconcileChildren(el("plan-tool-cards"),data.tools || [],t=>t,()=>nodeFrom('<button type="button"><i class="tool-swatch"></i><span></span></button>'),(button,t)=>{button.dataset.tool=t;button.querySelector("i").style.background=color(t);setText(button.querySelector("span"),displayTool(t));button.onclick=()=>{el("plan-tool").value=t;resetPreview();updateWizard();};});
  }
  async function readPlans(preserve = true) {
    const owned = ++generation;
    controller?.abort(); controller = new AbortController();
    dialog.setAttribute("aria-busy", "true");
    el("new-plan").disabled = true;
    try {
      const response = await fetch("/api/subscriptions", {cache:"no-store", signal:controller.signal});
      if (!response.ok) throw new Error("Could not load subscriptions");
      const incoming = await response.json();
      if (owned !== generation || !dialog.open) return;
      const choice = el("plan-choice").value, tool = el("plan-tool").value;
      data = incoming;
      el("plan-choice").replaceChildren(...data.plans.map(p => new Option(p.terms.at(-1).name + " · #" + p.id, p.id)));
      if ([...el("plan-choice").options].some(o => o.value === choice)) el("plan-choice").value = choice;
      el("plan-tool").replaceChildren(...data.tools.map(t => new Option(displayTool(t), t)));
      if (tool) el("plan-tool").value = tool;
      el("plan-timezone").textContent = "Dates use " + data.timezone + ". Monthly equivalents are references; selected-window cost is prorated separately.";
      renderPlans();
      if (!preserve) fields(true);
      message("History loaded. Unsaved fields are preserved.");
      return true;
    } catch (error) {
      if (owned === generation && error.name !== "AbortError") message(error.message + ". Close and reopen to reload history. Your input is preserved.");
      return false;
    } finally {
      if (owned === generation) { dialog.setAttribute("aria-busy", "false"); el("new-plan").disabled = !(data.tools || []).length; }
    }
  }

  function openPlans(button=document.activeElement){opener=button;if(!dialog.open)dialog.showModal();showList();readPlans();}
  window.BurnratePlans={open:openPlans};
  ["nav-plans","manage-plans","setup-plans"].forEach(id=>el(id).addEventListener("click",event=>openPlans(event.currentTarget)));
  el("close-plans").addEventListener("click",()=>dialog.close());
  dialog.addEventListener("close",()=>{++generation;controller?.abort();writeOwner=null;pending=false;el("save-plan").disabled=false;restoreOpener(opener,"nav-plans");});
  el("new-plan").addEventListener("click",()=>startWizard());
  el("plan-back").addEventListener("click",()=>{if(pending)return;resetPreview();if(step===steps()[0])showList();else{step=steps()[steps().indexOf(step)-1];updateWizard();el("plan-manager-title").focus();}});
  el("term-choice").addEventListener("change",()=>fields(true));
  form.querySelectorAll("[data-cadence]").forEach(button=>button.addEventListener("click",()=>{el("plan-cadence").value=button.dataset.cadence;resetPreview();updateWizard();}));
  form.addEventListener("input",event=>{if(event.target.id!=="plan-confirm"){resetPreview();updateWizard();}});
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (pending) return;
    const op = el("plan-action").value, p = plan(), t = term();
    if (op !== "add" && !p) { message("Select a plan first."); return; }
    const required=step===1 ? ["plan-name"] : step===2 ? ["plan-amount",...(op==="correct"?["plan-name"]:[])] : op==="end" ? ["plan-end"] : ["plan-start",...(op==="schedule"?[]:["plan-end"])];
    if(op==="correct" && step===2 && !el("plan-name").value)el("plan-correction-fields").open=true;
    for(const id of required)if(!el(id).reportValidity())return;
    if(step<3){step=steps()[steps().indexOf(step)+1];updateWizard();el("plan-manager-title").focus();return;}
    const values = {name:el("plan-name").value, tool_key:el("plan-tool").value,
      amount_usd:el("plan-amount").value, cadence:el("plan-cadence").value,
      start_date:el("plan-start").value, end_date:el("plan-end").value || null};
    const body = buildPlanMutation(op,values,p,t,preview,el("plan-confirm").checked);
    const key = JSON.stringify(body);
    if (key !== retryKey) { retryKey = key; requestId = crypto.randomUUID(); }
    body.request_id = requestId;
    const owned = ++generation;
    const owner = {};
    writeOwner = owner;
    controller?.abort(); controller = new AbortController(); pending = true;
    el("save-plan").disabled = true; dialog.setAttribute("aria-busy","true"); message("Saving…");
    try {
      const response = await fetch("/api/subscriptions", {method:"POST", signal:controller.signal,
        headers:{"Content-Type":"application/json","X-BURNRATE-Request":"1"},body:JSON.stringify(body)});
      const result = await response.json();
      if (owned !== generation || !dialog.open) return;
      if (!response.ok) throw new Error((response.status === 409 ? "Conflict: reload history before retrying. " : "") + (result.error || "Save failed"));
      if (body.preview) {
        preview = result; el("plan-preview").hidden = false; el("plan-confirm-label").hidden = false;
        el("plan-before-value").textContent=dollars(decimal(result.before_usd)); el("plan-after-value").textContent=dollars(decimal(result.after_usd));
        el("plan-affected").textContent=daysInclusive(result.from,result.through)+" days affected · "+result.from+" → "+result.through;
        el("plan-preview-note").textContent=result.note;
        el("save-plan").textContent = "Confirm correction"; message("Review the affected period and check confirmation.");
        el("plan-confirm").focus();
      } else {
        resetPreview();
        const loaded = await readPlans();
        if (!dialog.open || writeOwner !== owner) return;
        showList(); message(loaded ? "Saved durably. Dashboard costs update on the next refresh." : "Saved durably, but history could not reload. Close and reopen to retry.");
      }
    } catch (error) {
      if (owned === generation && error.name !== "AbortError") message(error.message + ". Your input is preserved.");
    } finally {
      if (writeOwner === owner) {
        writeOwner = null; pending = false; el("save-plan").disabled = false;
        if (dialog.open) dialog.setAttribute("aria-busy","false");
      }
    }
  });
})();
