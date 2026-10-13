/* Plans & Value comparison view. Backend owns arithmetic; this module formats and owns request lifetime. */
(() => {
  const PERIODS = [{key: "this_month", label: "This month"}, {key: "last_month", label: "Last month"}, {key: "this_cycle", label: "This cycle"}];
  const periodIndex = key => Math.max(0, PERIODS.findIndex(item => item.key === key));
  const esc = s => String(s ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
  const finite = v => (v == null || v === "" || !Number.isFinite(+v)) ? null : +v;
  const formatUsd = v => finite(v) == null ? null : "$" + (+v).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const formatMultiple = v => finite(v) == null ? null : (+v).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2}) + "×";
  const formatShortDate = d => !/^\d{4}-\d{2}-\d{2}$/.test(d || "") ? (d || "") : new Intl.DateTimeFormat("en-US", {month: "short", day: "numeric", year: "numeric", timeZone: "UTC"}).format(new Date(d + "T12:00:00Z"));
  const periodCaption = p => (p?.period === "this_cycle" ? (p.periodLabel || "Each plan's current quota window") : null) || p?.periodLabel || ((p?.localFrom || p?.localStartDate) && (p?.localTo || p?.localEndDate) ? `${formatShortDate(p.localFrom || p.localStartDate)} – ${formatShortDate(p.localTo || p.localEndDate)} (${p.timezone || ""})` : (p?.timezone ? `(${p.timezone})` : ""));

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

  const collectionLabel = g => g?.collectionEvidence?.label || "Collection status unavailable";

  const attributionLabel = g => {
    const a = g?.attribution || {};
    if (a.label) return String(a.label);
    const st = a.status || "configured";
    return st === "shared" ? "Shared tool association" : st === "unsupported" ? "Unsupported custom association" : st === "unassigned" ? "Unassigned" : "Configured tool association";
  };

  const toolBadge = g => String(g?.toolLabel || (g?.toolKeys || []).join(" / ") || "tool");
  const MARKS = {
    codex: ["Codex", "M8.086.457a6.105 6.105 0 013.046-.415c1.333.153 2.521.72 3.564 1.7a.117.117 0 00.107.029c1.408-.346 2.762-.224 4.061.366l.063.03.154.076c1.357.703 2.33 1.77 2.918 3.198.278.679.418 1.388.421 2.126a5.655 5.655 0 01-.18 1.631.167.167 0 00.04.155 5.982 5.982 0 011.578 2.891c.385 1.901-.01 3.615-1.183 5.14l-.182.22a6.063 6.063 0 01-2.934 1.851.162.162 0 00-.108.102c-.255.736-.511 1.364-.987 1.992-1.199 1.582-2.962 2.462-4.948 2.451-1.583-.008-2.986-.587-4.21-1.736a.145.145 0 00-.14-.032c-.518.167-1.04.191-1.604.185a5.924 5.924 0 01-2.595-.622 6.058 6.058 0 01-2.146-1.781c-.203-.269-.404-.522-.551-.821a7.74 7.74 0 01-.495-1.283 6.11 6.11 0 01-.017-3.064.166.166 0 00.008-.074.115.115 0 00-.037-.064 5.958 5.958 0 01-1.38-2.202 5.196 5.196 0 01-.333-1.589 6.915 6.915 0 01.188-2.132c.45-1.484 1.309-2.648 2.577-3.493.282-.188.55-.334.802-.438.286-.12.573-.22.861-.304a.129.129 0 00.087-.087A6.016 6.016 0 015.635 2.31C6.315 1.464 7.132.846 8.086.457zm-.804 7.85a.848.848 0 00-1.473.842l1.694 2.965-1.688 2.848a.849.849 0 001.46.864l1.94-3.272a.849.849 0 00.007-.854l-1.94-3.393zm5.446 6.24a.849.849 0 000 1.695h4.848a.849.849 0 000-1.696h-4.848z"],
    "claude-code": ["Claude", "M4.709 15.955l4.72-2.647.08-.23-.08-.128H9.2l-.79-.048-2.698-.073-2.339-.097-2.266-.122-.571-.121L0 11.784l.055-.352.48-.321.686.06 1.52.103 2.278.158 1.652.097 2.449.255h.389l.055-.157-.134-.098-.103-.097-2.358-1.596-2.552-1.688-1.336-.972-.724-.491-.364-.462-.158-1.008.656-.722.881.06.225.061.893.686 1.908 1.476 2.491 1.833.365.304.145-.103.019-.073-.164-.274-1.355-2.446-1.446-2.49-.644-1.032-.17-.619a2.97 2.97 0 01-.104-.729L6.283.134 6.696 0l.996.134.42.364.62 1.414 1.002 2.229 1.555 3.03.456.898.243.832.091.255h.158V9.01l.128-1.706.237-2.095.23-2.695.08-.76.376-.91.747-.492.584.28.48.685-.067.444-.286 1.851-.559 2.903-.364 1.942h.212l.243-.242.985-1.306 1.652-2.064.73-.82.85-.904.547-.431h1.033l.76 1.129-.34 1.166-1.064 1.347-.881 1.142-1.264 1.7-.79 1.36.073.11.188-.02 2.856-.606 1.543-.28 1.841-.315.833.388.091.395-.328.807-1.969.486-2.309.462-3.439.813-.042.03.049.061 1.549.146.662.036h1.622l3.02.225.79.522.474.638-.079.485-1.215.62-1.64-.389-3.829-.91-1.312-.329h-.182v.11l1.093 1.068 2.006 1.81 2.509 2.33.127.578-.322.455-.34-.049-2.205-1.657-.851-.747-1.926-1.62h-.128v.17l.444.649 2.345 3.521.122 1.08-.17.353-.608.213-.668-.122-1.374-1.925-1.415-2.167-1.143-1.943-.14.08-.674 7.254-.316.37-.729.28-.607-.461-.322-.747.322-1.476.389-1.924.315-1.53.286-1.9.17-.632-.012-.042-.14.018-1.434 1.967-2.18 2.945-1.726 1.845-.414.164-.717-.37.067-.662.401-.589 2.388-3.036 1.44-1.882.93-1.086-.006-.158h-.055L4.132 18.56l-1.13.146-.487-.456.061-.746.231-.243 1.908-1.312-.006.006z"],
    cursor: ["Cursor", "M22.106 5.68L12.5.135a.998.998 0 00-.998 0L1.893 5.68a.84.84 0 00-.419.726v11.186c0 .3.16.577.42.727l9.607 5.547a.999.999 0 00.998 0l9.608-5.547a.84.84 0 00.42-.727V6.407a.84.84 0 00-.42-.726zm-.603 1.176L12.228 22.92c-.063.108-.228.064-.228-.061V12.34a.59.59 0 00-.295-.51l-9.11-5.26c-.107-.062-.063-.228.062-.228h18.55c.264 0 .428.286.296.514z"],
    grok: ["Grok", "M9.27 15.29l7.978-5.897c.391-.29.95-.177 1.137.272.98 2.369.542 5.215-1.41 7.169-1.951 1.954-4.667 2.382-7.149 1.406l-2.711 1.257c3.889 2.661 8.611 2.003 11.562-.953 2.341-2.344 3.066-5.539 2.388-8.42l.006.007c-.983-4.232.242-5.924 2.75-9.383.06-.082.12-.164.179-.248l-3.301 3.305v-.01L9.267 15.292M7.623 16.723c-2.792-2.67-2.31-6.801.071-9.184 1.761-1.763 4.647-2.483 7.166-1.425l2.705-1.25a7.808 7.808 0 00-1.829-1A8.975 8.975 0 005.984 5.83c-2.533 2.536-3.33 6.436-1.962 9.764 1.022 2.487-.653 4.246-2.34 6.022-.599.63-1.199 1.259-1.682 1.925l7.62-6.815"],
    zai: ["Z.AI", "M12.105 2L9.927 4.953H.653L2.83 2h9.276zM23.254 19.048L21.078 22h-9.242l2.174-2.952h9.244zM24 2L9.264 22H0L14.736 2H24z"],
    antigravity: ["Antigravity", "M12 4.5 20 18.5H4z"],
    openrouter: ["OpenRouter", "M7 10a2 2 0 100 4 2 2 0 000-4zm10-4.5a2 2 0 100 4 2 2 0 000-4zM17 14.5a2 2 0 100 4 2 2 0 000-4zM9 12h6"],
    xai: ["xAI", "M6.469 8.776L16.512 23h-4.464L2.005 8.776H6.47zm-.004 7.9l2.233 3.164L6.467 23H2l4.465-6.324zM22 2.582V23h-3.659V7.764L22 2.582zM22 1l-9.952 14.095-2.233-3.163L17.533 1H22z"],
    custom: ["Custom", "M12 5.5a6.5 6.5 0 100 13 6.5 6.5 0 000-13zm0 4.5a2 2 0 100 4 2 2 0 000-4z"]
  };
  const markKey = tool => tool === "opencode" || tool === "zcode" ? "zai" : tool;
  const toolsOf = g => {
    const keys = (g?.toolKeys || []).map(key => markKey(String(key)));
    const list = (keys.length ? keys : ["custom"]).filter((key, index, all) => all.indexOf(key) === index);
    return list.slice(0, 2);
  };
  const svgMark = markup => {
    const node = typeof document.createElementNS === "function"
      ? document.createElementNS("http://www.w3.org/2000/svg", "svg")
      : document.createElement("svg");
    node.setAttribute("viewBox", "0 0 24 24");
    node.setAttribute("aria-hidden", "true");
    node.innerHTML = '<path fill="currentColor" fill-rule="evenodd" d="' + markup + '"/>';
    return node;
  };
  const logo = tool => {
    const key = markKey(tool);
    const spec = MARKS[key] || MARKS.custom;
    const id = MARKS[key] ? key : "custom";
    const wrap = h("span", {className: "plans-value-logo", "data-tool": id, title: spec[0]});
    wrap.append(svgMark(spec[1]));
    return wrap;
  };
  const includedPlans = g => (g?.plans || []).map(p => p.name || ("Plan " + (p.id ?? ""))).join(", ");

  const sanitizeExplanationText = v => String(v ?? "")
    .replace(/[A-Za-z]:\\[^\s]+/g, "[path omitted]")
    .replace(/\/(?:Users|home|var|etc|tmp)\/[^\s]+/g, "[path omitted]")
    .replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi, "[redacted]")
    .replace(/\b(sk-|Bearer\s+)[A-Za-z0-9._-]{8,}/gi, "[redacted]");

  const explanationSections = (g, p) => {
    const ex = g?.explanation || {}, ref = ex.referenceValue || {};
    const join = (...values) => values.flat().filter(v => (typeof v === "string" && v) || typeof v === "number").map(sanitizeExplanationText).join(". ");
    const date = value => {
      const parsed = new Date(value || "");
      if (!Number.isFinite(parsed.getTime())) return "Unavailable";
      return new Intl.DateTimeFormat("en-US", {dateStyle:"medium",timeStyle:"short",timeZone:p?.timezone || "UTC"}).format(parsed);
    };
    const collection = g?.collectionEvidence || {};
    const sources = (collection.sources || []).slice(0,12).map(s => join(
      s.sourceLabel || s.source, s.status, s.configuredState,
      `Last attempt: ${date(s.lastAttemptAt)}`, `Last success: ${date(s.lastSuccessAt)}`,
      `Freshness: ${s.freshness?.state || "unknown"}`, s.reason, s.coverage
    ));
    const exclusions = (ref.excluded || []).slice(0,8).map(item => `Excluded: ${typeof item?.detail === "string" ? item.detail : "Reason unavailable"}`);
    return [
      ["Period", join(periodCaption(p), (ex.period?.activeIntervals || []).map(i => `${i.startUtc} – ${i.endUtc}`), ex.period?.note)],
      ["Configured cost", join(includedPlans(g), ex.configuredCost?.formula, ex.configuredCost?.note)],
      ["Tools & association", join(toolBadge(g), ex.tools?.label, ex.tools?.note)],
      ["Reference value", join(ref.note, `${ref.pricedEvents ?? "Unavailable"} priced events; ${ref.unpricedEvents ?? "Unavailable"} unpriced events`, (ref.unpricedModels || []).map(m => typeof m === "string" ? m : `${m.modelKey || "Unknown model"}: ${m.tokens ?? "Unavailable"} tokens`), exclusions)],
      ["Collection limits", join(collectionLabel(g), sources, collection.note, ex.limits?.pricingVsCollection)],
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
    const inner = h("div", {className: "plans-value-why-inner"});
    for (const [title, text] of explanationSections(g, p)) {
      inner.append(h("section", {className: "plans-value-why-section"}, h("h4", {text: title}), h("p", {text})));
    }
    return h("details", {className: "plans-value-why"},
      h("summary", {"aria-label": `Why this number for ${g?.name || "plan"}?`}, "Why", h("span", {className: "plans-value-why-chevron", "aria-hidden": "true"})),
      h("div", {className: "plans-value-why-body"}, inner)
    );
  }

  function renderRow(g, p, ctx) {
    const name = g?.name || "Plan group", pl = includedPlans(g);
    const cycleOff = p?.period === "this_cycle" && g?.cycleAvailable === false;
    const cov = g?.pricingCoverage || {}, ev = g?.collectionEvidence || {};
    const covCls = cov.status === "complete" ? "badge-complete" : cov.status === "partial" ? "badge-partial" : "badge-warn";
    const evCls = ev.status === "healthy" ? "badge-complete" : "badge-warn";
    const note = cycleOff ? (g.cycleReason || "Cycle comparison is unavailable for this plan.") : (g?.cycleLabel || "");
    const showIncluded = pl && pl !== name;
    const metric = (l, cls, txt) => {
      const figure = /^[$≥]/.test(txt) || /×$/.test(txt);
      return h("div", {className: "plans-value-metric"},
        h("strong", {className: cls + " mono" + (figure ? " is-figure" : ""), title: txt, text: txt}),
        h("span", {className: "plans-value-metric-label", text: l})
      );
    };
    const attribution = attributionLabel(g);
    const showAttribution = attribution !== "Configured tool association";
    const tools = toolsOf(g);
    const logos = h("div", {className: tools.length > 1 ? "plans-value-logos is-pair" : "plans-value-logos"});
    for (const tool of tools) logos.append(logo(tool));
    return h("article", {className: "plans-value-row", "data-group-id": g?.groupId || "", "data-tool": tools[0], "aria-label": `Comparison for ${name}`},
      h("div", {className: "plans-value-card"},
        logos,
        h("div", {className: "plans-value-identity"},
          h("strong", {className: "plans-value-name", text: name}),
          h("span", {className: "plans-value-tool-badge", text: toolBadge(g)}),
          showIncluded ? h("p", {className: "plans-value-included", text: "Includes: " + pl}) : null,
          note ? h("p", {className: "plans-value-included", text: note}) : null
        ),
        h("div", {className: "plans-value-metrics"},
          metric("Cost", "plans-value-cost", cycleOff ? "Unavailable" : costLabel(g)),
          metric("Usage", "plans-value-usage", cycleOff ? "Unavailable" : usageLabel(g)),
          metric("Multiple", "plans-value-multiple", cycleOff ? "Unavailable" : multipleLabel(g))
        )
      ),
      h("div", {className: "plans-value-meta"},
        h("div", {className: "plans-value-badges", "aria-label": "Coverage and attribution"},
          h("span", {className: "plans-value-badge " + covCls, "data-kind": "pricing", title: coverageLabel(g), text: coverageLabel(g)}),
          h("span", {className: "plans-value-badge " + evCls, "data-kind": "collection", title: collectionLabel(g), text: collectionLabel(g)}),
          showAttribution ? h("span", {className: "plans-value-badge badge-complete", "data-kind": "attribution", title: attribution, text: attribution}) : null
        ),
        h("div", {className: "plans-value-row-actions"},
          btn("settings-link", `Manage plans for ${name}`, () => ctx.openPlanManager?.(g), "Plans"),
          btn("ghost-button", `Check connection for ${name}`, event => ctx.openHarnesses?.(g, event.currentTarget), "Connect")
        )
      ),
      renderWhy(g, p)
    );
  }

  function renderUnassigned(un, ctx) {
    if (!un) return null;
    return h("section", {className: "plans-value-unassigned", "aria-label": "Unassigned usage"},
      h("div", {className: "plans-value-unassigned-head"},
        h("strong", {text: "Unassigned / no configured plan"}),
        h("span", {className: "plans-value-usage mono", text: usageLabel(un)}),
        btn("settings-link", "Open plan manager for unassigned usage", () => ctx.openPlanManager?.(), "Plans")
      ),
      h("p", {className: "plans-value-unassigned-note", text: sanitizeExplanationText(un.explanation?.note || (typeof un.explanation === "string" ? un.explanation : "Usage without an applicable configured plan or safe tool association."))})
    );
  }

  const hasClass = (node, name) => String(node?.className || "").split(/\s+/).includes(name);
  function findClass(root, name) {
    const stack = [...(root?.children || [])];
    while (stack.length) {
      const node = stack.shift();
      if (hasClass(node, name)) return node;
      if (node?.children?.length) stack.push(...node.children);
    }
    return null;
  }

  function playShift(node, direction) {
    if (!node?.setAttribute) return;
    node.setAttribute("data-shift", direction || "in");
    const list = node.classList;
    if (!list || typeof list.toggle !== "function") return;
    list.toggle("is-entering", false);
    if (typeof node.getBoundingClientRect === "function") node.getBoundingClientRect();
    list.toggle("is-entering", true);
  }

  function ensureFrame(cnt, period, ctx, caption) {
    let frame = findClass(cnt, "plans-value");
    if (!frame) {
      frame = h("section", {className: "plans-value", "aria-label": "Plans and Value"},
        h("div", {className: "plans-value-toolbar"},
          h("div", {className: "cadence-control plans-value-period", role: "group", "aria-label": "Comparison period"},
            ...PERIODS.map(it => h("button", {
              type: "button", "data-period": it.key, "aria-pressed": "false", "aria-label": it.label,
              onClick: () => ctx.onPeriodChange?.(it.key)
            }, it.label))
          ),
          h("p", {className: "plans-value-dates", "aria-label": "Selected period dates"})
        ),
        h("div", {className: "plans-value-stage"}),
        h("div", {className: "plans-value-footer"},
          btn("settings-link", "Open plan manager", () => ctx.openPlanManager?.(), "Plans"),
          btn("ghost-button", "Open connection diagnostics", event => ctx.openHarnesses?.(null, event.currentTarget), "Connections")
        )
      );
      cnt.replaceChildren(frame);
    }
    const buttons = [];
    const pending = [...(frame.children || [])];
    while (pending.length) {
      const node = pending.shift();
      if (node?.getAttribute?.("data-period")) buttons.push(node);
      if (node?.children?.length) pending.push(...node.children);
    }
    for (const button of buttons) {
      const on = button.getAttribute("data-period") === period;
      button.setAttribute("aria-pressed", String(on));
      button.classList?.toggle?.("active", on);
    }
    const dates = findClass(frame, "plans-value-dates");
    if (dates) dates.textContent = caption || "";
    return frame;
  }

  function skelCard() {
    return h("article", {className: "plans-value-skel", "aria-hidden": "true"},
      h("span", {className: "plans-value-skel-mark"}),
      h("span", {className: "plans-value-skel-copy"}, h("i"), h("i", {className: "is-short"})),
      h("span", {className: "plans-value-skel-metrics"}, h("i"), h("i"), h("i"))
    );
  }

  function renderLoading(cnt, period, ctx = {}) {
    const label = period === "last_month" ? "last month" : period === "this_cycle" ? "this cycle" : "this month";
    const frame = ensureFrame(cnt, period, ctx, "");
    const stage = findClass(frame, "plans-value-stage");
    stage.replaceChildren(h("div", {className: "plans-value-loading", role: "status", "aria-label": "Loading Plans and Value"},
      h("p", {text: `Loading ${label}…`}),
      skelCard(), skelCard(), skelCard()
    ));
  }

  function renderError(cnt, err, onRetry, period, ctx = {}) {
    const frame = ensureFrame(cnt, period, ctx, "");
    findClass(frame, "plans-value-stage").replaceChildren(h("div", {className: "plans-value-error", role: "alert"}, h("p", {text: err?.message || "Plans & Value could not load."}), btn("ghost-button", "Retry loading Plans and Value", onRetry, "Retry")));
  }

  function renderContent(cnt, p, ctx, period, shift) {
    const frame = ensureFrame(cnt, period, ctx, periodCaption(p || {timezone: ctx.timezone}));
    const stage = findClass(frame, "plans-value-stage");
    const groups = p?.groups || [];
    stage.replaceChildren();
    if (!groups.length && !p?.unassigned) renderEmpty(stage, p || {emptyReason: "no_plans"}, ctx);
    else {
      for (const g of groups) stage.append(renderRow(g, p, ctx));
      const un = renderUnassigned(p?.unassigned, ctx);
      if (un) stage.append(un);
    }
    playShift(stage, shift);
    playShift(findClass(frame, "plans-value-dates"), shift);
  }

  function defaultFetchPeriod(period, {signal} = {}) {
    return fetch("/api/subscriptions/value?period=" + encodeURIComponent(period), {cache: "no-store", signal}).then(async res => {
      const d = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(d.error || "Plans & Value request failed.");
      return d;
    });
  }

  function createPlansValueController(container, context = {}) {
    let generation = 0, period = context.initialPeriod || "this_month", payload = null, abort = null, closed = false, shift = "in";
    const fetchPeriod = context.fetchPeriod || defaultFetchPeriod;
    const viewContext = {...context, onPeriodChange(next) { load(next); }};
    function owns(token) { return !closed && token === generation; }
    function paint() { if (!closed) renderContent(container, payload, viewContext, period, shift); shift = "in"; }

    async function load(nextPeriod = period) {
      const token = ++generation;
      if (nextPeriod !== period) shift = periodIndex(nextPeriod) >= periodIndex(period) ? "next" : "prev";
      period = nextPeriod;
      abort?.abort();
      abort = typeof AbortController !== "undefined" ? new AbortController() : null;
      payload = null;
      renderLoading(container, period, viewContext);
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
        renderError(container, err, () => load(period), period, viewContext);
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

  if (typeof module !== "undefined" && module.exports) {
   const api = {
    renderPlansValue, createPlansValueController, renderContent, renderLoading, renderError,
    formatUsd, formatMultiple, formatShortDate, periodCaption, esc, multipleLabel, usageLabel, costLabel, coverageLabel, collectionLabel, attributionLabel,
    sanitizeExplanationText, PERIODS
  };
   module.exports = api;
   if (typeof window !== "undefined") window.PlansValue = window.plansValueApi = api;
  }
  if (typeof window !== "undefined") {
    window.renderPlansValue = renderPlansValue;
    window.createPlansValueController = createPlansValueController;
    window.BurnrateMarks = { mark: logo };
  }
})();
