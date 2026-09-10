// Exercise production functions, event handlers, renderers, CSS and real DOM.
(async () => {
  const assert = (ok, message) => { if (!ok) throw new Error(message); };
  assert(raceStartupErrors.length === 0, "Startup errors: " + raceStartupErrors.join("; "));
  assert(typeof state !== "undefined", "Dashboard state missing after deferred scripts");
  const flush = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };
  const requests = path => raceRequests.filter(r => new URL(r.url, location.href).pathname.endsWith(path));
  const last = path => requests(path).at(-1);
  const settle = async (request, fail = false) => {
    assert(request && !request.settled, "request must exist and settle only once");
    request.settled = true;
    if (fail) request.reject(new Error("controlled failure"));
    else {
      const url = new URL(request.url, location.href);
      let data;
      if (url.pathname.endsWith("summary")) data = raceSummary(url.searchParams.get("window"));
      else if (url.pathname.endsWith("entity")) {
        data = structuredClone(RACE_FIXTURES.entity);
        data.window = raceSummary(url.searchParams.get("window")).window;
        data.kind = url.searchParams.get("kind");
        data.name = url.searchParams.get("key");
      } else data = structuredClone(RACE_FIXTURES.health);
      request.resolve({ok:true, json:async () => data});
    }
    await flush();
  };
  const click = id => document.getElementById(id).click();
  const select = key => document.querySelector(`#${state.view === "detail" ? "detail-ranges" : "range-switch"} [data-window="${key}"]`).click();
  const tick = () => raceTicks.forEach(callback => callback());
  const busy = () => document.getElementById("main").getAttribute("aria-busy") === "true";
  const errorVisible = () => !document.getElementById("error-banner").hidden;
  const visible = id => {
    const node = document.getElementById(id);
    return !!node.getClientRects().length && getComputedStyle(node).visibility !== "hidden"
      && !node.closest("[hidden], [inert]");
  };
  const overview = key => {
    assert(state.view === "overview" && !document.getElementById("overview-view").hidden, "overview must remain selected");
    assert(state.window === key && state.summary?.window.key === key, "overview data must match requested range");
    assert(document.querySelector('#range-switch [aria-checked="true"]').dataset.window === key, "selected range mismatch");
    assert(document.getElementById("range-label").textContent.startsWith(key + " window"), "rendered range mismatch");
    assert(visible("kpi-grid"), "matching KPIs must be visible");
  };
  const seed = async () => {
    await settle(last("summary"));
    await settle(last("health"));
    overview("1d");
  };
  const fail = RACE_CASE.endsWith("_fail");
  if (RACE_CASE.startsWith("snapshot_")) {
    await flush();
    const valid = RACE_CASE === "snapshot_valid";
    assert(Boolean(state.summary) === valid, "snapshot validity rejected or accepted incorrectly");
    if (valid) {
      assert(state.summary.snapshot && state.summary.status === "stale", "snapshot must be labeled stale");
      assert(document.getElementById("diagnostics-button").dataset.state === "stale", "snapshot navbar must be stale");
    }
    assert(requests("summary").length === 1, "matching startup prefetch must be used once");
    await settle(last("summary"));
    overview("1d");
  } else if (RACE_CASE.startsWith("health_")) {
    await settle(last("summary"));
    overview("1d");
    assert(!busy(), "slow health must not block summary or its loading cleanup");
    if (RACE_CASE === "health_across_ticks") {
      const slow = last("health");
      const count = requests("health").length;
      tick(); await settle(last("summary"));
      tick(); await settle(last("summary"));
      assert(requests("health").length === count, "slow auxiliary health must not overlap");
      await settle(slow);
      assert(state.health != null, "slow health must survive same-view summary generations");
      overview("1d");
    } else if (fail) await settle(last("health"), true);
    else {
      loadEntity("model", "model-a");
      await settle(last("entity"));
      await settle(requests("health")[0]);
      assert(state.view === "detail", "late health cannot change the view");
    }
  } else {
    await seed();
    if (RACE_CASE === "standalone_startup") {
      overview("1d");
    } else if (RACE_CASE.startsWith("resume_")) {
      tick();
      const old = last("summary");
      if (RACE_CASE.includes("detail")) { loadEntity("model", "model-a"); await settle(last("entity")); }
      else { click("diagnostics-button"); await settle(last("health")); }
      await settle(old, fail);
      click("home-button");
      const count = requests("summary").length;
      tick();
      assert(requests("summary").length > count, "automatic overview refresh must recover after navigation");
      await settle(last("summary"));
      overview("1d");
    } else if (RACE_CASE.startsWith("ownership")) {
      tick();
      const a = last("summary");
      select("1w");
      const b = last("summary");
      await settle(a, fail);
      assert(b !== a && busy(), "older cleanup must not clear newer loading");
      const count = requests("summary").length;
      tick(); tick();
      assert(requests("summary").length === count, "poll cannot supersede B");
      await settle(b); overview("1w"); assert(!busy(), "latest request must release loading");
    } else if (RACE_CASE === "ranges") {
      select("1w"); const a = last("summary");
      select("1mo"); const b = last("summary");
      select("15m"); const c = last("summary");
      await settle(b, true); assert(!errorVisible(), "obsolete range error must stay hidden");
      await settle(a); assert(busy(), "obsolete success cannot clear latest loading");
      await settle(c); overview("15m");
      assert(JSON.parse(localStorage.getItem("burnrate:snapshot:v1:15m")).summary.window.key === "15m", "snapshot stored under wrong range");
    } else if (RACE_CASE === "range_failure") {
      select("1w"); await settle(last("summary"), true);
      assert(errorVisible() && !busy(), "selected range failure must be recoverable");
      assert(!visible("kpi-grid"), "failed 1w must not expose old 1d KPIs");
      assert(!visible("chart-table-body"), "failed range must not expose previous chart table");
      click("retry-button"); await settle(last("summary")); overview("1w");
      assert(!errorVisible(), "retry must clear selected-range error");
    } else if (RACE_CASE.startsWith("detail_back")) {
      loadEntity("model", "model-a"); await settle(last("entity"));
      select("1w"); await settle(last("entity"));
      click(RACE_CASE.endsWith("home") ? "home-button" : "detail-back");
      assert(!visible("kpi-grid") || state.summary.window.key === "1w", "Back must not expose cached 1d as 1w");
      await settle(last("summary")); overview("1w");
    } else if (RACE_CASE.startsWith("late_")) {
      const detail = RACE_CASE.includes("detail");
      if (detail) loadEntity("model", "model-a"); else click("diagnostics-button");
      const old = last(detail ? "entity" : "health");
      const intendedImmediately = state.view === (detail ? "detail" : "diagnostics");
      click(detail ? "detail-back" : "diagnostics-back");
      overview("1d");
      await settle(old, fail);
      overview("1d");
      assert(!busy() && !errorVisible(), "obsolete request cannot change busy/error state");
      assert(intendedImmediately, "navigation intent must be immediate");
    } else if (RACE_CASE.startsWith("poll_")) {
      const detail = RACE_CASE.includes("detail");
      const diagnostics = RACE_CASE.includes("diagnostics");
      if (detail) { loadEntity("model", "model-a"); await settle(last("entity")); }
      if (diagnostics) { click("diagnostics-button"); await settle(last("health")); }
      const path = detail ? "entity" : diagnostics ? "health" : "summary";
      tick(); const slow = last(path); const count = requests(path).length;
      for (let i = 0; i < 10; i++) tick();
      assert(requests(path).length === count, "timer must not overlap or starve slow request");
      await settle(slow, true);
      assert(errorVisible(), "same-range failed refresh must show failure");
      tick(); assert(requests(path).length === count + 1, "poll must recover after failure");
      await settle(last(path)); assert(!errorVisible(), "successful refresh clears error");
    } else if (RACE_CASE === "cache_reuse") {
      const count = requests("summary").length;
      loadEntity("model", "model-a"); await settle(last("entity"));
      click("detail-back");
      overview("1d");
      assert(requests("summary").length === count, "matching overview cache must be reused");
      assert(state.summary.status === "stale", "cached return must be labeled stale");
      loadEntity("model", "model-a");
      assert(visible("detail-kpis"), "matching entity cache should stay visible during refresh");
      const old = last("entity");
      loadEntity("tool", "tool-b");
      assert(!visible("detail-kpis"), "different entity kind/key must invalidate cached detail");
      await settle(old);
      assert(busy(), "old entity cleanup must not clear new loading");
      await settle(last("entity"));
    } else if (RACE_CASE === "mismatched_response") {
      select("1w");
      const r = last("summary");
      r.settled = true;
      r.resolve({ok:true, json:async () => raceSummary("1d")});
      await flush();
      assert(errorVisible() && !visible("kpi-grid"), "wrong server range must remain unavailable");
      assert(!localStorage.getItem("burnrate:snapshot:v1:1w"), "wrong server range must never be stored");
      click("retry-button"); await settle(last("summary")); overview("1w");
    } else if (RACE_CASE === "abort_transport") {
      select("1w"); const old = last("summary");
      select("1mo"); const latest = last("summary");
      assert(old.options.signal?.aborted === true, "superseded transport must be aborted");
      assert(latest.options.signal?.aborted === false, "new transport must remain usable");
      old.settled = true;
      old.reject(new DOMException("Cancelled", "AbortError"));
      await flush();
      assert(busy() && !errorVisible(), "cancelled transport cannot release new loading or show an error");
      await settle(latest); overview("1mo");
    } else if (RACE_CASE === "entity_identity") {
      loadEntity("model", "model-a"); const a = last("entity");
      loadEntity("tool", "tool-b"); const b = last("entity");
      await settle(a, true); assert(!errorVisible(), "obsolete entity failure must stay hidden");
      await settle(b);
      assert(document.getElementById("detail-name").textContent === "tool-b", "latest entity identity must win");
    } else if (RACE_CASE === "cancel") {
      select("1w");
      const old = last("summary");
      click("home-button"); // Supersedes the request, but retains the selected range.
      old.settled = true; old.reject(new DOMException("Cancelled", "AbortError")); await flush();
      assert(!errorVisible(), "intentional cancellation must be silent");
      await settle(last("summary")); overview("1w");
    } else throw new Error("unknown case");
  }
  await flush();
  assert(raceUnhandled.length === 0, "unhandled promise rejection: " + raceUnhandled);
  document.getElementById("race-result").textContent = JSON.stringify({pass:true, case:RACE_CASE});
})().catch(error => {
  document.getElementById("race-result").textContent = JSON.stringify({pass:false, case:RACE_CASE, error:error.stack});
});
