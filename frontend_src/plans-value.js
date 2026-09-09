/* Plans & Value comparison view. Backend owns arithmetic; this module formats and owns request lifetime. */
(() => {
  const PERIODS = [{key: "this_month", label: "This month"}, {key: "last_month", label: "Last month"}];
  const esc = s => String(s ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
  const finite = v => (v == null || v === "" || !Number.isFinite(+v)) ? null : +v;
  const formatUsd = v => finite(v) == null ? null : "$" + (+v).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const formatMultiple = v => finite(v) == null ? null : (+v).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2}) + "×";
  const formatShortDate = d => !/^\d{4}-\d{2}-\d{2}$/.test(d || "") ? (d || "") : new Intl.DateTimeFormat("en-US", {month: "short", day: "numeric", year: "numeric", timeZone: "UTC"}).format(new Date(d + "T12:00:00Z"));
  const periodCaption = p => p?.periodLabel || ((p?.localFrom || p?.localStartDate) && (p?.localTo || p?.localEndDate) ? `${formatShortDate(p.localFrom || p.localStartDate)} – ${formatShortDate(p.localTo || p.localEndDate)} (${p.timezone || ""})` : (p?.timezone ? `(${p.timezone})` : ""));

  const usageLabel = g => {
    const st = g?.usageValueStatus || g?.usageStatus;
    if (st === "no_records") return "No recorded usage";
    const m = formatUsd(g?.usageValueUsd);
    if (m == null) return st === "measured_zero" ? "$0.00" : "Unavailable";
    return (g?.usageBound === "lower_bound" || st === "lower_bound" || st === "partial") ? "≥ " + m : m;
  };

  const multipleLabel = g => {
    const m = g?.multiple ?? g?.referenceMultiple;
    const f = formatMultiple(m);
    if (f != null) return ((g?.multipleBasis) === "lower_bound") ? "≥ " + f : f;
    return g?.multipleReason || "Unavailable";
  };

  const costLabel = g => formatUsd(g?.configuredCostUsd ?? g?.accruedCostUsd) || "$0.00";

  const coverageLabel = g => {
    const c = g?.pricingCoverage || {};
    if (c.label) return String(c.label);
    const st = c.status || "none", ev = c.eventCount, un = c.unpricedModels || [];
    if (st === "complete") return `Complete (${ev ?? 0} events)`;
    if (st === "partial") return `Partial (${un.length === 1 ? "1 unpriced model" : (un.length || 0) + " unpriced models"})`;
    return st === "none" ? "No recorded usage" : "Unavailable";
  };

  const collectionLabel = g => {
    const ev = g?.collectionEvidence || {};
    if (ev.label) return String(ev.label);
    const st = ev.status || "unknown", s = ev.freshnessSeconds ?? ev.freshness_seconds;
    const r = s == null ? null : s < 60 ? "just now" : s < 3600 ? Math.floor(s/60)+"m ago" : s < 86400 ? Math.floor(s/3600)+"h ago" : Math.floor(s/86400)+"d ago";
    return st === "healthy" ? (r ? `Healthy (${r})` : "Healthy") : st === "stale" ? "Stale" : st === "partial" ? "Partial collection" : st === "failed" ? "Collection failed" : "Unknown";
  };

  const attributionLabel = g => {
    const a = g?.attribution || {};
    if (a.label) return String(a.label);
    const st = a.status || "configured";
    return st === "shared" ? "Shared tool association" : st === "unsupported" ? "Unsupported custom association" : st === "unassigned" ? "Unassigned" : "Configured tool association";
  };

  const toolBadge = g => String(g?.toolLabel || (g?.toolKeys || []).join(" / ") || "tool");
  const includedPlans = g => (g?.plans || []).map(p => p.name || ("Plan " + (p.id ?? ""))).join(", ");

  const sanitizeExplanationText = v => String(v ?? "")
    .replace(/[A-Za-z]:\\[^\s]+/g, "[path omitted]")
    .replace(/\/(?:Users|home|var|etc|tmp)\/[^\s]+/g, "[path omitted]")
    .replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi, "[redacted]")
    .replace(/\b(sk-|Bearer\s+)[A-Za-z0-9._-]{8,}/gi, "[redacted]");

  const explanationSections = (g, p) => {
    const ex = g?.explanation || {}, ref = ex.referenceValue || {};
    const join = (...values) => values.flat().filter(v => v != null && v !== "").map(sanitizeExplanationText).join(". ");
    return [
      ["Period", join(periodCaption(p), (ex.period?.activeIntervals || []).map(i => `${i.startUtc} – ${i.endUtc}`), ex.period?.note)],
      ["Configured cost", join(includedPlans(g), ex.configuredCost?.formula, ex.configuredCost?.note)],
      ["Tools & association", join(toolBadge(g), ex.tools?.label, ex.tools?.note)],
      ["Reference value", join(ref.note, `${ref.pricedEvents ?? 0} priced events; ${ref.unpricedEvents ?? 0} unpriced events`, (ref.unpricedModels || []).map(m => typeof m === "string" ? m : `${m.modelKey}: ${m.tokens} tokens`), ref.excluded)],
      ["Collection limits", join(collectionLabel(g), ex.limits?.pricingVsCollection)],
      ["Reference-value multiple", join(ex.multiple?.formula || multipleLabel(g), ex.multiple?.note)]
    ];
  };

  const h = (tag, a, ...c) => {
    const n = document.createElement(tag);
    if (a) for (const [k, v] of Object.entries(a)) {
      if (v == null || v === false) continue;
      if (k === "className") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2).toLowerCase(), v);
      else if (k === "dataset") Object.assign(n.dataset, v);
      else n.setAttribute(k, v === true ? "" : String(v));
    }
    for (const ch of c.flat()) if (ch != null && ch !== false) n.append(typeof ch === "string" ? document.createTextNode(ch) : ch);
    return n;
  };

  const btn = (cls, lbl, act, txt) => h("button", {type: "button", className: cls, "aria-label": lbl, onClick: act}, txt);

  function renderEmpty(cnt, p, ctx) {
    const r = p?.emptyReason || p?.empty_reason;
    const msg = r === "no_plans" ? "No plans are configured for this period." : r === "no_usage" ? "No recorded usage for this period." : "No plan comparisons are available for this period.";
    cnt.append(h("div", {className: "plans-value-empty", role: "status"}, h("p", {text: msg}), btn("settings-link", "Manage plans", () => ctx.openPlanManager?.(), "Manage plans")));
  }

  function renderWhy(g, p) {
    const b = h("div", {className: "plans-value-why-body"});
    for (const [title, text] of explanationSections(g, p)) {
      b.append(h("section", {className: "plans-value-why-section"}, h("h4", {text: title}), h("p", {text})));
    }
    return h("details", {className: "plans-value-why"}, h("summary", {"aria-label": `Why this number for ${g?.name || "plan"}?`}, "Why this number?"), b);
  }

  function renderRow(g, p, ctx) {
    const name = g?.name || "Plan group", pl = includedPlans(g);
    const cov = g?.pricingCoverage || {}, ev = g?.collectionEvidence || {};
    const covCls = cov.status === "complete" ? "badge-complete" : cov.status === "partial" ? "badge-partial" : "badge-warn";
    const evCls = ev.status === "healthy" ? "badge-complete" : "badge-warn";
    const metric = (l, cls, txt) => h("div", {className: "plans-value-metric"}, h("span", {className: "plans-value-metric-label", text: l}), h("strong", {className: cls + " mono", text: txt}));
    return h("article", {className: "plans-value-row", "data-group-id": g?.groupId || "", "aria-label": `Comparison for ${name}`},
      h("header", {className: "plans-value-row-head"},
        h("div", null,
          h("strong", {className: "plans-value-name", text: name}),
          pl ? h("p", {className: "plans-value-included", text: "Includes: " + pl}) : null,
          h("span", {className: "plans-value-tool-badge", text: toolBadge(g)})
        ),
        h("div", {className: "plans-value-metrics"},
          metric("Configured cost", "plans-value-cost", costLabel(g)),
          metric("Recorded usage value", "plans-value-usage", usageLabel(g)),
          metric("Reference-value multiple", "plans-value-multiple", multipleLabel(g))
        )
      ),
      h("div", {className: "plans-value-badges", "aria-label": "Coverage and attribution"},
        h("span", {className: "plans-value-badge " + covCls, "data-kind": "pricing", text: coverageLabel(g)}),
        h("span", {className: "plans-value-badge " + evCls, "data-kind": "collection", text: collectionLabel(g)}),
        h("span", {className: "plans-value-badge badge-complete", "data-kind": "attribution", text: attributionLabel(g)})
      ),
      renderWhy(g, p),
      h("div", {className: "plans-value-row-actions"},
        btn("settings-link", `Manage plans for ${name}`, () => ctx.openPlanManager?.(g), "Manage plans"),
        btn("ghost-button", `Check connection for ${name}`, () => ctx.openHarnesses?.(g), "Check connection")
      )
    );
  }

  function renderUnassigned(un, ctx) {
    if (!un) return null;
    return h("section", {className: "plans-value-unassigned", "aria-label": "Unassigned usage"},
      h("h3", {text: "Unassigned / no configured plan"}),
      h("p", {className: "plans-value-usage mono", text: "Recorded usage: " + usageLabel(un)}),
      h("p", {className: "plans-value-unassigned-note", text: sanitizeExplanationText(un.explanation?.note || (typeof un.explanation === "string" ? un.explanation : "Usage without an applicable configured plan or safe tool association."))}),
      btn("settings-link", "Open plan manager for unassigned usage", () => ctx.openPlanManager?.(), "Manage plans")
    );
  }

  function renderLoading(cnt, period) {
    cnt.replaceChildren(h("div", {className: "plans-value-loading", role: "status", "aria-label": "Loading Plans and Value"}, h("p", {text: `Loading ${period === "last_month" ? "last month" : "this month"}…`})));
  }

  function renderError(cnt, err, onRetry) {
    cnt.replaceChildren(h("div", {className: "plans-value-error", role: "alert"}, h("p", {text: err?.message || "Plans & Value could not load."}), btn("ghost-button", "Retry loading Plans and Value", onRetry, "Retry")));
  }

  function renderContent(cnt, p, ctx, period) {
    const head = h("div", {className: "plans-value-toolbar"},
      h("div", {className: "cadence-control plans-value-period", role: "group", "aria-label": "Comparison period"},
        ...PERIODS.map(it => h("button", {
          type: "button", "data-period": it.key, "aria-pressed": String(it.key === period),
          className: it.key === period ? "active" : "", "aria-label": it.label, onClick: () => ctx.onPeriodChange?.(it.key)
        }, it.label))
      ),
      h("p", {className: "plans-value-dates", "aria-label": "Selected period dates"}, periodCaption(p || {timezone: ctx.timezone}))
    );
    const body = h("div", null);
    const groups = p?.groups || [];
    if (!groups.length && !p?.unassigned) renderEmpty(body, p || {emptyReason: "no_plans"}, ctx);
    else {
      for (const g of groups) body.append(renderRow(g, p, ctx));
      const un = renderUnassigned(p?.unassigned, ctx);
      if (un) body.append(un);
    }
    const footer = h("div", {className: "plans-value-footer"},
      btn("settings-link", "Open plan manager", () => ctx.openPlanManager?.(), "Manage plans"),
      btn("ghost-button", "Open connection diagnostics", () => ctx.openHarnesses?.(), "Check connection")
    );
    cnt.replaceChildren(h("section", {className: "plans-value", "aria-label": "Plans and Value"}, head, body, footer));
  }

  function defaultFetchPeriod(period, {signal} = {}) {
    return fetch("/api/subscriptions/value?period=" + encodeURIComponent(period), {cache: "no-store", signal}).then(async res => {
      const d = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(d.error || "Plans & Value request failed.");
      return d;
    });
  }

  function createPlansValueController(container, context = {}) {
    let generation = 0, period = context.initialPeriod || "this_month", payload = null, abort = null, closed = false;
    const fetchPeriod = context.fetchPeriod || defaultFetchPeriod;
    const viewContext = {...context, onPeriodChange(next) { load(next); }};
    function owns(token) { return !closed && token === generation; }
    function paint() { if (!closed) renderContent(container, payload, viewContext, period); }

    async function load(nextPeriod = period) {
      const token = ++generation;
      period = nextPeriod;
      abort?.abort();
      abort = typeof AbortController !== "undefined" ? new AbortController() : null;
      payload = null;
      renderLoading(container, period);
      try {
        const data = await fetchPeriod(period, {signal: abort?.signal});
        if (!owns(token)) return null;
        payload = data;
        period = data.period || period;
        paint();
        return data;
      } catch (err) {
        if (!owns(token) || err?.name === "AbortError") return null;
        payload = null;
        renderError(container, err, () => load(period));
        return null;
      }
    }

    function apply(data) {
      const token = ++generation;
      abort?.abort();
      if (!owns(token)) return;
      payload = data;
      period = data?.period || period;
      paint();
    }

    return {
      load, apply, close() { closed = true; ++generation; abort?.abort(); }, reopen() { closed = false; }, paint,
      getPeriod: () => period, getPayload: () => payload, getGeneration: () => generation, isClosed: () => closed
    };
  }

  function renderPlansValue(container, payload, context = {}) {
    const ctrl = createPlansValueController(container, context);
    if (payload) ctrl.apply(payload);
    else ctrl.load();
    return ctrl;
  }

  const api = {
    renderPlansValue, createPlansValueController, renderContent, renderLoading, renderError,
    formatUsd, formatMultiple, formatShortDate, periodCaption, esc, multipleLabel, usageLabel, costLabel, coverageLabel, collectionLabel, attributionLabel,
    sanitizeExplanationText, PERIODS
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof window !== "undefined") {
    window.renderPlansValue = renderPlansValue;
    window.createPlansValueController = createPlansValueController;
    window.PlansValue = api;
    window.plansValueApi = api;
  }
})();
