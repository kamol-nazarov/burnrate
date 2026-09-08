/* Independent wizard request ownership; also loadable by Node unit tests. */
(() => {
  class ConnectionFlow {
    constructor(uuid) { this.uuid = uuid; this.epoch = 0; this.ids = new Map(); this.stage = "choose"; }
    move(stage) { this.stage = stage; return ++this.epoch; }
    owns(epoch) { return epoch === this.epoch; }
    request(body) {
      const key = JSON.stringify(body);
      if (!this.ids.has(key)) this.ids.set(key, this.uuid());
      return {...body, requestId: this.ids.get(key)};
    }
    saved(body) { this.ids.delete(JSON.stringify(body)); }
    editedCredential(source) { for (const key of this.ids.keys()) if (JSON.parse(key).source === source) this.ids.delete(key); }
    selectCandidate(items, current) { if (current.check.checked) items.filter(item => item.row.source === current.row.source && item !== current).forEach(item => { item.check.checked = false; }); }
  }
  if (typeof module !== "undefined") module.exports = {ConnectionFlow};
  if (typeof document === "undefined") return;
  const flow = new ConnectionFlow(() => crypto.randomUUID());
  const dialog = document.createElement("dialog");
  dialog.id = "connection-wizard";
  dialog.setAttribute("aria-labelledby", "connection-title");
  dialog.innerHTML = '<header><h2 id="connection-title">Connect harness</h2><button type="button" data-close>Close</button></header><p>Locations are on the computer running BURNRATE, not this browser’s computer.</p><p>Connections read usage. They do not route inference, purchase subscriptions or log in to another app.</p><nav aria-label="Connection steps"><button type="button" data-back>Back</button><span data-stage></span></nav><p role="status" aria-live="polite" data-message></p><section data-content></section><details><summary>BURNRATE access token</summary><label>Access token <input type="password" autocomplete="off" data-token></label><p>Only needed when this BURNRATE server requires authentication. Kept in memory until this dialog closes.</p></details>';
  document.body.append(dialog);
  const content = dialog.querySelector("[data-content]");
  const message = dialog.querySelector("[data-message]");
  let opener, controller, rows = [], busy = false;
  const draft = {source: "codex_local", location: ""};
  const text = value => { message.textContent = value; };
  const button = (label, action, parent = content) => {
    const node = document.createElement("button"); node.type = "button"; node.textContent = label;
    node.addEventListener("click", action); parent.append(node); return node;
  };
  const para = (value, parent = content) => { const p = document.createElement("p"); p.textContent = value; parent.append(p); return p; };
  function clearSecret() { content.querySelectorAll('input[type="password"]').forEach(input => { input.value = ""; }); }
  function stage(name) {
    flow.move(name); controller?.abort(); clearSecret(); content.replaceChildren(); busy = false; text("");
    dialog.querySelector("[data-stage]").textContent = name;
    dialog.querySelector("[data-back]").hidden = name === "choose";
    const heading = dialog.querySelector("h2"); heading.tabIndex = -1; heading.focus();
  }
  async function request(path, body) {
    controller?.abort(); controller = new AbortController();
    const owner = flow.epoch;
    const headers = {"X-BURNRATE-Request": "1"};
    const token = dialog.querySelector("[data-token]").value;
    if (token) headers.Authorization = "Bearer " + token;
    if (body) headers["Content-Type"] = "application/json";
    const response = await fetch(path, {method: body ? "POST" : "GET", headers, cache: "no-store", signal: controller.signal, ...(body ? {body: JSON.stringify(body)} : {})});
    const payload = await response.json();
    if (!flow.owns(owner)) throw new DOMException("Obsolete request", "AbortError");
    if (!response.ok) throw new Error(response.status === 401 ? "Authentication required. Enter your BURNRATE access token below and retry." : payload.error || "Connection request failed.");
    return payload;
  }
  async function attempt(action) {
    if (busy) return;
    busy = true; const owner = flow.epoch;
    try { await action(); } catch (error) { if (flow.owns(owner) && error.name !== "AbortError") text(error.message); }
    finally { if (flow.owns(owner)) busy = false; }
  }
  async function load() { const data = await request("/api/connections"); rows = data.connections; return data; }
  function choose() {
    stage("choose");
    button("Auto-detect (recommended)", discover);
    button("Choose manually", () => manual(false));
    button("Add provider API", () => manual(true));
    button("Connection status", statuses);
    button("Manage subscriptions", () => { dialog.close(); document.getElementById("manage-plans").click(); });
  }
  function details(row, parent) {
    para(row.name + (row.experimental ? " · Experimental" : ""), parent);
    para(row.capabilities.join(", ") + " · " + row.state.replaceAll("_", " "), parent);
    if (row.location) para(row.location, parent);
    para(row.detail || "", parent);
    if (row.kind !== "api") para(row.capabilityNote, parent);
  }
  async function statuses() {
    stage("Connection status");
    button("Reload status", statuses);
    await attempt(async () => {
      await load();
      for (const row of rows) {
        const card = document.createElement("article"); content.append(card); details(row, card);
        para("Last verification: " + (row.lastVerification || "not verified") + " · Last successful import: " + (row.lastImport || "none for this binding") + " · Binding revision: " + row.revision + " · Imported revision: " + (row.importRevision ?? "none"), card);
        if (row.externallyManaged) continue;
        button(row.kind === "api" ? "Replace credential" : "Change location", () => { draft.source = row.source; draft.location = row.location; manual(row.kind === "api"); }, card);
        if (row.kind !== "api" && row.revision > 0) button("Recheck", () => attempt(async () => { await save(row, "recheck", row.location); await statuses(); }), card);
        button(row.enabled === false ? "Reconnect" : "Disable", () => {
          if (row.kind === "api" && row.enabled === false) { draft.source = row.source; manual(true); return; }
          attempt(async () => { await save(row, row.enabled === false ? "reconnect" : "disable", row.location); await statuses(); });
        }, card);
      }
    });
  }
  async function save(row, operation, location, secret, mode = "manual") {
    const body = {source: row.source, operation, location, mode, revision: row.revision};
    const response = await request("/api/connections", {...flow.request(body), ...(secret ? {secret} : {})});
    flow.saved(body);
    return response;
  }
  async function discover() {
    stage("Auto-detect → Verify → Connect selected");
    button("Rescan", discover);
    await attempt(async () => {
      const data = await request("/api/connections/discover", {});
      const selected = [];
      for (const row of data.connections) {
        const card = document.createElement("article"); content.append(card); details(row, card);
        for (const candidate of row.candidates) {
          const label = document.createElement("label"); const check = document.createElement("input"); check.type = "checkbox";
          check.disabled = candidate.state !== "readable" || row.externallyManaged;
          label.append(check, document.createTextNode(candidate.location + " · " + candidate.detail)); card.append(label);
          const result = para("", card);
          const item = {row, candidate, check, result};
          check.addEventListener("change", () => flow.selectCandidate(selected, item));
          selected.push(item);
        }
      }
      button("Connect selected", () => attempt(async () => {
        const owner = flow.epoch;
        for (const item of selected.filter(item => item.check.checked && !item.check.disabled)) {
          if (!flow.owns(owner)) return;
          try {
            const response = await save(item.row, "connect", item.candidate.location, null, "auto");
            item.row.revision = response.connection.revision;
            item.result.textContent = "Saved, awaiting first ingest."; item.check.disabled = true; item.check.checked = false;
          } catch (error) {
            if (!flow.owns(owner)) return;
            item.result.textContent = error.message + " Retry Connect selected for this item.";
          }
        }
      }));
      button("View connection status", statuses);
    });
  }
  async function manual(api) {
    stage(api ? "Provider API → Test and connect" : "Choose manually → Verify");
    button("Reload choices", () => manual(api));
    await attempt(async () => {
      const data = await load();
      const choices = rows.filter(row => (row.kind === "api") === api);
      const form = document.createElement("form"); content.append(form);
      const label = document.createElement("label"); label.textContent = api ? "Provider " : "Harness ";
      const select = document.createElement("select"); label.append(select); form.append(label);
      for (const row of choices) { const option = document.createElement("option"); option.value = row.source; option.textContent = row.name; select.append(option); }
      if (choices.some(row => row.source === draft.source)) select.value = draft.source;
      const help = para("", form);
      const inputLabel = document.createElement("label"); inputLabel.textContent = api ? "Provider key " : "Local folder or source file ";
      const input = document.createElement("input"); input.type = api ? "password" : "text"; input.autocomplete = "off"; input.required = true; inputLabel.append(input); form.append(inputLabel);
      const verify = document.createElement("button"); verify.type = "submit"; verify.textContent = api ? "Test and connect" : "Verify"; form.append(verify);
      const result = para("", form); let checked = null;
      const connect = button("Connect", () => attempt(async () => {
        if (!checked) return;
        const row = choices.find(row => row.source === select.value);
        await save(row, "connect", checked.location); await statuses();
      }), form); connect.hidden = true;
      function edit() {
        flow.move(flow.stage); controller?.abort(); busy = false; checked = null; connect.hidden = true; result.textContent = "";
        draft.source = select.value; if (!api) draft.location = input.value;
      }
      function selection() {
        const row = choices.find(row => row.source === select.value);
        input.value = api ? "" : (draft.source === row.source && draft.location ? draft.location : row.location);
        help.textContent = (api ? row.credentialHelp : "Example: " + row.location + ". A harness home is normalized to its sessions folder or database file.") + (row.externallyManaged ? " This connection is externally managed; change the explicit external setting first." : "");
        verify.disabled = row.externallyManaged || (api && !data.vaultAvailable);
        if (api && !data.vaultAvailable) help.textContent += " Secure Windows credential storage is unavailable; local setup still works.";
        edit();
      }
      select.addEventListener("change", selection); input.addEventListener("input", () => { if (api) flow.editedCredential(select.value); edit(); }); selection();
      form.addEventListener("submit", event => {
        event.preventDefault();
        attempt(async () => {
          const row = choices.find(row => row.source === select.value);
          if (api) { await save(row, "connect", "", input.value, "api"); input.value = ""; await statuses(); }
          else { checked = await request("/api/connections/verify", {source: row.source, location: input.value}); result.textContent = checked.detail; connect.hidden = false; }
        });
      });
    });
  }
  dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());
  dialog.querySelector("[data-back]").addEventListener("click", choose);
  dialog.addEventListener("close", () => { flow.move("closed"); controller?.abort(); busy = false; clearSecret(); dialog.querySelector("[data-token]").value = ""; opener?.focus(); });
  window.BurnrateConnections = {open: async () => {
    opener = document.activeElement; if (!dialog.open) dialog.showModal(); choose();
    await attempt(async () => { await load(); });
  }};
})();
