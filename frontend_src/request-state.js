// Request slots own transport lifetimes; state.request owns rendering/navigation.
// A completed old transport can only release its own slot.
const pendingViews = {overview:null, detail:null, diagnostics:null};
let overviewHealth = null;
let navigationEpoch = 0;
function entityIdentity(kind, key, windowKey) {
  return JSON.stringify([kind, key, windowKey]);
}
function summaryMatches() {
  return state.summary?.window?.key === state.window;
}
function entityMatches() {
  return Boolean(state.entity && state.entityData
    && state.entityData.window?.key === state.window
    && state.entityDataIdentity === entityIdentity(state.entity.kind, state.entity.key, state.window));
}
function activeDataMatches() {
  return state.view === "overview" ? summaryMatches()
    : state.view === "detail" ? entityMatches() : Boolean(state.health);
}
function updateDataValidity() {
  for (const view of ["overview", "detail"]) {
    const root = $(view + "-view");
    const valid = view === "overview" ? summaryMatches() : entityMatches();
    root.classList.toggle("data-unavailable", !valid);
    for (const child of root.children) {
      if (child.matches(".window-row, .page-heading, .back-button, .overview-h1, .range-status")) continue;
      child.inert = !valid;
      if (!valid) child.setAttribute("aria-hidden", "true");
      else child.removeAttribute("aria-hidden");
    }
    const status = $(view + "-range-status");
    status.hidden = valid;
    status.textContent = pendingViews[view]
      ? "Loading " + windowLabel(state.window) + " data…"
      : "Data unavailable for " + windowLabel(state.window) + ". Use Retry to load this range.";
  }
  if (state.view !== "diagnostics" && !activeDataMatches()) {
    $("range-label").textContent = windowLabel(state.window) + " window · unavailable";
    if (state.view === "overview") $("window-note").textContent = "Waiting for the selected range";
  }
}
function navigateView(view, entity = null) {
  ++navigationEpoch;
  ++state.request; // Includes navigation that only paints cached content.
  for (const name of Object.keys(pendingViews)) {
    pendingViews[name]?.controller.abort();
    pendingViews[name] = null;
  }
  if (overviewHealth) {
    overviewHealth.controller.abort();
    overviewHealth = null;
  }
  state.entity = entity;
  setView(view); // Intent is visible before any response arrives.
  clearError();
  setLoading(false);
  renderRanges(false);
  updateDataValidity();
}
function ownsRender(request) {
  return !request.controller.signal.aborted
    && request.generation === state.request
    && request.view === state.view
    && request.window === state.window
    && (request.view !== "detail" || request.identity === entityIdentity(state.entity?.kind, state.entity?.key, state.window));
}
function startViewRequest(view) {
  if (state.view !== view || pendingViews[view]) return null;
  const request = {
    view, window:state.window, generation:++state.request,
    kind:state.entity?.kind, key:state.entity?.key,
    identity:entityIdentity(state.entity?.kind, state.entity?.key, state.window),
    controller:new AbortController()
  };
  pendingViews[view] = request;
  setLoading(true);
  updateDataValidity();
  return request;
}
function finishViewRequest(request) {
  if (pendingViews[request.view] !== request) return;
  pendingViews[request.view] = null;
  if (ownsRender(request)) {
    setLoading(false);
    updateDataValidity();
  }
}
function failViewRequest(request, error) {
  if (!ownsRender(request) || error?.name === "AbortError") return;
  const range = windowLabel(request.window);
  if (request.view === "overview" && summaryMatches()) {
    state.summary = {...state.summary, status:"stale"};
    renderNavbar(state.summary);
  } else if (request.view === "detail" && entityMatches()) {
    state.entityData = {...state.entityData, status:"stale"};
    renderNavbar(state.entityData);
  }
  showError(new Error((request.view === "diagnostics" ? "Diagnostics" : range + " data")
    + " could not refresh. " + (activeDataMatches() ? "Showing the last successful data. " : "Use Retry. ")
    + (error?.message || String(error))));
}
function refreshOverviewHealth(prefetched) {
  if (overviewHealth) return;
  const task = {controller:new AbortController(), epoch:navigationEpoch};
  overviewHealth = task;
  jsonFetch("/api/spend/health", prefetched, task.controller.signal).then(health => {
    if (overviewHealth === task && task.epoch === navigationEpoch && state.view === "overview"
      && !task.controller.signal.aborted) state.health = health;
  }).catch(() => {}).finally(() => {
    if (overviewHealth === task) overviewHealth = null;
  });
}

function returnOverview(home = false) {
  navigateView("overview");
  if (home) window.scrollTo({top:0});
  if (summaryMatches()) {
    state.summary = {...state.summary, status:"stale", snapshot:true};
    renderPreservingScroll(true, renderOverview);
  }
  else return loadSummary(true);
}
