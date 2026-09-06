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
  el("manage-plans").click();
  await until(()=>el("plan-message").textContent.includes("History loaded"));
  const phase=localStorage.getItem("product-test-phase");
  if (!phase) {
    set("plan-name","<img src=x> Example"); set("plan-amount","-1");set("plan-start","2026-09-01");
    assert(!el("plan-amount").checkValidity(),"negative amount accepted by browser form");
    set("plan-amount","100");
    submit(); submit();
    await until(()=>el("plan-message").textContent.includes("Your input is preserved"));
    assert(el("plan-name").value==="<img src=x> Example","failed write erased input");
    submit(); await saved();
    assert(el("plan-history").querySelectorAll("section").length===1,"retry created a duplicate");
    assert(!el("plan-history").querySelector("img"),"plan name interpreted as markup");
    localStorage.setItem("product-test-phase","added");
    location.reload(); return;
  }
  assert(el("plan-history").textContent.includes("$100.00/month"),"added plan did not survive reload");
  change("plan-action","schedule");
  set("plan-amount","150");set("plan-start","2026-09-15");
  // A concurrent editor advances the version after this editor loaded it.
  const latest=await (await fetch("/api/subscriptions")).json();
  const p=latest.plans[0];
  await fetch("/api/subscriptions",{method:"POST",headers:{"Content-Type":"application/json","X-BURNRATE-Request":"1"},
    body:JSON.stringify({operation:"end",request_id:"concurrent-end-123",plan_id:p.id,expected_version:p.version,end_date:null})});
  submit(); await until(()=>el("plan-message").textContent.includes("Conflict"));
  assert(el("plan-amount").value==="150","conflict erased draft");
  el("reload-plans").click();await until(()=>el("plan-message").textContent.includes("History loaded"));
  submit();await saved();
  assert(el("plan-history").textContent.includes("2026-09-14"),"prior term history lost");
  assert(el("plan-history").textContent.includes("$150.00/month"),"scheduled term missing");
  change("plan-action","correct");change("term-choice",el("term-choice").options[0].value);
  set("plan-amount","90");submit();
  await until(()=>!el("plan-preview").hidden);
  assert(el("plan-preview").textContent.includes("Before $"),"impact preview missing");
  el("plan-confirm").checked=true;submit();await saved();
  change("plan-action","end");set("plan-end","2026-09-20");submit();await saved();
  assert(el("plan-history").textContent.includes("Plan last active: 2026-09-20"),"end date missing");
  assert(el("plan-manager").textContent.includes("does not cancel"),"provider cancellation disclosure missing");
  assert(el("plan-manager").scrollWidth<=el("plan-manager").clientWidth+1,"dialog horizontally overflows");
  el("close-plans").click();await until(()=>!el("plan-manager").open);
  await until(()=>document.activeElement.id==="manage-plans");
  el("product-result").textContent=JSON.stringify({pass:true});
})().catch(error=>{document.getElementById("product-result").textContent=JSON.stringify({pass:false,error:error.stack});});
