(async () => {
  const el = id => document.getElementById(id);
  const assert = (value, why) => { if (!value) throw new Error(why); };
  const until = async check => {
    for (let i=0;i<400;i++) { if (check()) return; await new Promise(r=>setTimeout(r,25)); }
    throw new Error("Condition did not settle: " + check);
  };
  const set = (id, value) => { el(id).value=value; el(id).dispatchEvent(new Event("input",{bubbles:true})); };
  const change = (id,value) => { el(id).value=value;el(id).dispatchEvent(new Event("change",{bubbles:true})); };
  const submit = () => el("plan-form").requestSubmit();
  const saved = () => until(()=>el("plan-message").textContent.startsWith("Saved durably"));
  await until(()=>!document.body.classList.contains("loading"));
  document.querySelector("#chart-hit-targets button")?.click();
  el("manage-plans").click();el("tab-plans-list").click();
  await until(()=>el("plan-message").textContent.includes("History loaded"));
  const phase=localStorage.getItem("product-test-phase");
  if (!phase) {
    el("new-plan").click();
  el("plan-name").focus();
  const arrow = new KeyboardEvent("keydown", {key:"ArrowLeft", bubbles:true, cancelable:true});
  el("plan-name").dispatchEvent(arrow);
  assert(!arrow.defaultPrevented, "pinned chart consumed a form arrow key");

    set("plan-name","<img src=x> Example"); submit();
    set("plan-amount","-1");set("plan-start","2026-09-01");
    assert(!el("plan-amount").checkValidity(),"negative amount accepted by browser form");
    set("plan-amount","100"); submit();
    submit(); submit();
    await until(()=>el("plan-message").textContent.includes("Your input is preserved"));
    assert(el("plan-name").value==="<img src=x> Example","failed write erased input");
    submit(); await saved();
    assert(el("plan-history").querySelectorAll(".plan-list-row").length===1,"retry created a duplicate");
    assert(!el("plan-history").querySelector("img"),"plan name interpreted as markup");
    localStorage.setItem("product-test-phase","added");
    location.reload(); return;
  }
  const row = () => el("plan-history").querySelector(".plan-list-row");
  const actions = () => { if(row().querySelector(".plan-row-actions").hidden)row().querySelector(".plan-row-main button").click();return row().querySelector(".plan-row-actions"); };
  const preset = operation => actions().querySelector(`[data-plan-op="${operation}"]`).click();
  const history = () => { const strip=actions();if(row().querySelector(".term-history").hidden)strip.querySelector("[data-history]").click(); };
  assert(el("plan-history").textContent.includes("$100.00/mo"),"added plan did not survive reload");
  preset("schedule");
  set("plan-amount","150");submit();set("plan-start","2026-09-15");
  // A concurrent editor advances the version after this editor loaded it.
  const latest=await (await fetch("/api/subscriptions")).json();
  const p=latest.plans[0];
  await fetch("/api/subscriptions",{method:"POST",headers:{"Content-Type":"application/json","X-BURNRATE-Request":"1"},
    body:JSON.stringify({operation:"end",request_id:"concurrent-end-123",plan_id:p.id,expected_version:p.version,end_date:null})});
  submit(); await until(()=>el("plan-message").textContent.includes("Conflict"));
  assert(el("plan-amount").value==="150","conflict erased draft");
  el("close-plans").click();el("manage-plans").click();el("tab-plans-list").click();await until(()=>el("plan-message").textContent.includes("History loaded") && el("plan-manager").getAttribute("aria-busy")!=="true");
  preset("schedule");set("plan-amount","150");submit();set("plan-start","2026-09-15");submit();await saved();
  history();
  assert(el("plan-history").textContent.includes("2026-09-14"),"prior term history lost");
  assert(el("plan-history").textContent.includes("$150.00/mo"),"scheduled term missing");
  preset("correct");change("term-choice",el("term-choice").options[0].value);
  set("plan-amount","90");submit();submit();
  await until(()=>!el("plan-preview").hidden);
  assert(el("plan-before-value").textContent.startsWith("$") && el("plan-after-value").textContent.startsWith("$") && el("plan-affected").textContent.includes("days affected"),"impact preview missing");
  el("plan-confirm").checked=true;submit();await saved();
  preset("end");set("plan-end","2026-09-20");submit();await saved();
  assert(el("plan-history").textContent.includes("→ 2026-09-20"),"end date missing");
  assert(el("plan-manager").textContent.includes("does not cancel"),"provider cancellation disclosure missing");
  assert(el("plan-manager").scrollWidth<=el("plan-manager").clientWidth+1,"dialog horizontally overflows");
  el("close-plans").click();await until(()=>!el("plan-manager").open);
  await until(()=>document.activeElement.id==="manage-plans");
  el("product-result").textContent=JSON.stringify({pass:true});
})().catch(error=>{document.getElementById("product-result").textContent=JSON.stringify({pass:false,error:error.stack});});
