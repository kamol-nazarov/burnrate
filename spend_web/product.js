// Optional product dialogs own their requests independently of dashboard polling.
(() => {
  const el = id => document.getElementById(id);
  const setup = el("setup-guidance");
  const preference = "burnrate:setup:v1";
  let setupData = null;
  const showSetup = () => { setup.hidden = false; el("setup-done").focus(); };
  const closeSetup = status => {
    try { localStorage.setItem(preference, status); } catch {}
    setup.hidden = true; el("setup-help").focus();
  };
  el("setup-help").addEventListener("click", showSetup);
  el("setup-dismiss").addEventListener("click", () => closeSetup("dismissed"));
  el("setup-done").addEventListener("click", () => closeSetup("complete"));
  el("setup-plans").addEventListener("click", () => el("manage-plans").click());
  fetch("/api/onboarding", {cache:"no-store"}).then(r => {
    if (!r.ok) throw new Error("Setup metadata unavailable");
    return r.json();
  }).then(data => {
    setupData = data;
    el("setup-timezone").textContent = "Timezone: " + data.timezone + ". Configure " + data.timezoneSetting + ".";
    let saved;
    try { saved = localStorage.getItem(preference); } catch {}
    if (!data.hasHistory && !saved) setup.hidden = false;
  }).catch(() => {
    el("setup-timezone").textContent = "Setup metadata is unavailable. The dashboard remains usable; check Diagnostics or reload to retry.";
  });
  window.renderSourceGuidance = payload => {
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
  const dialog = el("plan-manager"), form = el("plan-form");
  let data = {plans:[]}, generation = 0, controller, pending = false, preview = null;
  let retryKey = "", requestId = "", opener, writeOwner = null;
  const plan = () => data.plans.find(p => String(p.id) === el("plan-choice").value);
  const term = () => plan()?.terms.find(t => String(t.id) === el("term-choice").value);
  const message = text => { el("plan-message").textContent = text; };
  function resetPreview() {
    preview = null; el("plan-preview").hidden = true;
    el("plan-confirm-label").hidden = true; el("plan-confirm").checked = false;
  }
  function fields(fill = false) {
    const op = el("plan-action").value, p = plan();
    const chosen = el("term-choice").value;
    el("term-choice").replaceChildren(...(p?.terms || []).map(t => new Option(t.start_date + " · " + t.cadence, t.id)));
    if ([...el("term-choice").options].some(o => o.value === chosen)) el("term-choice").value = chosen;
    const t = op === "correct" ? term() : p?.terms.at(-1);
    const shown = {
      "plan-choice-label": op !== "add", "term-choice-label": op === "correct",
      "plan-name-label": op === "add" || op === "correct", "plan-tool-label": op === "add" || op === "correct",
      "plan-amount-label": op !== "end", "plan-cadence-label": op !== "end",
      "plan-start-label": op !== "end", "plan-end-label": op !== "schedule"
    };
    Object.entries(shown).forEach(([id, show]) => { el(id).hidden = !show; });
    ["plan-name", "plan-amount", "plan-start"].forEach(id => { el(id).required = !el(id + "-label").hidden; });
    if (fill && t && op !== "add") {
      el("plan-name").value = t.name; el("plan-tool").value = t.tool_key;
      el("plan-amount").value = t.amount_usd; el("plan-cadence").value = t.cadence;
      el("plan-start").value = op === "schedule" ? data.today : t.start_date;
      el("plan-end").value = op === "end" ? p.end_date || "" : t.end_date || "";
    }
    el("save-plan").textContent = op === "correct" ? "Preview correction" : "Save plan";
    resetPreview();
  }
  async function readPlans(preserve = true) {
    const owned = ++generation;
    controller?.abort(); controller = new AbortController();
    dialog.setAttribute("aria-busy", "true");
    try {
      const response = await fetch("/api/subscriptions", {cache:"no-store", signal:controller.signal});
      if (!response.ok) throw new Error("Could not load subscriptions");
      const incoming = await response.json();
      if (owned !== generation || !dialog.open) return;
      const choice = el("plan-choice").value, tool = el("plan-tool").value;
      data = incoming;
      el("plan-choice").replaceChildren(...data.plans.map(p => new Option(p.terms.at(-1).name + " · #" + p.id, p.id)));
      if ([...el("plan-choice").options].some(o => o.value === choice)) el("plan-choice").value = choice;
      el("plan-tool").replaceChildren(...data.tools.map(t => new Option(t === "opencode" ? "Z.AI shared plan (OpenCode / ZCode)" : t, t)));
      if (tool) el("plan-tool").value = tool;
      el("plan-timezone").textContent = "Dates use " + data.timezone + ". Monthly equivalents are references; selected-window cost is prorated separately.";
      const history = el("plan-history"); history.replaceChildren();
      for (const p of data.plans) {
        const group = document.createElement("section");
        const title = document.createElement("h3"); title.textContent = p.terms.at(-1).name + " · #" + p.id;
        group.append(title);
        for (const t of p.terms) {
          const row = document.createElement("p");
          row.textContent = t.status + " · " + t.start_date + " through " + (t.end_date || "ongoing")
            + " · $" + Number(t.amount_usd).toFixed(2) + "/" + {monthly:"month",quarterly:"quarter",annual:"year"}[t.cadence]
            + " · $" + Number(t.monthly_equivalent).toFixed(2) + "/month equivalent";
          group.append(row);
        }
        if (p.end_date) { const ended = document.createElement("p"); ended.textContent = "Plan last active: " + p.end_date; group.append(ended); }
        history.append(group);
      }
      if (!data.plans.length) history.textContent = "No configured plans. Adding one is optional.";
      if (!preserve) fields(true);
      message("History loaded. Unsaved fields are preserved.");
    } catch (error) {
      if (owned === generation && error.name !== "AbortError") message(error.message + ". Reload history to retry.");
    } finally {
      if (owned === generation) dialog.setAttribute("aria-busy", "false");
    }
  }
  el("manage-plans").addEventListener("click", () => {
    opener = el("manage-plans"); opener.focus(); dialog.showModal(); fields(); readPlans();
  });
  el("close-plans").addEventListener("click", () => { dialog.close(); opener?.focus(); });
  dialog.addEventListener("close", () => { ++generation; controller?.abort(); writeOwner = null; pending = false; el("save-plan").disabled = false; opener?.focus(); });
  el("reload-plans").addEventListener("click", () => { if (!pending) { resetPreview(); readPlans(); } });
  el("new-plan").addEventListener("click", () => {
    if (pending) return;
    form.reset(); el("plan-start").value = data.today || ""; retryKey = ""; requestId = ""; fields(); el("plan-name").focus();
  });
  ["plan-action", "plan-choice", "term-choice"].forEach(id => el(id).addEventListener("change", () => fields(true)));
  form.addEventListener("input", event => { if (event.target.id !== "plan-confirm") resetPreview(); });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (pending) return;
    const op = el("plan-action").value, p = plan(), t = term();
    if (op !== "add" && !p) { message("Select a plan first."); return; }
    const values = {name:el("plan-name").value, tool_key:el("plan-tool").value,
      amount_usd:el("plan-amount").value, cadence:el("plan-cadence").value,
      start_date:el("plan-start").value, end_date:el("plan-end").value || null};
    const body = {operation:op};
    if (op !== "add") Object.assign(body, {plan_id:p.id, expected_version:p.version});
    if (op === "schedule") Object.assign(body, {effective_date:values.start_date, values:{amount_usd:values.amount_usd,cadence:values.cadence}});
    else if (op === "end") body.end_date = values.end_date;
    else body.values = values;
    if (op === "correct") {
      body.term_id = t?.id;
      if (preview && el("plan-confirm").checked) Object.assign(body, {confirmed:true,preview_token:preview.preview_token});
      else body.preview = true;
    }
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
        el("plan-preview").textContent = result.from + " through " + result.through + "\nBefore $" + Number(result.before_usd).toFixed(2) + " → After $" + Number(result.after_usd).toFixed(2) + "\n" + result.note;
        el("save-plan").textContent = "Confirm correction"; message("Review the affected period and check confirmation.");
        el("plan-confirm").focus();
      } else {
        resetPreview();
        await readPlans(); message("Saved durably. Dashboard costs update on the next refresh.");
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
