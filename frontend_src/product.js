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
function buildPlanMutation(op,values,p,t,preview,confirmed){const b={operation:op};if(op!=="add")Object.assign(b,{plan_id:p.id,expected_version:p.version});if(op==="schedule")Object.assign(b,{effective_date:values.start_date,values:{amount_usd:values.amount_usd,cadence:values.cadence}});else if(op==="end")b.end_date=values.end_date;else b.values=values;if(op==="correct"){b.term_id=t?.id;if(preview&&confirmed)Object.assign(b,{confirmed:true,preview_token:preview.preview_token});else b.preview=true;}return b;}
  const sourceTool = source => ({claude_local:"claude-code",openai_admin:"codex",anthropic_admin:"claude-code",cursor_usage_service:"cursor",cursor_csv:"cursor",cursor_admin:"cursor"}[source] || source.replace(/_local$/,""));
  const capacitySourceKey = source => sourceTool(source) === "zcode" ? "opencode" : sourceTool(source);
  const sourceTone = state => ["healthy","partial"].includes(state) ? "good" : ["detected_without_history","stale"].includes(state) ? "warning" : state === "failed" ? "danger" : "quiet";
  const harnessCount = sources => { const count=sources.filter(s=>["healthy","partial"].includes(s.state)).length; return {count,label:count ? `${count} harnesses` : "No harnesses",tone:count ? "good" : sources.some(s=>s.state === "detected_without_history") ? "warning" : "quiet"}; };
  const helpers={decimal,dollars,daysInclusive,periodDays,currentTotal,priceCards,sourceTool,capacitySourceKey,sourceTone,harnessCount,wizardSteps,buildPlanMutation};
  if (typeof module !== "undefined") module.exports=helpers;
  if (typeof window !== "undefined") window.ProductLogic=helpers;
})();
// Optional product dialogs own their requests independently of dashboard polling.
(() => {
  if (typeof document === "undefined") return;
  const {n:nodeFrom,t:setText,e:setEmpty,r:reconcileChildren}=window.BurnrateDOM;
  const {decimal,dollars,daysInclusive,currentTotal,priceCards,sourceTool,sourceTone,harnessCount,wizardSteps,buildPlanMutation}=window.ProductLogic;
  const el = id => document.getElementById(id);
  function restoreOpener(button,fallback) {
    const ownerDialog=button?.closest?.("dialog"),active=[...document.querySelectorAll("dialog[open]")].at(-1);
    if(button?.isConnected && typeof button.focus === "function" && !button.closest?.("[hidden]") && (!ownerDialog || ownerDialog.open) && (!active || ownerDialog===active))button.focus();
    else (active?.querySelector("[tabindex='-1']") || active || el(fallback)).focus();
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
    const zone = el("setup-timezone");
    const setting = String(data.timezoneSetting || "");
    const key = setting.split(/\s+/)[0];
    zone.textContent = "Timezone " + data.timezone + (key ? " · " + key : "");
    if (setting && setting !== key) zone.title = setting;
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
    reconcileChildren(root, sources, s => s.source,
      () => nodeFrom('<section class="panel"><h3></h3><p></p><p></p><p></p><p></p><p></p><p></p><a>Integration documentation</a></section>'),
      (card, s) => {
      setText(card.querySelector("h3"), s.source + " · " + s.state.replaceAll("_"," "));
      const t = [
        "Latest attempt: " + (s.lastAttempt || "none") + " · Last success: " + (s.lastSuccess || "none"),
        "Measurements: " + (s.measurements.join(", ") || "none observed") + (s.freshnessSeconds == null ? "" : " · Last success " + Math.round(s.freshnessSeconds) + "s ago"),
        s.reason || "", s.nextAction,
        s.pricingMissing.length ? "Dollar value unavailable for: " + s.pricingMissing.join(", ") : "",
        s.experimental ? "Experimental integration; other sources continue independently." : ""
      ];
      card.querySelectorAll("p").forEach((p,i) => { setText(p,t[i]); p.hidden=!t[i]; });
      card.querySelector("a").href = s.documentation;
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
    el("harness-scanned").textContent=harnessScannedAt == null ? "Not checked yet" : "Checked "+relative((Date.now()-harnessScannedAt)/1000);
    el("harness-docs").href=payload.documentation || payload.sources?.find(s=>s.documentation)?.documentation || "";
    const sourceCopy={antigravity_local:["Antigravity","Local app"],claude_local:["Claude Code","Transcripts"],codex_local:["Codex","Sessions"],cursor_local:["Cursor","Project files"],cursor_usage_service:["Cursor","Usage service"],cursor_csv:["Cursor","CSV import"],grok_local:["Grok","Local logs"],opencode_local:["OpenCode","Local database"],traycer_local:["Traycer","Host"],zcode_local:["ZCode","Local database"]};
    const integrationCopy={OPENAI_ADMIN_KEY:["OpenAI","Admin API"],ANTHROPIC_ADMIN_KEY:["Anthropic","Admin API"],CURSOR_API_KEY:["Cursor","Admin API"],OPENROUTER_MANAGEMENT_KEY:["OpenRouter","Management API"],BURNRATE_ENABLE_CLAUDE_OAUTH_USAGE:["Claude","Account usage"],BURNRATE_ENABLE_CURSOR_USAGE_SERVICE:["Cursor","Usage service"],BURNRATE_ENABLE_ZAI_QUOTA:["Z.AI","Quota"]};
    const integrationTool={OPENAI_ADMIN_KEY:"codex",ANTHROPIC_ADMIN_KEY:"claude-code",CURSOR_API_KEY:"cursor",OPENROUTER_MANAGEMENT_KEY:"openrouter",BURNRATE_ENABLE_CLAUDE_OAUTH_USAGE:"claude-code",BURNRATE_ENABLE_CURSOR_USAGE_SERVICE:"cursor",BURNRATE_ENABLE_ZAI_QUOTA:"zai"};
    const statusLine=source=>{
      const state=source.state;
      if(state==="healthy")return "Updated "+relative(source.freshnessSeconds);
      if(state==="partial")return (source.pricingMissing||[]).length?"Prices missing":"Partial read";
      if(state==="failed")return "Read failed";
      if(state==="stale")return "Out of date";
      if(state==="not_detected")return "Not installed";
      if(state==="configuration_missing")return "Not enabled";
      if(state==="not_checked"||state==="detected_without_history"||state==="waiting_activity"||state==="awaiting_ingest")return "No usage yet";
      return "";
    };
    const paintMark=(node,tool)=>{
      const slot=node.querySelector(".plan-logo"),dot=node.querySelector(".settings-dot");
      if(slot&&tool&&window.BurnrateMarks){slot.hidden=false;slot.replaceChildren(window.BurnrateMarks.mark(tool));if(dot)dot.hidden=true;}
      else{if(slot)slot.hidden=true;if(dot)dot.hidden=false;}
    };
    const local=(payload.sources || []).filter(s=>s.source.endsWith("_local") || ["cursor_usage_service","cursor_csv"].includes(s.source));
    reconcileChildren(el("harness-rows"),local,s=>s.source,()=>nodeFrom('<article class="source-setting-row"><span class="harness-mark"><span class="plan-logo" hidden></span><i class="settings-dot"></i></span><div class="source-setting-name"><strong></strong><span class="experimental-pill" hidden>Preview</span><small class="mono"></small></div><div class="source-setting-status"><b></b><small></small></div><button type="button" class="ghost-button row-secondary"></button></article>'),(node,source)=>{
      const copy=sourceCopy[source.source]||[sourceTool(source.source),""];
      node.dataset.tone=sourceTone(source.state); setText(node.querySelector("strong"),copy[0]);
      node.querySelector(".experimental-pill").hidden=!source.experimental;
      const pathNode=node.querySelector(".mono"); setText(pathNode,copy[1]); if(pathNode) pathNode.title=source.path||copy[1];
      const headline={healthy:"Healthy",partial:"Partial",failed:"Failed",stale:"Stale",not_detected:"Not found",not_checked:"Not checked",configuration_missing:"Off",detected_without_history:"Waiting",waiting_activity:"Waiting",awaiting_ingest:"Waiting"}[source.state]||String(source.state||"").replaceAll("_"," ");
      setText(node.querySelector(".source-setting-status b"),headline);
      const detail=statusLine(source),full=[detail,source.reason,source.nextAction].filter(Boolean).join(" — ");
      const note=node.querySelector(".source-setting-status small"); setText(note,detail); if(note) note.title=full;
      paintMark(node,sourceTool(source.source));
      const button=node.querySelector("button"),action=["healthy","partial","receiving_usage"].includes(source.state) ? "View usage" : ["detected_without_history","stale","waiting_activity","awaiting_ingest"].includes(source.state) ? "Recheck" : "Docs";
      setText(button,action); button.onclick=()=>{ if (action === "View usage") { harnessRestore=false;harnessDialog.close(); window.openHarnessUsage?.(sourceTool(source.source)); } else if(action === "Recheck") refreshHarnesses(); else if(source.documentation) window.open(source.documentation,"_blank","noopener,noreferrer"); };
    });
    if (!local.length) setEmpty(el("harness-rows"),'<p class="settings-helper">No local source metadata is available.</p>');
    const integrations=payload.integrations || []; el("harness-vault-note").hidden=!integrations.some(i=>i.managed);
    reconcileChildren(el("integration-rows"),integrations,i=>i.setting,()=>nodeFrom('<article class="source-setting-row integration-row"><span class="harness-mark"><span class="plan-logo" hidden></span><i class="settings-dot"></i></span><div class="source-setting-name"><strong></strong><span class="experimental-pill" hidden>Preview</span><small class="mono"></small></div><div class="source-setting-status"><b></b></div><button class="config-indicator" type="button" aria-expanded="false"><span class="toggle-track" aria-hidden="true"><i></i></span></button><div class="integration-hint" hidden><span></span><button type="button" class="ghost-button row-secondary">Copy</button></div></article>'),(node,item)=>{
      const copy=integrationCopy[item.setting]||[item.setting.replace(/^BURNRATE_ENABLE_/,"").replaceAll("_"," "),item.host];
      node.dataset.tone=item.configured ? "good" : "quiet"; setText(node.querySelector("strong"),copy[0]);
      node.querySelector(".experimental-pill").hidden=!item.experimental;
      const hostNode=node.querySelector(".mono"); setText(hostNode,copy[1]); if(hostNode) hostNode.title=`${item.setting} → ${item.host}`;
      setText(node.querySelector(".source-setting-status b"),item.configured ? (item.experimental ? "On" : "Key set") : (item.experimental ? "Off" : "Not set"));
      paintMark(node,integrationTool[item.setting]);
      const button=node.querySelector(".config-indicator"),hint=node.querySelector(".integration-hint"),instructions=item.configured?"This connection is on.":item.experimental?"Set this to 1, then restart BURNRATE.":"Add this key, then restart BURNRATE.";
      const copyValue=item.experimental?item.setting+"=1":item.setting;
      hint.id="integration-hint-"+item.setting;button.setAttribute("aria-controls",hint.id);
      button.setAttribute("aria-label",`${item.setting}: ${item.configured ? "configured" : "not configured"}. Show configuration instructions; read-only.`);
      button.onclick=()=>{hint.hidden=!hint.hidden;button.setAttribute("aria-expanded",String(!hint.hidden));}; setText(hint.querySelector("span"),instructions);
      hint.querySelector("button").onclick=async()=>{try {await navigator.clipboard.writeText(copyValue);setText(hint.querySelector("span"),"Copied. Restart BURNRATE after you save it.");}catch{setText(hint.querySelector("span"),instructions+" Copy this manually: "+copyValue);}};
    });
    if (!integrations.length) setEmpty(el("integration-rows"),'<p class="settings-helper">No integration metadata is available.</p>');
  }
  function setRescan(scanning) {
    const button=el("rescan-harnesses"); if(!button) return;
    button.classList?.toggle?.("is-scanning", scanning);
    button.disabled=scanning;
    button.setAttribute("aria-busy", scanning ? "true" : "false");
    const label=button.querySelector(".harness-rescan-label");
    if(label) label.textContent=scanning ? "Scanning" : "Rescan";
    const scanned=el("harness-scanned");
    if(scanned) scanned.textContent=scanning ? "Scanning sources…" : (harnessScannedAt==null ? "Not checked yet" : "Checked "+relative((Date.now()-harnessScannedAt)/1000));
  }
  async function refreshHarnesses() {
    const owner=++harnessGeneration; ++metadataGeneration; harnessController?.abort();harnessController=new AbortController();
    const started=Date.now();
    harnessDialog.setAttribute("aria-busy","true"); harnessDialog.classList?.add?.("is-scanning"); setRescan(true);
    try {const response=await fetch("/api/onboarding",{cache:"no-store",signal:harnessController.signal});if(!response.ok)throw new Error();const payload=await response.json();if(owner!==harnessGeneration || !harnessDialog.open)return;harnessMetadataReady=true;harnessScannedAt=Date.now();setupData=payload;acceptSources(payload.sources || []);renderHarnesses(payload);if(owner===harnessGeneration)setRescan(true);}
    catch(error){if(owner===harnessGeneration && error.name!=="AbortError"){harnessMetadataReady=false;el("harness-content").hidden=true;el("harness-message").hidden=false;el("harness-message").textContent=metadataError;}}
    finally{
      const remain=Math.max(0, 720-(Date.now()-started));
      if(remain && typeof setTimeout==="function") await new Promise(resolve=>setTimeout(resolve, remain));
      if(owner===harnessGeneration){harnessDialog.setAttribute("aria-busy","false"); harnessDialog.classList?.remove?.("is-scanning"); setRescan(false);}
    }
  }
  function openHarnesses(button=document.activeElement){harnessOpener=button;harnessRestore=true;if(!harnessDialog.open)harnessDialog.showModal();el("harness-manager-title").focus();refreshHarnesses();}
  window.openHarnessManager=openHarnesses;
  if(window.startAttention)window.BurnrateAttention=window.startAttention({
    connections:(_item,button)=>openHarnesses(button),
    capacity:()=>window.openAttentionView?.('capacity'),
    pricing:item=>window.openAttentionView?.('pricing',item.evidence.model)
  });
  ["nav-connect","capacity-connect"].forEach(id=>el(id).addEventListener("click",showSetup));
  el("rescan-harnesses").addEventListener("click",refreshHarnesses);el("close-harnesses").addEventListener("click",()=>harnessDialog.close());
  harnessDialog.addEventListener("close",()=>{if(harnessDialog.open)return;++harnessGeneration;harnessController?.abort();harnessDialog.setAttribute("aria-busy","false");if(harnessRestore)restoreOpener(harnessOpener,"nav-connect");});
  el("managed-connections").addEventListener("click",()=>{harnessRestore=false;harnessDialog.close();restoreOpener(harnessOpener,"nav-connect");window.BurnrateConnections.open();});
  el("show-setup-help").addEventListener("click",()=>{setup.hidden=false;harnessOpener=el("setup-done");harnessDialog.close();el("setup-done").focus();});

  const dialog = el("plan-manager"), form = el("plan-form");
  let data = {plans:[]}, generation = 0, controller, pending = false, preview = null;
  let retryKey = "", requestId = "", opener, writeOwner = null;
  let step = 1;
  const expanded = new Set(), histories = new Set();
  const color = key => window.colorFor?.(key) || "var(--dim)";
  const TOOL_LABELS = {codex:"Codex","claude-code":"Claude Code",cursor:"Cursor",grok:"SuperGrok",zai:"Z.AI",opencode:"Z.AI (OpenCode / ZCode)",zcode:"Z.AI",antigravity:"Antigravity",openrouter:"OpenRouter",xai:"xAI"};
  const displayTool = key => TOOL_LABELS[key] || key;
  const prettyDate = value => /^\d{4}-\d{2}-\d{2}$/.test(value || "") ? new Intl.DateTimeFormat("en-US",{month:"short",day:"numeric",year:"numeric",timeZone:"UTC"}).format(new Date(value+"T12:00:00Z")) : (value || "");
  const cadenceLabel = key => ({monthly:"Monthly",quarterly:"Quarterly",annual:"Yearly"})[key] || key;
  const planTabs = shown => { const tabs = dialog.querySelector(".plan-dialog-tabs"); if (tabs) tabs.hidden = !shown; };
  const steps = () => wizardSteps(el("plan-action").value);
  const plan = () => data.plans.find(p => String(p.id) === el("plan-choice").value);
  const term = () => plan()?.terms.find(t => String(t.id) === el("term-choice").value);
  const message = text => { el("plan-message").textContent = text; };
  function resetPreview() {
    preview = null; el("plan-preview").hidden = true;
    el("plan-confirm-label").hidden = true; el("plan-confirm").checked = false;
  }

  let plansValueCtrl = null;
  const planLead = text => { const node = el("plan-manager-lead"); if (node) node.textContent = text; };
  function showPlansValue() {
    const from = !el("plan-list-view").hidden ? "list" : !form.hidden ? "form" : "";
    if (from) dialog.dataset.from = from;
    const pv=el("plans-value-view");if(pv)pv.hidden=false;el("plan-list-view").hidden=true;form.hidden=true;
    for(const [t,s] of [["tab-plans-value",true],["tab-plans-list",false]]){const b=el(t);if(b){b.classList.toggle("active",s);b.setAttribute("aria-pressed",String(s));}}
    planTabs(true);el("plan-manager-title").textContent="Plans & Value";planLead("Reference usage set against configured cost.");el("plan-message").hidden=true;el("plan-manager-title").focus();
    if(!plansValueCtrl&&window.createPlansValueController&&pv){plansValueCtrl=window.createPlansValueController(pv,{timezone:data.timezone,openPlanManager:showList,openHarnesses:(_group,button)=>openHarnesses(button)});plansValueCtrl.load("this_month");}
    else if(plansValueCtrl){plansValueCtrl.reopen();plansValueCtrl.load();}
  }

  function showList(){
    plansValueCtrl?.close();
    const from = !form.hidden ? "form" : !el("plans-value-view").hidden ? "value" : "";
    if (from) dialog.dataset.from = from;
    const pv=el("plans-value-view");if(pv)pv.hidden=true;el("plan-list-view").hidden=false;form.hidden=true;
    for(const [t,s] of [["tab-plans-value",false],["tab-plans-list",true]]){const b=el(t);if(b){b.classList.toggle("active",s);b.setAttribute("aria-pressed",String(s));}}
    planTabs(true);el("plan-manager-title").textContent="Subscriptions";planLead("Price, term, and the dates each subscription covers.");el("plan-message").hidden=false;el("plan-list-view").querySelector("footer").before(el("plan-message"));el("plan-manager-title").focus();
  }
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
    const review=el("plan-review"),reviewSwatch=review.querySelector("i");if(reviewSwatch)reviewSwatch.style.background=color(el("plan-tool").value);
    const reviewLogo=review.querySelector(".plan-logo");if(reviewLogo&&window.BurnrateMarks)reviewLogo.replaceChildren(window.BurnrateMarks.mark(el("plan-tool").value));
    const reviewName=review.querySelector(".plan-review-name")||review.querySelector("strong span");if(reviewName)reviewName.textContent=el("plan-name").value||displayTool(el("plan-tool").value)||"Plan";
    el("plan-end-caption").textContent=op==="end" ? "Last active date" : "Last active date (optional)";
    el("plan-end-note").textContent=op==="end" ? "Leave this blank to keep the plan active. A date is the last day it applies, and it does not cancel the subscription with the provider." : "Inclusive. Attribution stops the following day. This does not cancel the plan with the provider.";
    const end=op==="schedule" ? plan()?.end_date : el("plan-end").value;
    const startLabel=prettyDate(el("plan-start").value),endLabel=end?prettyDate(end):"";
    el("plan-review").querySelector("p").textContent=`${dollars(decimal(amount))} every ${{monthly:"month",quarterly:"quarter",annual:"year"}[cadence]}, from ${startLabel}, ${endLabel ? "through "+endLabel : "ongoing"}. Adds ${cards.monthly}/mo to fixed costs.`;
    el("plan-back").textContent=step===steps()[0] ? "Cancel" : "Back";
    el("save-plan").textContent=step<3 ? "Continue" : op==="schedule" ? "Schedule" : op==="end" ? "Record end" : op==="correct" ? (preview ? "Confirm correction" : "Preview correction") : "Save plan";
    el("save-plan").dataset.operation=op;
  }
  function fields(fill=false){
    const op=el("plan-action").value,p=plan(),chosen=el("term-choice").value;
    el("term-choice").replaceChildren(...(p?.terms || []).map(t=>new Option(prettyDate(t.start_date)+" · "+cadenceLabel(t.cadence),t.id)));
    if([...el("term-choice").options].some(o=>o.value===chosen))el("term-choice").value=chosen;
    const t=op==="correct" ? term() : p?.terms.at(-1);
    if(fill && t && op!=="add"){
      el("plan-name").value=t.name;el("plan-tool").value=t.tool_key;el("plan-amount").value=t.amount_usd;el("plan-cadence").value=t.cadence;
      const lastTerm=p?.terms.at(-1);
      const keptActive=lastTerm && String(t.id)===String(lastTerm.id) ? (t.end_date || p.end_date || "") : (t.end_date || "");
      el("plan-start").value=op==="schedule" ? data.today : t.start_date;el("plan-end").value=op==="end" ? (p.end_date || data.today) : keptActive;
    }
    el("term-choice-label").hidden=op!=="correct";el("plan-correction-fields").hidden=op!=="correct";
    (op==="correct" ? el("plan-correction-fields") : el("plan-tool-slot")).append(el("plan-tool-fields"));
    el("plan-name").required=["add","correct"].includes(op);el("plan-amount").required=op!=="end";el("plan-start").required=op!=="end";el("plan-end").required=false;
    resetPreview();updateWizard();
  }
  function startWizard(op="add",id="",termId=""){
    if(pending)return;
    showList();
    el("plan-action").value=op;if(id!=="")el("plan-choice").value=String(id);
    const terms=plan()?.terms || [];el("term-choice").replaceChildren(...terms.map(t=>new Option(prettyDate(t.start_date)+" · "+cadenceLabel(t.cadence),t.id)));el("term-choice").value=String(termId || terms.at(-1)?.id || "");
    if(op==="add"){el("plan-name").value="";el("plan-amount").value="";el("plan-start").value=data.today || "";el("plan-end").value="";el("plan-cadence").value="monthly";}
    step=steps()[0];retryKey="";requestId="";fields(op!=="add");message("");dialog.dataset.from="list";el("plan-list-view").hidden=true;form.hidden=false;planTabs(false);
    el("plan-manager-title").textContent=op==="add"?"Add a plan":op==="schedule"?"Change price":op==="end"?"Record end":"Correct a term";
    planLead(op==="add"?"Choose the tool, the price, and when it starts.":op==="end"?"Leave the date blank to keep this plan active.":op==="schedule"?"The new price applies from the date you set.":"This rewrites a term already on record.");
    el("plan-manager-title").focus();
  }
  function renderPlans(){
    el("plan-monthly-total").textContent=dollars(currentTotal(data.plans));
    reconcileChildren(el("plan-history"),data.plans,p=>p.id,()=>nodeFrom('<article class="plan-list-row"><div class="plan-row-main"><span class="plan-logo" aria-hidden="true"></span><i class="tool-swatch"></i><div><strong></strong><small></small></div><span class="plan-row-price mono"></span><button class="ghost-button row-secondary" type="button">Edit</button></div><div class="term-bar" aria-label="Price history"></div><div class="plan-row-actions" hidden><button type="button" class="ghost-button row-secondary" data-plan-op="schedule">Change price</button><button type="button" class="ghost-button row-secondary" data-plan-op="end">Record end</button><button type="button" class="ghost-button row-secondary" data-history>View history</button><button type="button" class="settings-link row-secondary" data-plan-op="correct">Correct a term…</button></div><div class="term-history" hidden></div></article>'),(node,p)=>{
      const latest=p.terms.at(-1),current=p.terms.find(t=>t.status==="active") || latest;if(!latest)return;
      node.querySelector(".tool-swatch").style.background=color(latest.tool_key);setText(node.querySelector("strong"),latest.name);
      const slot=node.querySelector(".plan-logo");if(slot&&window.BurnrateMarks)slot.replaceChildren(window.BurnrateMarks.mark(latest.tool_key));
      const start=p.terms[0].start_date,end=p.end_date || latest.end_date;
      setText(node.querySelector("small"),(end ? `${start} → ${end}` : `${start} · ongoing`)+(p.terms.length>1 ? ` · price changed ${p.terms.length-1}×` : ""));
      setText(node.querySelector(".plan-row-price"),dollars(decimal(current.amount_usd))+"/"+{monthly:"mo",quarterly:"qtr",annual:"yr"}[current.cadence]);
      const edit=node.querySelector(".plan-row-main button"),actions=node.querySelector(".plan-row-actions"),history=node.querySelector(".term-history");
      actions.hidden=!expanded.has(p.id);history.hidden=!expanded.has(p.id)||!histories.has(p.id);setText(edit,expanded.has(p.id)?"Close":"Edit");edit.setAttribute("aria-expanded",String(expanded.has(p.id)));
      actions.id="plan-actions-"+p.id;edit.setAttribute("aria-controls",actions.id);edit.setAttribute("aria-label",(expanded.has(p.id)?"Close actions for ":"Edit ")+latest.name+" · plan "+p.id);
      edit.onclick=()=>{expanded.has(p.id)?expanded.delete(p.id):expanded.add(p.id);renderPlans();};
      actions.querySelectorAll("[data-plan-op]").forEach(b=>{b.onclick=()=>startWizard(b.dataset.planOp,p.id);});
      const hBtn=actions.querySelector("[data-history]");hBtn.setAttribute("aria-expanded",String(histories.has(p.id)));hBtn.onclick=()=>{histories.has(p.id)?histories.delete(p.id):histories.add(p.id);renderPlans();};
      reconcileChildren(node.querySelector(".term-bar"),p.terms,t=>t.id,()=>document.createElement("span"),(seg,t)=>{seg.style.background=color(t.tool_key);seg.style.flexGrow=String(daysInclusive(t.start_date,t.end_date || p.end_date || data.today));seg.dataset.active=String(t.status==="active");seg.title=`${t.start_date} → ${t.end_date || p.end_date || "ongoing"}`;});
      reconcileChildren(history,p.terms,t=>t.id,()=>nodeFrom('<div class="term-history-row"><i class="settings-dot"></i><span></span><strong class="mono"></strong><button type="button" class="settings-link row-secondary">Correct</button></div>'),(row,t)=>{row.dataset.tone=t.status==="active"?"good":"quiet";setText(row.querySelector("span"),`${t.start_date} → ${t.end_date || p.end_date || "ongoing"}`);setText(row.querySelector("strong"),dollars(decimal(t.amount_usd))+"/"+{monthly:"mo",quarterly:"qtr",annual:"yr"}[t.cadence]);row.querySelector("button").onclick=()=>startWizard("correct",p.id,t.id);});
    });
    if(!data.plans.length)setEmpty(el("plan-history"),'<p class="settings-helper">No configured plans. Adding one is optional.</p>');
    reconcileChildren(el("plan-tool-cards"),data.tools || [],t=>t,()=>nodeFrom('<button type="button"><span class="plan-logo" aria-hidden="true"></span><i class="tool-swatch"></i><span class="plan-tool-name"></span></button>'),(b,t)=>{b.dataset.tool=t;const swatch=b.querySelector("i");if(swatch)swatch.style.background=color(t);const slot=b.querySelector(".plan-logo");if(slot&&window.BurnrateMarks)slot.replaceChildren(window.BurnrateMarks.mark(t));setText(b.querySelector(".plan-tool-name"),displayTool(t));b.onclick=()=>{el("plan-tool").value=t;resetPreview();updateWizard();};});
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

  function bindPlanDrag(node){
    const handle = node.querySelector(".plan-dialog-header");
    if (!handle || handle.dataset.drag) return;
    handle.dataset.drag = "1";
    let drag = null;
    const clamp = (value, min, max) => Math.min(Math.max(value, min), max);
    const current = () => {
      const parts = String(node.style.translate || "").trim().split(/\s+/);
      return {x: parseFloat(parts[0]) || 0, y: parseFloat(parts[1]) || 0};
    };
    handle.addEventListener("pointerdown", event => {
      if (event.button != null && event.button !== 0) return;
      if (event.target?.closest?.("button, a, input, select, textarea, summary")) return;
      if (typeof node.getBoundingClientRect !== "function") return;
      const at = current();
      drag = {id: event.pointerId, x: event.clientX, y: event.clientY, ox: at.x, oy: at.y};
      handle.setPointerCapture?.(event.pointerId);
      node.classList?.add?.("is-dragging");
    });
    handle.addEventListener("pointermove", event => {
      if (!drag || event.pointerId !== drag.id) return;
      const w = node.offsetWidth || 0, h = node.offsetHeight || 0;
      const vw = window.innerWidth || 0, vh = window.innerHeight || 0;
      let x = drag.ox + event.clientX - drag.x, y = drag.oy + event.clientY - drag.y;
      if (w && h && vw && vh) {
        const minX = 16 - (vw - w) / 2, maxX = (vw - w) / 2 - 16;
        const minY = 16 - (vh - h) / 2, maxY = (vh - h) / 2 - 16;
        if (minX <= maxX) x = clamp(x, minX, maxX); else x = 0;
        if (minY <= maxY) y = clamp(y, minY, maxY); else y = 0;
      }
      node.style.translate = Math.round(x) + "px " + Math.round(y) + "px";
    });
    const end = event => {
      if (!drag || (event && event.pointerId != null && event.pointerId !== drag.id)) return;
      drag = null;
      node.classList?.remove?.("is-dragging");
    };
    handle.addEventListener("pointerup", end);
    handle.addEventListener("pointercancel", end);
  }
  bindPlanDrag(dialog);
  function openPlans(button=document.activeElement){opener=button;dialog.classList?.remove?.("is-dragging");dialog.style.translate="0px 0px";if(!dialog.open)dialog.showModal();showPlansValue();readPlans();}
  window.BurnratePlans={open:openPlans};
  ["nav-plans","manage-plans","setup-plans"].forEach(id=>el(id).addEventListener("click",event=>openPlans(event.currentTarget)));
  el("tab-plans-value")?.addEventListener("click",showPlansValue);
  el("tab-plans-list")?.addEventListener("click",showList);
  function invalidatePlanDialog(){++generation;controller?.abort();writeOwner=null;pending=false;el("save-plan").disabled=false;dialog.setAttribute("aria-busy","false");plansValueCtrl?.close();}
  el("close-plans").addEventListener("click",()=>{invalidatePlanDialog();dialog.close();});
  dialog.addEventListener("cancel",invalidatePlanDialog);
  dialog.addEventListener("close",()=>{if(dialog.open)return;invalidatePlanDialog();restoreOpener(opener,"nav-plans");});
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
    const required=step===1 ? ["plan-name"] : step===2 ? ["plan-amount",...(op==="correct"?["plan-name"]:[])] : op==="end" ? [] : ["plan-start",...(op==="schedule"?[]:["plan-end"])];
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
    const owned = ++generation, owner = {};
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
