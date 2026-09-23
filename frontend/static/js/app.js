/**
 * CCB Fraud Desk — black-bar trace navigation and live backend wiring.
 *
 * Endpoints this file calls (see reports/wiring.md for the full audit):
 *   - GET  /api/testbed?customer_id=...
 *   - GET  /api/eval-scorecard
 *   - POST /api/chat
 *   - GET  /api/chat/stream           (EventSource, A2UI frames)
 *   - GET  /api/card-oversight?customer_id=...
 *   - POST /api/card-oversight/decide
 *   - POST /api/compaction/run
 *
 * One rule runs through every render function below: a value the backend did
 * not send is drawn as the literal string "n/r". Never a zero, never a
 * remembered default. Nothing on this page is computed client-side to cover a
 * gap in the response.
 */

(function () {
  "use strict";

  // Prevent inline fallback scripts from double-binding
  window.__ccbV2FallbackBound = true;
  window.__ccbV1TraceBound = true;

  const DEFAULT_CUSTOMER_ID = "CUST00001";
  const NOT_RECORDED = "n/r";

  const STATE = {
    customerId: DEFAULT_CUSTOMER_ID,
    // Must be a member of backend.models.BankChannel. "WEB_CHAT" was not one,
    // so every turn silently arrived tagged MOBILE_APP.
    channel: "WEB_PORTAL",
    replayRunning: false,
    replayTimers: [],
    currentTraceKey: "policy",
    // The operator's preset choice. Sent as preset_id so the backend does not
    // have to guess the scenario from the question's wording.
    scenario: "",
    scenarios: [],
    testbedData: null,
    scorecardData: null,
    stream: null,
  };

  // =========================================================================
  // 0. PROVENANCE
  // Backend values arrive as {value, _provenance:{tag, source}}. Both halves
  // are rendered: a number with no origin is exactly the thing this UI used to
  // show and nobody could check.
  // =========================================================================
  function pvValue(item) {
    if (item === null || item === undefined) return NOT_RECORDED;
    if (typeof item === "object" && !Array.isArray(item) && "value" in item) {
      const v = item.value;
      return v === null || v === undefined || v === "" ? NOT_RECORDED : v;
    }
    return item;
  }

  function pvTag(item) {
    if (item && typeof item === "object" && item._provenance && item._provenance.tag) {
      return String(item._provenance.tag);
    }
    return NOT_RECORDED;
  }

  function pvSource(item) {
    if (item && typeof item === "object" && item._provenance && item._provenance.source) {
      return String(item._provenance.source);
    }
    return NOT_RECORDED;
  }

  function pvText(item) {
    const v = pvValue(item);
    return v === NOT_RECORDED ? NOT_RECORDED : String(v);
  }

  /** A value with its origin next to it. The tag is the audit trail. */
  function pvChip(item) {
    const tag = pvTag(item);
    const cls = tag === "LIVE_API" ? "ok" : tag === "STATIC" ? "no" : "on";
    return (
      '<span class="tag ' +
      cls +
      '" title="' +
      escapeHtml(pvSource(item)) +
      '">' +
      escapeHtml(tag) +
      "</span>"
    );
  }

  /** Writes value + provenance into one element, leaving "n/r" verbatim. */
  function renderMetric(el, item) {
    if (!el) return;
    el.innerHTML = escapeHtml(pvText(item)) + " " + pvChip(item);
    el.title = pvTag(item) + " · " + pvSource(item);
  }

  // =========================================================================
  // 1. 6-AGENT BLACK BAR TRACE NAVIGATION (AGENTS, buildMenu, headline, readout, pick, renderTrace, openMenu, closeMenu, setTrace)
  // =========================================================================
  /* newest first: 06 memory writer -> 01 orchestrator */
  
let AGENTS = [];
window.AGENTS = AGENTS;

/* Hard refusals. Anything else non-ALLOW (e.g. ALLOW_TO_OWNER_ONLY) is a
   conditional allow and is reported separately. */
const REFUSAL_ACTIONS = new Set(["REFUSE", "DENY", "BLOCK", "QUARANTINE"]);

/* Phase labels come from the backend as `phase.1_evidence`; show them as
   "1 · evidence" rather than leaking the span id into the UI. */
function phaseName(label) {
  const m = /^phase\.(\d+)_(.+)$/.exec(label);
  if (!m) return label;
  return m[1] + " · " + m[2].replace(/_/g, " ");
}

/* A step is one depth-0 span: a phase of the turn. Everything below it (golden
   query contracts, catalog checks, BigQuery calls) is nested underneath. Spans
   are buffered on exit, so a parent lands after its children: sort by start
   time to recover causal order. */
function groupSpansIntoSteps(spans, t0) {
  const steps = [];
  let current = null;

  const rel = (ms) => "+" + ((ms - t0) / 1000).toFixed(2) + "s";
  const secs = (ms) => (ms / 1000).toFixed(1) + "s";

  spans.forEach((span) => {
    const f = span.fields || {};
    const o = span.outcome || {};

    if (span.depth === 0) {
      current = {
        k: span.label,
        i: "",
        name: phaseName(span.label),
        ms: secs(span.elapsed_ms),
        state: span.state === "ok" ? "done" : "error",
        refusals: 0,
        tools: 0,
        deny: 0,
        sql: 0,
        jobs: 0,
        conditional: 0,
        contracts: 0,
        started: rel(span.started_ms),
        ended: rel(span.started_ms + span.elapsed_ms),
        spans: [],
        foot: [],
      };
      steps.push(current);
      return;
    }
    if (!current) return;

    const subject = f.table || f.column || f.sql || f.search || f.scope || "";
    let kind = "tool";
    let what = span.label + (subject ? " " + subject : "");

    if (o.action) {
      // A catalog verdict. The aspect that decided it is the whole point.
      // ALLOW_TO_OWNER_ONLY is a conditional allow, not a refusal: counting it
      // as one would overstate how much the catalog actually blocked.
      if (REFUSAL_ACTIONS.has(o.action)) {
        kind = "deny";
        current.deny++;
        current.refusals++;
      } else if (o.action !== "ALLOW") {
        current.conditional++;
      }
      what = subject + " → " + o.action + (o.evidence ? " · " + o.evidence : "");
    } else if (/^golden_query\./.test(span.label)) {
      // `agent` is the contract's authorised caller, not proof an agent ran.
      current.contracts++;
      what =
        span.label + (f.tool ? " · " + f.tool : "") +
        (f.agent ? " · authorised for " + f.agent : "") +
        (o.rows != null ? " · " + o.rows + " rows" : "");
    } else if (span.label === "bigquery.query") {
      kind = "out";
      current.jobs++;
      // Ad-hoc means SQL that did not come from a reviewed golden contract file.
      if (!/\.sql$/.test(String(f.sql || ""))) current.sql++;
      what =
        (f.sql || "query") +
        " · " + (o.rows != null ? o.rows + " rows" : "n/r rows") +
        (o.job_id ? " · job " + o.job_id : "");
    } else if (span.label === "channel.produce") {
      kind = "out";
      what =
        (f.agent || "channel") + " [" + (f.channel || "n/r") + "]" +
        " · " + (o.sessions != null ? o.sessions + " sessions" : "n/r") +
        " · " + (o.memories != null ? o.memories + " memories" : "n/r");
    } else if (span.label === "fraud.card_control_or_escalate") {
      kind = "out";
      what =
        (f.agent || "fraud_monitoring_agent") + " [" + (f.action || "CARD_BLOCK") + "]" +
        " · " + (o.suspicious_rows != null ? o.suspicious_rows + " suspicious" : "n/r") +
        " · " + (o.geo_rows != null ? o.geo_rows + " geo rows" : "n/r");
    } else if (span.label === "catalog.discover") {
      what =
        "searched " + (f.search || "") +
        " · " + (o.entries != null ? o.entries : "n/r") + " entries" +
        " · " + (o.tables != null ? o.tables : "n/r") + " tables" +
        (o.siblings && o.siblings.length ? " · " + o.siblings.join(", ") : "");
    }

    // Indent by nesting depth so the call tree is legible in a flat list.
    if (span.depth > 1) what = "\u00a0\u00a0".repeat(span.depth - 1) + "└ " + what;

    if (span.state === "error") {
      kind = "deny";
      current.deny++;
      what += " · " + (span.error || "failed");
    }

    current.tools++;
    current.spans.push([rel(span.started_ms), kind, what, span.elapsed_ms.toFixed(0) + "ms", "nest"]);
  });

  steps.forEach((s, n) => {
    s.i = n + 1 < 10 ? "0" + (n + 1) : String(n + 1);
    s.foot = [
      ["contracts", String(s.contracts)],
      ["tools", String(s.tools)],
      ["refusals", String(s.refusals)],
      ["owner-only", String(s.conditional)],
      ["bq jobs", String(s.jobs)],
      ["ad-hoc sql", String(s.sql)],
    ];
  });
  return steps;
}

async function fetchAndRenderTrace(runId) {
  try {
    const resp = await fetch("/api/trace/" + (runId || "latest"));
    if (!resp.ok) {
      renderRoster();
      return;
    }
    const data = await resp.json();
    const spans = (data.spans || []).slice().sort((a, b) => a.started_ms - b.started_ms);
    if (!spans.length) {
      renderRoster();
      return;
    }

    const t0 = spans[0].started_ms;
    const last = spans.reduce((m, s) => Math.max(m, s.started_ms + s.elapsed_ms), t0);
    const wall = ((last - t0) / 1000).toFixed(1) + "s";
    const steps = groupSpansIntoSteps(spans, t0);
    const runMeta = "Run " + (data.run_id || "n/r");

    AGENTS = steps.slice().reverse(); // the bar reads newest first
    window.AGENTS = AGENTS;

    const totals = steps.reduce(
      (acc, s) => ({ deny: acc.deny + s.deny, sql: acc.sql + s.sql, jobs: acc.jobs + s.jobs }),
      { deny: 0, sql: 0, jobs: 0 }
    );

    setAgentCountText("8 agents · " + steps.length + " phases");
    setRunMetaText(runMeta + " · 8-agent desk · " + steps.length + " phases · " + wall);
    setStampText(runMeta + " · " + wall + " · ad-hoc SQL " + totals.sql);
    setTruthEngineText(
      "8-agent hub-and-spoke · 5 live channels · " + steps.length + " phases in " + wall + " · " +
      totals.jobs + " BigQuery jobs · " + totals.deny + " refusals · ad-hoc SQL " + totals.sql
    );
    pick(0);
    buildMenu();
    renderRoster();
    renderGovernanceTraps(spans);
  } catch (err) {
    console.error("fetchAndRenderTrace failed", err);
  }
}

function renderRoster() {
  const rosterList = $("agentRosterList");
  if (!rosterList) return;

  const summary = STATE.channelsSummary;
  const deskAgents = (summary && Array.isArray(summary.agents)) ? summary.agents : [];
  const channelMap = {};
  if (summary && summary.snapshot && Array.isArray(summary.snapshot.channels)) {
    summary.snapshot.channels.forEach((ch) => {
      channelMap[ch.agent_id] = ch;
      channelMap[ch.agent_name] = ch;
    });
  }

  let html = "";
  if (AGENTS.length) {
    html += AGENTS.map((a, idx) => {
      return `<div class="item ${a.state}" style="cursor:pointer" data-step="${idx}">
        <div class="item-top"><span class="dot"></span>
        <span class="nm">${escapeHtml(a.name)}</span><span class="tag ok">${escapeHtml(a.ms)}</span></div>
        <p class="rd">${a.tools} spans &middot; ${a.contracts} golden &middot; ${a.deny} refused &middot; ${a.jobs} bq</p></div>`;
    }).join("");
  }

  if (deskAgents.length) {
    html += deskAgents.map((ag) => {
      const ch = channelMap[ag.agent_id] || channelMap[ag.name];
      const isAudit = ag.name === "memory_bank_audit_agent";
      const isHub = ag.name === "consumer_credit_synthesizer_agent";
      let badge = isAudit ? "ASYNC JOB" : (isHub ? "HUB · 7 SPOKES" : "LIVE");
      let tagClass = isAudit ? "" : "ok";
      let subline = escapeHtml(ag.role || "");
      if (ch) {
        badge = escapeHtml(ch.channel_label) + " · " + ch.session_count + " sess";
        subline = `${ch.session_count} sessions &middot; ${ch.event_count} events &middot; ${ch.memory_count} memories &middot; ${(ag.tools || []).length} tools`;
      } else if (isAudit) {
        const st = (STATE.auditSweep && STATE.auditSweep.status) || "IDLE";
        badge = "ASYNC · " + st;
        subline = "95s consolidated sweep kept off turn path &middot; trigger on demand";
      } else if (isHub) {
        const totSess = (summary && summary.snapshot && summary.snapshot.total_sessions) || 0;
        subline = `Synthesizes ${totSess} sessions across 5 channels + Dataplex glossary`;
      }
      return `<div class="item done">
        <div class="item-top"><span class="dot"></span>
        <span class="nm">${escapeHtml(ag.agent_id.replace(/_/g, " "))}</span>
        <span class="tag ${tagClass}">${badge}</span></div>
        <p class="rd">${subline}</p></div>`;
    }).join("");
  }

  if (!html) {
    rosterList.innerHTML = "<div class='item'><p class='rd'>Loading 8-agent desk &amp; 5 live channels…</p></div>";
    return;
  }

  rosterList.innerHTML = html;
  rosterList.querySelectorAll("[data-step]").forEach((el) => {
    el.addEventListener("click", () => {
      pick(Number(el.getAttribute("data-step")));
      setTrace(true);
    });
  });
}

/* Only non-ALLOW verdicts are traps. Each one names the aspect that decided it,
   so the panel can be checked against Dataplex rather than taken on trust. */
function renderGovernanceTraps(spans) {
  const box = $("govTrapsContainer");
  if (!box) return;
  const seen = new Set();
  const traps = [];
  spans.forEach((span) => {
    const o = span.outcome || {};
    const f = span.fields || {};
    if (!o.action || o.action === "ALLOW") return;
    const subject = f.table || f.column || f.sql || "unknown";
    const key = subject + "|" + o.action;
    if (seen.has(key)) return;
    seen.add(key);
    traps.push({ subject, action: o.action, evidence: o.evidence || "" });
  });

  if (!traps.length) {
    box.innerHTML = "<div class='item'><p class='rd'>n/r — no catalog refusals in this run.</p></div>";
    return;
  }
  box.innerHTML = traps.map((t) => {
    return `<div class="item done"><div class="item-top"><span class="dot"></span>
      <span class="nm">${escapeHtml(t.subject)}</span><span class="tag ok">${escapeHtml(t.action)}</span></div>
      <p class="rd"><code>${escapeHtml(t.evidence)}</code></p></div>`;
  }).join("");
}


  let idx = 0;

  function $(id) {
    return document.getElementById(id);
  }

  function escapeHtml(str) {
    return String(str == null ? "" : str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function ensureLiveBarDom() {
    const livebarIn = document.querySelector(".livebar-in");
    if (livebarIn && !$("tok")) {
      livebarIn.innerHTML =
        '<span class="pulse done" id="livePulse" aria-hidden="true"></span>' +
        '<div class="pickwrap">' +
        '  <button class="tok" id="tok" type="button" aria-expanded="false" aria-haspopup="listbox" aria-controls="menu">' +
        '    <span class="ix" id="tokIx">06</span>' +
        '    <span class="an" id="tokName">memory writer</span>' +
        '    <span class="cv" aria-hidden="true">▾</span>' +
        "  </button>" +
        '  <div class="menu" id="menu" role="listbox" aria-label="Choose an agent" hidden>' +
        '    <div class="menu-head"><span>Trace</span><span>Newest first</span></div>' +
        '    <div id="menuList"></div>' +
        '    <div class="menu-foot"><span>Selecting changes the trace. Use <b>Trace</b> to show or hide it.</span></div>' +
        "  </div>" +
        "</div>" +
        '<p class="barinfo" id="barinfo">' +
        '  <span class="bk" id="biKind"></span>' +
        '  <span class="bt" id="biText"></span>' +
        '  <span class="bm" id="biMetrics"></span>' +
        "</p>" +
        '<button class="barbtn" id="traceBtn" type="button" aria-expanded="false" aria-controls="drop">Trace ▾</button>' +
        '<span class="stamp" id="liveLogStamp">Waiting for trace...</span>';
    } else {
      const pulseEl = $("livePulse") || document.querySelector(".livebar .pulse");
      if (pulseEl && !pulseEl.id) pulseEl.id = "livePulse";
    }

    if (!$("drop")) {
      const legacyDrawer = $("drawer1");
      const dropEl = document.createElement("div");
      dropEl.className = "drop";
      dropEl.id = "drop";
      dropEl.hidden = true;
      dropEl.innerHTML =
        '<div class="drop-in">' +
        '  <p class="who">' +
        '    <span id="whoTxt"></span>' +
        '    <span class="stepnav">' +
        '      <button id="navNewer" type="button" title="Newer step">‹ newer</button>' +
        '      <button id="navOlder" type="button" title="Older step">older ›</button>' +
        "    </span>" +
        "  </p>" +
        '  <div id="body"></div>' +
        "</div>";
      if (legacyDrawer && legacyDrawer.parentNode) {
        legacyDrawer.parentNode.replaceChild(dropEl, legacyDrawer);
      } else {
        const livebar = document.querySelector(".livebar");
        if (livebar && livebar.parentNode) {
          livebar.parentNode.insertBefore(dropEl, livebar.nextSibling);
        }
      }
    }
  }

  function buildMenu() {
    const wrap = $("menuList");
    if (!wrap) return;
    wrap.innerHTML = "";
    AGENTS.forEach((a, n) => {
      const b = document.createElement("button");
      const isRunning = a.state === "running" || a.state === "run";
      b.className = "mi" + (isRunning ? " live" : "");
      b.type = "button";
      b.setAttribute("role", "option");
      b.setAttribute("aria-selected", String(n === idx));
      const right = isRunning
        ? "running"
        : a.state === "queue"
        ? "queued"
        : a.ms + (a.refusals ? ' · <span class="dots">' + a.refusals + " deny</span>" : "");
      b.innerHTML =
        '<span class="n">' +
        a.i +
        '</span><span class="nm">' +
        a.name +
        '</span><span class="rt">' +
        right +
        "</span>";
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        pick(n);
        closeMenu();
        setTrace(true);
        const tok = $("tok");
        if (tok) tok.focus();
      });
      wrap.appendChild(b);
    });
  }

  function headline(a) {
    const outs = a.spans.filter((s) => s[1] === "out");
    if (outs.length) return outs[outs.length - 1];
    const tools = a.spans.filter((s) => s[1] === "tool");
    if (tools.length) return tools[tools.length - 1];
    return a.spans[a.spans.length - 1];
  }

  function readout(a) {
    if (!a) return;
    const h = headline(a);
    const kind = $("biKind");
    const text = $("biText");
    const metrics = $("biMetrics");
    if (kind && h) {
      kind.className = "bk " + h[1];
      kind.textContent = h[1];
    }
    if (text && h) {
      text.className = "bt" + (h[1] === "deny" ? " deny" : "");
      text.textContent = h[2];
      text.title = h[2];
    }
    if (metrics) {
      const get = (label) => {
        const row = (a.foot || []).filter((f) => f[0] === label)[0];
        return row ? row[1] : "n/r";
      };
      const deny = get("refusals");
      const sql = get("ad-hoc sql");
      metrics.innerHTML =
        "<span>" +
        a.ms +
        "</span>" +
        "<span>tools <b>" +
        get("tools") +
        "</b></span>" +
        '<span class="' +
        (deny !== "0" ? "warn" : "") +
        '">deny <b>' +
        deny +
        "</b></span>" +
        '<span class="' +
        (sql === "0" ? "zero" : "warn") +
        '">sql <b>' +
        sql +
        "</b></span>";
    }
  }

  function renderTrace(a) {
    if (!a) return;
    const whoTxt = $("whoTxt");
    const body = $("body");
    if (whoTxt) {
      whoTxt.innerHTML = a.i + " · <b>" + a.name + "</b> · " + a.state + " · " + a.ms;
    }
    if (body) {
      let html = a.pending && (a.state === "running" || a.state === "run")
        ? '<p class="pending">' + a.pending + "</p>"
        : "";
      a.spans.forEach((s) => {
        html +=
          '<div class="span ' +
          s[1] +
          " " +
          s[4] +
          '">' +
          '<span class="ts">' +
          s[0] +
          '</span><span class="kind">' +
          s[1] +
          "</span>" +
          '<span class="what">' +
          s[2] +
          '</span><span class="ms">' +
          s[3] +
          "</span></div>";
      });
      html += '<div class="tfoot">';
      a.foot.forEach((f) => {
        html += "<span>" + f[0] + " <b>" + f[1] + "</b></span>";
      });
      html += "</div>";
      body.innerHTML = html;
    }
  }

  function pick(n) {
    if (n < 0 || n >= AGENTS.length) return;
    idx = n;
    const a = AGENTS[n];
    const tokIx = $("tokIx");
    const tokName = $("tokName");
    if (tokIx) tokIx.textContent = a.i;
    if (tokName) tokName.textContent = a.name;
    readout(a);
    document.querySelectorAll(".mi").forEach((m, j) => {
      m.setAttribute("aria-selected", String(j === n));
    });
    const navNewer = $("navNewer");
    const navOlder = $("navOlder");
    if (navNewer) navNewer.disabled = n === 0;
    if (navOlder) navOlder.disabled = n === AGENTS.length - 1;
    renderTrace(a);
  }

  function openMenu() {
    const menu = $("menu");
    const tok = $("tok");
    if (menu) menu.hidden = false;
    if (tok) tok.setAttribute("aria-expanded", "true");
  }

  function closeMenu() {
    const menu = $("menu");
    const tok = $("tok");
    if (menu) menu.hidden = true;
    if (tok) tok.setAttribute("aria-expanded", "false");
  }

  function setTrace(open) {
    const drop = $("drop");
    const traceBtn = $("traceBtn");
    if (drop) drop.hidden = !open;
    if (traceBtn) {
      traceBtn.setAttribute("aria-expanded", String(Boolean(open)));
      traceBtn.textContent = open ? "Trace ▴" : "Trace ▾";
    }
  }

  // Expose globally for direct console/test inspection
  window.AGENTS = AGENTS;
  window.buildMenu = buildMenu;
  window.headline = headline;
  window.readout = readout;
  window.pick = pick;
  window.renderTrace = renderTrace;
  window.openMenu = openMenu;
  window.closeMenu = closeMenu;
  window.setTrace = setTrace;

  function getLivePulseEl() {
    return $("livePulse") || document.querySelector(".livebar .pulse");
  }

  function setStampText(stampText) {
    const stampEl = $("liveLogStamp") || document.querySelector(".livebar .stamp");
    if (stampEl && stampText) {
      stampEl.textContent = stampText;
    }
  }

  function setAgentCountText(text) {
    const pill = $("agentCountPill");
    const colIdx = $("agentsColIndex");
    if (pill) pill.textContent = text;
    if (colIdx) colIdx.textContent = text;
  }

  function setRunMetaText(text) {
    const metaPill = $("runMetaPill");
    const eyebrow = document.querySelector(".runhead .eyebrow");
    if (metaPill) metaPill.textContent = text;
    if (eyebrow) eyebrow.textContent = text;
  }

  function setTruthEngineText(text) {
    const el = $("truthEngine");
    if (el) el.textContent = text;
  }

  // =========================================================================
  // 1b. SCENARIO PICKER (/api/scenarios)
  // The list is served by the backend, derived from AVENUE_PLANS. The UI holds
  // no copy of it: a picker that offers a preset the backend cannot run is a
  // demo failure, so there must be exactly one source of truth.
  // =========================================================================
  async function loadScenarios() {
    const sel = $("scenarioSelect");
    if (!sel) return;
    try {
      const resp = await fetch("/api/scenarios");
      const data = await resp.json();
      const list = data.scenarios || [];
      if (!resp.ok || !list.length) {
        sel.innerHTML = "<option value=''>n/r — scenarios unavailable</option>";
        return;
      }
      STATE.scenarios = list;
      STATE.scenario = data.default || list[0].id;
      sel.innerHTML = list
        .map(
          (s) =>
            `<option value="${escapeHtml(s.id)}"${s.id === STATE.scenario ? " selected" : ""}>` +
            `${escapeHtml(s.label)}</option>`
        )
        .join("");
      applyScenario(STATE.scenario, { rerun: false });
      sel.addEventListener("change", () => applyScenario(sel.value, { rerun: true }));
    } catch (err) {
      console.error("loadScenarios failed", err);
      sel.innerHTML = "<option value=''>n/r — scenarios unavailable</option>";
    }
  }

  /* Swaps the case framing to the chosen preset. The avenue list shown is the
     plan the backend will actually execute, not a description of it. */
  function applyScenario(scenarioId, opts) {
    const spec = (STATE.scenarios || []).filter((s) => s.id === scenarioId)[0];
    if (!spec) return;
    STATE.scenario = scenarioId;

    const lede = $("heroLede");
    if (lede) lede.textContent = spec.lede;

    // Retarget the preset question chips at the selected case.
    const chips = document.querySelectorAll(".ask-preset-chip");
    if (chips.length) {
      chips[0].setAttribute("data-q", spec.suggested_question);
      chips[0].textContent = spec.label;
    }
    const askInput = $("askInput");
    if (askInput) askInput.placeholder = spec.suggested_question;

    // Show the plan that will run, so the operator knows what to expect.
    const planned = (spec.avenues || [])
      .map((a) => a.probe_kind + " · " + a.probe_target)
      .join("  |  ");
    setRunMetaText(spec.avenues.length + " avenues planned · " + planned);

    if (opts && opts.rerun) {
      submitAskQuestion(spec.suggested_question);
    }
  }

  function openOpsDrawer() {
    const drawer = $("consoleDrawer");
    const btnOps = $("btnToggleOps");
    if (drawer) {
      drawer.style.display = "grid";
      drawer.classList.add("open");
    }
    if (btnOps) {
      btnOps.classList.add("active");
    }
  }

  function closeOpsDrawer() {
    const drawer = $("consoleDrawer");
    const btnOps = $("btnToggleOps");
    if (drawer) {
      drawer.style.display = "none";
      drawer.classList.remove("open");
    }
    if (btnOps) {
      btnOps.classList.remove("active");
    }
  }

  function toggleOpsDrawer() {
    const drawer = $("consoleDrawer");
    if (!drawer) return;
    const isHidden = drawer.style.display === "none" || !drawer.classList.contains("open");
    if (isHidden) {
      openOpsDrawer();
      loadLiveTestbed(STATE.customerId);
    } else {
      closeOpsDrawer();
    }
  }

  // =========================================================================
  // 2. REPLAY: RE-RUNS THE REAL PIPELINE (#btnReplayRun)
  // =========================================================================
  // The old version animated a hardcoded six-step script against DOM ids that
  // no longer exist, so it threw on the first click and left the button stuck.
  // This asks the backend to actually run the turn again and renders its trace.

  function clearReplayTimers() {
    STATE.replayTimers.forEach((t) => clearTimeout(t));
    STATE.replayTimers = [];
  }

  /* Reveals the already-completed steps one at a time. The sequencing is
     presentation; every duration shown is the one the backend measured. */
  function revealStepsInOrder() {
    clearReplayTimers();
    const total = AGENTS.length;
    if (!total) return;
    const stepDelayMs = 320;
    for (let n = total - 1; n >= 0; n--) {
      const order = total - 1 - n;
      const timer = setTimeout(() => {
        pick(n);
        setAgentCountText(order + 1 + " / " + total + " done");
      }, order * stepDelayMs);
      STATE.replayTimers.push(timer);
    }
  }

  async function runReplayAnimation() {
    if (STATE.replayRunning) return;
    STATE.replayRunning = true;
    clearReplayTimers();

    const btn = $("btnReplayRun");
    const livePulse = getLivePulseEl();
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Running pipeline...";
    }
    if (livePulse) livePulse.classList.remove("done");
    setRunMetaText("Running the desk against live GCP...");
    setStampText("in flight");

    const question =
      (STATE.lastQuestion && String(STATE.lastQuestion)) ||
      "Why does the credit card have no provisional credit date?";

    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          customer_id: DEFAULT_CUSTOMER_ID,
          preset_id: STATE.scenario || undefined,
          message: question,
        }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "HTTP " + resp.status);
      if (data.channels_snapshot) {
        STATE.channelsSummary = STATE.channelsSummary || {};
        STATE.channelsSummary.snapshot = data.channels_snapshot;
        renderChannelEventStream(data.channels_snapshot);
      }
      if (data.audit_sweep) {
        STATE.auditSweep = data.audit_sweep;
        renderAuditSweepState(data.audit_sweep);
      }
      await fetchAndRenderTrace(data.run_id);
      revealStepsInOrder();
      if (livePulse) livePulse.classList.add("done");
    } catch (err) {
      console.error("replay failed", err);
      setRunMetaText("Replay failed — " + err.message);
      setStampText("n/r");
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Replay run \u2192";
      }
      STATE.replayRunning = false;
    }
  }

  // =========================================================================
  // 3. LIVE GCP TESTBED & SCORECARD WIRING (/api/testbed, /api/eval-scorecard)
  // =========================================================================
  async function loadLiveTestbed(customerId) {
    const cid = customerId || DEFAULT_CUSTOMER_ID;
    const memListEl = $("liveMemoriesList");
    try {
      const resp = await fetch(`/api/testbed?customer_id=${encodeURIComponent(cid)}`);
      const data = await resp.json();
      STATE.testbedData = data;

      if (!resp.ok) {
        // A failed upstream call is shown, not hidden behind stale markup.
        if (memListEl) {
          memListEl.innerHTML = `<div class="bad">Testbed unavailable — ${escapeHtml(
            data.error || resp.status
          )}</div>`;
        }
        return;
      }

      // Backend key is vertex_memory_bank_live_memories, and it is provenance
      // wrapped. The old reads (vertex_memory_bank.memories / memories) never
      // matched anything, so this panel was always empty.
      const block = data.vertex_memory_bank_live_memories;
      const memories = pvValue(block);
      if (memListEl) {
        if (!Array.isArray(memories) || memories.length === 0) {
          memListEl.innerHTML = `<div><code>${NOT_RECORDED}</code> — no memories in scope ${escapeHtml(
            cid
          )} ${pvChip(block)}</div>`;
        } else {
          memListEl.innerHTML = memories
            .slice(0, 3)
            .map((m) => {
              const id = escapeHtml(m.memory_id || NOT_RECORDED);
              const fact = escapeHtml(m.fact || NOT_RECORDED);
              const when = escapeHtml(m.update_time || NOT_RECORDED);
              const topic = escapeHtml(m.topic || NOT_RECORDED);
              return `<div style="margin-bottom:4px;"><code>${id}</code> <span style="font-size:0.85em; background:var(--ink); color:var(--bg); padding:1px 4px; border-radius:2px; margin-right:4px;">${topic}</span>: ${fact} <small>${when}</small></div>`;
            })
            .join("") + `<div style="margin-top:6px;">${pvChip(block)}</div>`;
        }
      }
    } catch (err) {
      console.error("Testbed fetch failed:", err);
      if (memListEl) {
        memListEl.innerHTML = `<div class="bad">Testbed fetch failed — ${escapeHtml(
          err.message
        )}</div>`;
      }
    }
  }

  /** Finds one metric row inside a scorecard layer by its id. */
  function metricById(layer, id) {
    const rows = (layer && layer.metrics) || [];
    for (let i = 0; i < rows.length; i++) {
      if (rows[i] && rows[i].id === id) return rows[i];
    }
    return null;
  }

  async function loadEvalScorecard() {
    const fineEl = $("scoreFineNote");
    try {
      const resp = await fetch("/api/eval-scorecard");
      const data = await resp.json();
      STATE.scorecardData = data;

      if (!resp.ok) {
        if (fineEl) {
          fineEl.innerHTML = `Scorecard unavailable — ${escapeHtml(data.error || resp.status)}`;
        }
        return;
      }

      // Every row reads {value, _provenance}. A metric the harness did not
      // record arrives as "n/r" and is drawn as "n/r" — never as 0, and never
      // recomputed here to make the row look populated.
      const l1 = data.layer_1_memory || {};
      const leakage = metricById(l1, "cross_scope_leakage");
      const contradiction = metricById(l1, "staleness_penalty");
      const recall = metricById(l1, "recall_at_k");

      renderMetric($("scoreLeakage"), leakage && leakage.display);
      renderMetric($("scoreContradictions"), contradiction && contradiction.display);
      renderMetric($("scoreRecall"), recall && recall.display);

      const fill = $("scoreRecallFill");
      if (fill) {
        const width = recall ? pvValue(recall.width_pct) : NOT_RECORDED;
        // No bar at all when there is no measurement. A 0%-wide bar reads as a
        // score of zero, which is a different claim from "not measured".
        fill.style.width = typeof width === "number" ? `${width}%` : "0%";
        fill.style.opacity = typeof width === "number" ? "1" : "0.25";
      }
      const recallRow = $("scoreRecallRow");
      if (recallRow && recall) {
        const measured = pvValue(recall.width_pct) !== NOT_RECORDED;
        recallRow.classList.toggle("skip", !measured);
      }

      if (fineEl) {
        const srcOn = (data.sources && data.sources.memory_on) || {};
        const err = srcOn.error && srcOn.error !== NOT_RECORDED ? ` — ${escapeHtml(srcOn.error)}` : "";
        fineEl.innerHTML =
          `Every figure above carries the tag of where it came from. Source: <code>${escapeHtml(
            srcOn.file || NOT_RECORDED
          )}</code>${err}. Anything reading <code>${NOT_RECORDED}</code> was not measured, ` +
          `which is not the same as scoring zero.`;
      }

      renderAbComparison(data.ab_comparison);
    } catch (err) {
      console.error("Scorecard fetch failed:", err);
      if (fineEl) {
        fineEl.innerHTML = `Scorecard fetch failed — ${escapeHtml(err.message)}`;
      }
    }
  }

  function renderAbComparison(ab) {
    const host = $("abComparison");
    const foot = $("abFootnote");
    if (!host) return;

    if (!ab || !Array.isArray(ab.measured) || !ab.measured.length) {
      host.innerHTML =
        '<p class="fine">No off-vs-on comparison in the scorecard payload.</p>';
      if (foot) foot.textContent = "";
      return;
    }

    // A score of 1.0 measured over 0 turns is not a pass, it is an absence.
    // With memory off, cross_scope_leakage scores 1.0 on 0 of 32 turns purely
    // because nothing was retrieved, so nothing could leak. Showing that as a
    // green tick next to the memory-on tick would invert the result.
    const cell = (side) => {
      if (!side) return '<td class="none">n/r</td>';
      const score = pvText(side.score);
      const vacuous = pvText(side.vacuous) === true || pvText(side.vacuous) === "true";
      const measured = pvText(side.turns_measured);
      const total = pvText(side.turns_total);
      if (score === NOT_RECORDED) return '<td class="none">n/r</td>';

      const pct = `${Math.round(Number(score) * 100)}%`;
      if (vacuous) {
        return `<td class="none" title="scored on 0 turns - nothing was retrieved, so nothing could be measured">${pct} <small>(0 turns)</small></td>`;
      }
      const cls = Number(score) >= 1 ? "ok" : "none";
      return `<td class="${cls}" title="${escapeHtml(
        `${measured} of ${total} turns measured · ${pvSource(side.score)}`
      )}">${pct}</td>`;
    };

    host.innerHTML = `
      <table class="kv" style="width:100%;border-collapse:collapse;">
        <thead>
          <tr>
            <th style="text-align:left;">Measured</th>
            <th style="text-align:right;">Memory off</th>
            <th style="text-align:right;">Memory on</th>
          </tr>
        </thead>
        <tbody>
          ${ab.measured
            .map(
              (m) =>
                `<tr><td>${escapeHtml(m.name)}</td>${cell(m.memory_off)}${cell(
                  m.memory_on
                )}</tr>`
            )
            .join("")}
        </tbody>
      </table>`;

    // The panel used to end with "68.8% fewer tokens" whether or not anything
    // had been measured. The harness says plainly that it measured no tokens;
    // that sentence is shown instead of a number nobody recorded.
    if (foot) {
      const why = pvText(ab.why_not_measured);
      const how = pvText(ab.how_to_measure);
      foot.innerHTML =
        `Tokens per session and questions re-asked are <b>not measured</b>. ` +
        `${escapeHtml(why === NOT_RECORDED ? "" : why)} ` +
        `<i>${escapeHtml(how === NOT_RECORDED ? "" : how)}</i>`;
    }
  }

  // =========================================================================
  // 4. ASK CONSOLE & MULTI-AGENT CHAT + RELEVANT-ONLY VERACITY AUDIT
  // =========================================================================
  async function submitAskQuestion(questionText) {
    const query = String(questionText || "").trim();
    if (!query) return;

    const customerId = DEFAULT_CUSTOMER_ID;
    const channel = STATE.channel || "WEB_PORTAL";
    STATE.lastQuestion = query; // so Replay re-runs what the user actually asked

    const askBtn = $("btnAskSend");
    if (askBtn) {
      askBtn.disabled = true;
      askBtn.textContent = "Working...";
    }

    openOpsDrawer();
    // Was openTrace("1","policy") — a function that does not exist anywhere in
    // this bundle. It threw a ReferenceError here, so the /api/chat fetch below
    // never ran and the console appeared to hang on every single question.
    setTrace(true);
    setStampText("WORKING");

    if ($("askAnswerTitle")) {
      $("askAnswerTitle").textContent = `Working case · ${customerId}`;
    }
    if ($("askAnswerTag")) {
      $("askAnswerTag").className = "tag on";
      $("askAnswerTag").textContent = "RUNNING";
    }
    if ($("askAnswerBody")) {
      $("askAnswerBody").innerHTML =
        "Querying BigQuery golden queries, the Dataplex glossary, and Vertex AI " +
        "Memory Bank...";
      // The engine id, dataset and glossary are deployment config. The page used
      // to print one project's literals here, which read as status but was just
      // a string. /api/session returns the live status if they need showing.
    }

    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          customer_id: customerId,
          channel: channel,
          preset_id: STATE.scenario || undefined,
          message: query,
        }),
      });

      const data = await resp.json();

      if (!resp.ok) {
        // 502 and 503 carry the real upstream reason. Show it.
        if ($("askAnswerTag")) {
          $("askAnswerTag").className = "tag no";
          $("askAnswerTag").textContent = String(data.status || resp.status);
        }
        if ($("askAnswerBody")) {
          $("askAnswerBody").innerHTML = `<span style="color:var(--bad);">${escapeHtml(
            data.error || "Request failed"
          )}</span>${data.needs ? ` <small>(needs ${escapeHtml(data.needs)})</small>` : ""}`;
        }
        return;
      }

      if (data.channels_snapshot) {
        STATE.channelsSummary = STATE.channelsSummary || {};
        STATE.channelsSummary.snapshot = data.channels_snapshot;
        renderChannelEventStream(data.channels_snapshot);
      }
      if (data.audit_sweep) {
        STATE.auditSweep = data.audit_sweep;
        renderAuditSweepState(data.audit_sweep);
      }
      if (data.run_id) {
        fetchAndRenderTrace(data.run_id);
      }

      if ($("askAnswerTitle")) {
        $("askAnswerTitle").textContent = `Governed response · ${customerId}`;
      }
      if ($("askAnswerTag")) {
        $("askAnswerTag").className = "tag ok";
        $("askAnswerTag").textContent = "CATALOG + MEMORY";
      }
      if ($("askAnswerBody")) {
        // The narrative is streamed, not returned by /api/chat. This route's
        // agent_resolution says so and carries its own provenance.
        const resolution = data.agent_resolution;
        $("askAnswerBody").innerHTML =
          pvValue(resolution) === NOT_RECORDED
            ? `<code>${NOT_RECORDED}</code> — narrative arrives on <code>/api/chat/stream</code>. ${pvChip(
                resolution
              )}`
            : formatAgentReply(pvText(resolution));
      }

      // Backend key is tool_trajectory. The old read of data.tool_sequence
      // never matched, so the UI drew a hardcoded four-tool list on every
      // answer whether or not the desk had said anything about tools.
      const steps = Array.isArray(data.tool_trajectory) ? data.tool_trajectory : [];
      if ($("askToolTags")) {
        $("askToolTags").innerHTML = steps.length
          ? steps
              .map(
                (s, idx) =>
                  `<span class="tag ${idx === steps.length - 1 ? "on" : "ok"}" title="status ${escapeHtml(
                    pvText(s.status)
                  )} · ${escapeHtml(pvSource(s.tool))}">${idx + 1}. ${escapeHtml(
                    pvText(s.tool)
                  )} · ${escapeHtml(pvText(s.status))}</span>`
              )
              .join("")
          : `<span class="tag">${NOT_RECORDED}</span>`;
      }

      renderVeracityAudit(data.veracity_evaluation);
      subscribeToSynthesisStream(customerId, query);
    } catch (err) {
      if ($("askAnswerBody")) {
        $("askAnswerBody").innerHTML = `<span style="color:var(--bad);">Error communicating with desk: ${escapeHtml(
          err.message
        )}</span>`;
      }
    } finally {
      if (askBtn) {
        askBtn.disabled = false;
        askBtn.textContent = "Send →";
      }
    }
  }

  function formatAgentReply(rawText) {
    const escaped = escapeHtml(rawText);
    return escaped
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\n\n/g, "<br><br>")
      .replace(/\n/g, "<br>");
  }

  function renderVeracityAudit(audit) {
    const tagEl = $("veracityStatusTag") || $("veracityStatusBadge");
    const outEl = $("veracityAuditOutput");
    if (!audit) {
      if (tagEl) {
        tagEl.className = "tag";
        tagEl.textContent = NOT_RECORDED;
      }
      if (outEl) outEl.innerHTML = `<div><code>${NOT_RECORDED}</code> — no audit returned.</div>`;
      return;
    }

    // Status and confidence are the validator's. Neither is defaulted here:
    // the old code fell back to VERIFIED_TRUE and 98%, which meant a broken
    // validator rendered as a confident pass.
    const status = pvText(audit.veracity_status);
    const rawConf = pvValue(audit.confidence_score);
    const confText =
      typeof rawConf === "number"
        ? `${Math.round(rawConf * (rawConf <= 1 ? 100 : 1))}%`
        : NOT_RECORDED;

    if (tagEl) {
      const isContradicted = status.includes("CONTRADICTED") || status.includes("FALSE");
      const unknown = status === NOT_RECORDED;
      tagEl.className = `tag ${unknown ? "" : isContradicted ? "no" : "ok"}`;
      tagEl.textContent = `${status} · ${confText}`;
      tagEl.title = `${pvTag(audit.veracity_status)} · ${pvSource(audit.veracity_status)}`;
    }

    // Backend keys are relevant_avenues_checked / avenue_title / finding_summary.
    // The old reads (av.avenue, av.finding, av.status) matched none of them, so
    // every avenue rendered as a blank line labelled CHECKED.
    const avenues = audit.relevant_avenues_checked || [];
    if (!outEl) return;

    if (!avenues.length) {
      const why = audit.error && audit.error !== NOT_RECORDED ? ` — ${escapeHtml(audit.error)}` : "";
      outEl.innerHTML = `<div><code>${NOT_RECORDED}</code> — no avenue was probed${why}.</div>`;
      return;
    }

    outEl.innerHTML = avenues
      .map((av, i) => {
        const title = escapeHtml(av.avenue_title || av.avenue_id || `Avenue 0${i + 1}`);
        const finding = escapeHtml(pvText(av.finding_summary));
        const rows = escapeHtml(pvText(av.row_count));
        const why = av.why_relevant ? ` <em>(${escapeHtml(av.why_relevant)})</em>` : "";
        const err = av.error ? ` <span style="color:var(--bad);">${escapeHtml(av.error)}</span>` : "";
        return (
          `<div style="margin-bottom:6px;"><b>${i + 1}. ${title}</b> ` +
          pvChip(av.finding_summary) +
          `<div>rows <b>${rows}</b> — ${finding}${why}${err}</div></div>`
        );
      })
      .join("");
  }

  // =========================================================================
  // 4b. A2UI SYNTHESIS STREAM (/api/chat/stream)
  // The backend has always served these frames and nothing subscribed, so the
  // zero-question causal synthesis never reached the page.
  // Frames: onAgentThought, onMemoryBankAccess, onAgentSynthesis,
  //         onUiComponentDelivery.
  // =========================================================================
  function appendStreamLine(text, kind) {
    const list = $("eventStreamList");
    if (!list) return;
    const li = document.createElement("li");
    li.className = kind || "";
    li.innerHTML = text;
    list.appendChild(li);
  }

  function subscribeToSynthesisStream(customerId, prompt) {
    if (typeof EventSource === "undefined") return;
    if (STATE.stream) {
      STATE.stream.close();
      STATE.stream = null;
    }
    const list = $("eventStreamList");
    if (list) list.innerHTML = "";

    const url =
      "/api/chat/stream?customer_id=" +
      encodeURIComponent(customerId) +
      "&prompt=" +
      encodeURIComponent(prompt);
    const source = new EventSource(url);
    STATE.stream = source;

    source.onmessage = (event) => {
      let frame;
      try {
        frame = JSON.parse(event.data);
      } catch (err) {
        appendStreamLine(`<b>malformed frame</b> — ${escapeHtml(event.data)}`, "bad");
        return;
      }
      const method = frame.method || NOT_RECORDED;
      const params = frame.params || {};

      if (method === "onAgentThought") {
        appendStreamLine(
          `<b>${escapeHtml(pvText(params.author))}</b> — ${escapeHtml(params.message || NOT_RECORDED)}`
        );
      } else if (method === "onMemoryBankAccess") {
        appendStreamLine(
          `<b>memory</b> — ${escapeHtml(pvText(params.memory_count))} memory(ies) ${pvChip(
            params.memories
          )}`
        );
      } else if (method === "onAgentSynthesis") {
        const narrative = params.narrative;
        appendStreamLine(`<b>synthesis</b> ${pvChip(narrative)}`);
        if ($("askAnswerBody")) {
          $("askAnswerBody").innerHTML =
            pvValue(narrative) === NOT_RECORDED
              ? `<code>${NOT_RECORDED}</code> — the model returned no narrative. ${pvChip(narrative)}`
              : formatAgentReply(pvText(narrative));
        }
      } else if (method === "onUiComponentDelivery") {
        renderSynthesisCard(params.payload);
        appendStreamLine("<b>a2ui</b> — synthesis card delivered");
        source.close();
        STATE.stream = null;
        setStampText("stream complete");
      }
    };

    source.onerror = () => {
      appendStreamLine("<b>stream closed</b>", "bad");
      source.close();
      STATE.stream = null;
    };
  }

  function renderSynthesisCard(card) {
    if (!card) return;
    if ($("caseCard1Title")) {
      $("caseCard1Title").textContent = card.title || NOT_RECORDED;
    }
    if ($("caseCard1Sub")) {
      $("caseCard1Sub").innerHTML =
        `${escapeHtml(pvText(card.memory_count))} memory(ies) · confidence ` +
        `${escapeHtml(pvText(card.confidence_score))} ${pvChip(card.confidence_score)}`;
    }
    if ($("caseCard1Kv")) {
      const timeline = Array.isArray(card.timeline) ? card.timeline : [];
      $("caseCard1Kv").innerHTML = timeline.length
        ? timeline
            .map(
              (t) =>
                `<div class="kv"><span>${escapeHtml(t.timestamp || NOT_RECORDED)}</span>` +
                `<b>${escapeHtml(t.title || NOT_RECORDED)}</b></div>`
            )
            .join("")
        : `<div class="kv"><span>${NOT_RECORDED}</span><b>no causal steps</b></div>`;
    }
  }

  // =========================================================================
  // 5. ADMIN CARD RESTRICTION / UNLOCK OVERSIGHT & MEMORY COMPACTION
  // =========================================================================
  async function loadCardOversight(customerId) {
    const cid = customerId || DEFAULT_CUSTOMER_ID;
    try {
      const resp = await fetch(`/api/card-oversight?customer_id=${encodeURIComponent(cid)}`);
      const data = await resp.json();
      if (!resp.ok) {
        const badge = $("oversightCardStatusBadge");
        if (badge) {
          badge.className = "tag no";
          badge.textContent = String(data.status || resp.status);
        }
        const summary = $("oversightVerificationSummary");
        if (summary) summary.textContent = data.error || `HTTP ${resp.status}`;
        return;
      }
      updateOversightUI(data);
    } catch (err) {
      console.error("Oversight fetch failed:", err);
    }
  }

  function updateOversightUI(data) {
    const badge = $("oversightCardStatusBadge");
    const summary = $("oversightVerificationSummary");
    if (!data) return;

    // Only card_status is emitted. Falling back to "RESTRICTED" made a missing
    // field look like a locked card, which is a claim about the account.
    const stateStr = data.card_status ? String(data.card_status).toUpperCase() : NOT_RECORDED;
    if (badge) {
      if (stateStr === NOT_RECORDED) {
        badge.className = "tag";
        badge.textContent = NOT_RECORDED;
      } else {
        const isUnlocked = stateStr.includes("UNLOCK") || stateStr.includes("ACTIVE");
        badge.className = `tag ${isUnlocked ? "ok" : "on"}`;
        badge.textContent = isUnlocked ? "UNLOCKED · ACTIVE" : "RESTRICTED";
      }
    }

    // Backend key is admin_review_case, not summary.
    const c = data.admin_review_case;
    if (summary) {
      if (!c) {
        summary.textContent = NOT_RECORDED;
      } else {
        const score = c.confidence_score === null || c.confidence_score === undefined
          ? NOT_RECORDED
          : c.confidence_score;
        summary.textContent =
          `${c.flagged_transaction || NOT_RECORDED} (${c.flagged_amount || NOT_RECORDED}) · ` +
          `confidence ${score} · ` +
          `${c.verification_passed ? "verification passed" : "verification not passed"} · ` +
          `recommends ${c.recommended_admin_decision || NOT_RECORDED}`;
      }
    }
  }

  async function submitCardDecision(decision) {
    const btnUnlock = $("btnAdminUnlock");
    const btnRestrict = $("btnAdminRestrict");
    if (btnUnlock) btnUnlock.disabled = true;
    if (btnRestrict) btnRestrict.disabled = true;

    const notes =
      decision === "APPROVE_UNLOCK"
        ? "Supervisor approved the unlock from the oversight card."
        : "Supervisor kept the restriction in place from the oversight card.";

    try {
      const resp = await fetch("/api/card-oversight/decide", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          customer_id: DEFAULT_CUSTOMER_ID,
          decision: decision,
          // The route reads admin_notes or reason. "supervisor_notes" was read
          // by nothing, so the supervisor's reason never reached the case file.
          admin_notes: notes,
        }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        const badge = $("oversightCardStatusBadge");
        if (badge) {
          badge.className = "tag no";
          badge.textContent = String(data.status || resp.status);
        }
        console.error("Card oversight decision failed:", data);
        return;
      }
      updateOversightUI(data);
      setStampText("SUPERVISOR LOGGED");
    } catch (err) {
      console.error("Card oversight decision error:", err);
    } finally {
      if (btnUnlock) btnUnlock.disabled = false;
      if (btnRestrict) btnRestrict.disabled = false;
    }
  }

  async function runMemoryCompaction() {
    const btn = $("btnRunCompaction");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Triggering consolidation...";
    }

    try {
      const resp = await fetch("/api/compaction/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ customer_id: DEFAULT_CUSTOMER_ID }),
      });
      const data = await resp.json();

      if (!resp.ok) {
        if (btn) btn.textContent = `✗ ${data.status || resp.status}`;
        console.error("Compaction failed:", data);
        return;
      }

      // Consolidation runs inside Memory Bank. It does not report a token count,
      // so the button shows "n/r" rather than the 68.8% this used to invent on
      // every click, success or failure.
      const report = data.compaction_report || {};
      const pct = pvText(report.token_reduction_pct);
      const before = pvText(report.memories_in_scope_before);
      if (btn) {
        btn.textContent = `Consolidation requested · ${before} in scope · saved ${pct}`;
        btn.title = `${pvTag(report.token_reduction_pct)} · ${pvSource(report.token_reduction_pct)}`;
      }
      await loadLiveTestbed(DEFAULT_CUSTOMER_ID);
    } catch (err) {
      console.error("Compaction error:", err);
      if (btn) btn.textContent = `✗ ${err.message}`;
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // =========================================================================
  // INITIALIZATION & EVENT LISTENERS
  // =========================================================================
  // =========================================================================
  // Deployment identity (/api/session)
  //
  // The engine id, dataset and glossary used to be literals in index.html, so
  // the page displayed one project's ids no matter which project the backend
  // was talking to -- and displayed them just as confidently when the backend
  // was unreachable. They are read from the API now, and a failed read says
  // so instead of leaving the stale value on screen.
  // =========================================================================
  async function loadDeploymentIdentity(customerId) {
    const cid = customerId || DEFAULT_CUSTOMER_ID;
    const set = (id, text, title) => {
      const el = $(id);
      if (!el) return;
      el.textContent = text;
      if (title) el.title = title;
    };

    try {
      const resp = await fetch(`/api/session?customer_id=${encodeURIComponent(cid)}`);
      const data = await resp.json();
      if (!resp.ok) {
        set("engineIdTag", "engine: unavailable", String(data.error || resp.status));
        return;
      }

      const engine = pvText(data.agent_engine_id);
      const project = pvText(data.project_id);
      const dataset = pvText(data.dataset);
      const glossary = pvText(data.glossary);
      const mbStatus = pvText(data.memory_bank_status);
      const sessions = pvText(data.open_session_count);

      const online = String(mbStatus).startsWith("ONLINE");
      const tagEl = $("engineIdTag");
      if (tagEl) tagEl.className = online ? "tag ok" : "tag no";
      set(
        "engineIdTag",
        online ? engine : `${engine} · ${mbStatus}`,
        `projects/${project}/locations/*/reasoningEngines/${engine}\n${pvTag(
          data.memory_bank_status
        )} · ${pvSource(data.memory_bank_status)}`
      );

      set("noteEngine", engine);
      set("noteDataset", dataset);
      set("noteGlossary", glossary);
      set(
        "noteCounts",
        `${sessions} session(s) on the engine, ${pvText(
          data.knowledge_catalog_status
        )} catalog`,
        pvSource(data.open_session_count)
      );
    } catch (err) {
      set("engineIdTag", "engine: unreachable", err.message);
    }
  }

  function init() {
    ensureLiveBarDom();

    // 1. Trace will be fetched on load.
    const livePulse = getLivePulseEl();
    if (livePulse) {
      livePulse.classList.add("done");
    }

    // 2. Build 6-Agent Menu & Wire Livebar / Drop Trace Controls
    buildMenu();
    pick(0);
    setTrace(false);

    const tok = $("tok");
    const menu = $("menu");
    const traceBtn = $("traceBtn");
    const navNewer = $("navNewer");
    const navOlder = $("navOlder");

    if (tok && !tok.dataset.bound) {
      tok.dataset.bound = "1";
      tok.addEventListener("click", (e) => {
        e.stopPropagation();
        const menuEl = $("menu");
        if (menuEl && menuEl.hidden) openMenu();
        else closeMenu();
      });
    }
    if (traceBtn && !traceBtn.dataset.bound) {
      traceBtn.dataset.bound = "1";
      traceBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        const dropEl = $("drop");
        setTrace(dropEl ? dropEl.hidden : true);
      });
    }
    if (navNewer && !navNewer.dataset.bound) {
      navNewer.dataset.bound = "1";
      navNewer.addEventListener("click", (e) => {
        e.stopPropagation();
        if (idx > 0) pick(idx - 1);
      });
    }
    if (navOlder && !navOlder.dataset.bound) {
      navOlder.dataset.bound = "1";
      navOlder.addEventListener("click", (e) => {
        e.stopPropagation();
        if (idx < AGENTS.length - 1) pick(idx + 1);
      });
    }
    document.addEventListener("click", (e) => {
      const menuEl = $("menu");
      if (menuEl && !menuEl.hidden && !menuEl.contains(e.target)) {
        closeMenu();
      }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeMenu();
    });


    // Deep-link support (#menu, #trace, #agent=...)
    const h = window.location.hash;
    if (h === "#menu") {
      openMenu();
    } else if (h === "#trace") {
      setTrace(true);
    } else if (h.indexOf("#agent=") === 0) {
      const want = h.slice(7);
      AGENTS.forEach((a, n) => {
        if (a.k === want) {
          pick(n);
          setTrace(true);
        }
      });
    }

    // 3. Replay Run button (#btnReplayRun)
    const btnReplay = $("btnReplayRun");
    if (btnReplay) {
      btnReplay.addEventListener("click", runReplayAnimation);
    }

    // 4. Operations drawer toggle (#btnToggleOps & #btnCloseDrawer)
    const btnOps = $("btnToggleOps");
    if (btnOps) {
      btnOps.addEventListener("click", toggleOpsDrawer);
    }
    const btnClose = $("btnCloseDrawer");
    if (btnClose) {
      btnClose.addEventListener("click", closeOpsDrawer);
    }

    // 5. Bottom persistent Ask Console (#askInput + #btnAskSend or #console + #ask)
    const askInput = $("askInput") || $("ask");
    const btnAskSend = $("btnAskSend");
    const consoleForm = $("console");
    if (btnAskSend && askInput) {
      btnAskSend.addEventListener("click", () => {
        const q = askInput.value.trim() || askInput.getAttribute("placeholder") || "";
        submitAskQuestion(q);
      });
      askInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          const q = askInput.value.trim() || askInput.getAttribute("placeholder") || "";
          submitAskQuestion(q);
        }
      });
    } else if (consoleForm && askInput) {
      consoleForm.addEventListener("submit", (e) => {
        e.preventDefault();
        const q = askInput.value.trim() || askInput.getAttribute("placeholder") || "";
        submitAskQuestion(q);
      });
    }

    // 6. Preset question chips inside drawer (.ask-preset-chip)
    document.querySelectorAll(".ask-preset-chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        const q = chip.getAttribute("data-q") || "";
        if (askInput) askInput.value = q;
        submitAskQuestion(q);
      });
    });

    // 7. Admin Card Unlock / Restrict buttons (#btnAdminUnlock, #btnAdminRestrict)
    const btnUnlock = $("btnAdminUnlock");
    if (btnUnlock) {
      btnUnlock.addEventListener("click", () => submitCardDecision("APPROVE_UNLOCK"));
    }
    const btnRestrict = $("btnAdminRestrict");
    if (btnRestrict) {
      btnRestrict.addEventListener("click", () => submitCardDecision("KEEP_RESTRICTED"));
    }

  function renderChannelEventStream(snapshot) {
    const streamList = $("eventStreamList");
    const badge = $("eventStreamColBadge");
    if (!streamList || !snapshot) return;

    const totalSessions = snapshot.total_sessions || 0;
    const totalEvents = snapshot.total_events || 0;
    if (badge) {
      badge.textContent = `04 / ${totalSessions} sess · ${totalEvents} events · 5 channels`;
    }

    const events = Array.isArray(snapshot.events) ? snapshot.events : [];
    if (!events.length) {
      streamList.innerHTML =
        "<li><span class='t'>0</span><span class='e'>No channel sessions recorded for this scope.</span></li>";
      return;
    }

    streamList.innerHTML = events.slice(0, 12).map((ev) => {
      const ts = String(ev.timestamp || "").replace(/^.*T/, "").slice(0, 8) || "live";
      const ch = escapeHtml(ev.channel || "CHANNEL");
      const sid = escapeHtml(ev.session_id || "");
      const txt = escapeHtml(ev.text || "");
      return `<li>
        <span class="t">${escapeHtml(ts)}</span>
        <span class="e"><span class="tag ok" style="margin-right:5px;">${ch}</span><code>${sid}</code> &middot; ${txt}</span>
      </li>`;
    }).join("");
  }

  let _auditPollTimer = null;

  function renderAuditSweepState(sweep) {
    if (!sweep) return;
    STATE.auditSweep = sweep;
    const tag = $("auditSweepStatusTag");
    const out = $("auditSweepOutput");
    const btn = $("btnTriggerAuditSweep");

    const status = sweep.status || "IDLE";
    if (tag) {
      tag.textContent = "ASYNC · " + status;
      tag.className = status === "DONE" ? "tag ok" : (status === "RUNNING" ? "tag on" : "tag");
    }
    if (status === "RUNNING") {
      if (btn) {
        btn.disabled = true;
        btn.textContent = "⏳ Running 95s sweep in background…";
      }
      if (out) {
        out.textContent = `Job ${sweep.job_id || ""} started at ${sweep.started_at || ""} — running off the chat path…`;
      }
      if (!_auditPollTimer) {
        _auditPollTimer = setInterval(async () => {
          try {
            const r = await fetch("/api/audit-sweep/status");
            if (!r.ok) return;
            const st = await r.json();
            renderAuditSweepState(st);
            renderRoster();
            if (st.status !== "RUNNING" && _auditPollTimer) {
              clearInterval(_auditPollTimer);
              _auditPollTimer = null;
            }
          } catch (e) {
            console.error("audit poll failed", e);
          }
        }, 4000);
      }
    } else {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "▶ Run 95s audit sweep (async)";
      }
      if (status === "DONE" && out) {
        const secs = sweep.elapsed_ms ? (sweep.elapsed_ms / 1000).toFixed(1) + "s" : "n/r";
        const rep = sweep.report || {};
        const anomalies = Array.isArray(rep.anomalies_detected) ? rep.anomalies_detected.length : (rep.total_anomalies || 0);
        out.textContent = `Sweep complete in ${secs} · ${anomalies} anomalies reported across enterprise Memory Bank.`;
      } else if (status === "ERROR" && out) {
        out.textContent = `Sweep error: ${sweep.error || "failed"}`;
      }
    }
  }

  async function loadChannelsAndRosterSummary(customerId) {
    const cid = customerId || DEFAULT_CUSTOMER_ID;
    try {
      const resp = await fetch(`/api/channels/summary?customer_id=${encodeURIComponent(cid)}`);
      if (!resp.ok) return;
      const data = await resp.json();
      STATE.channelsSummary = data;
      if (data.snapshot) {
        renderChannelEventStream(data.snapshot);
      }
      if (data.audit_sweep) {
        renderAuditSweepState(data.audit_sweep);
      }
      renderRoster();

      // If no chat run has been executed yet in this server process, hydrate the
      // livebar and header with the live 8-agent + 5-channel state instead of
      // leaving "Waiting for trace...".
      if (!AGENTS.length && data.snapshot) {
        const totSess = data.snapshot.total_sessions || 0;
        const totEv = data.snapshot.total_events || 0;
        setAgentCountText("8 agents · 5 channels");
        setRunMetaText(`8-agent hub-and-spoke desk · ${totSess} live sessions (${totEv} events) across 5 channels`);
        setStampText(`${totSess} sessions · 5 channels live`);
        setTruthEngineText(
          `8-agent desk ready · FRAUD_OPS_OUTBOUND, IVR, MOBILE_APP, WEB_CHAT, BRANCH (${totSess} sessions in Memory Bank)`
        );
        const biKind = $("biKind");
        const biText = $("biText");
        const biMetrics = $("biMetrics");
        if (biKind) biKind.textContent = "HUB";
        if (biText) biText.textContent = `consumer_credit_synthesizer_agent + 7 spokes · ${totSess} channel sessions`;
        if (biMetrics) biMetrics.textContent = "click Replay run → to trace a turn";
      }
    } catch (err) {
      console.error("loadChannelsAndRosterSummary failed", err);
    }
  }

  async function injectLiveChannelEvent() {
    const sel = $("channelIngestSelect");
    const txt = $("channelIngestText");
    const statusEl = $("channelIngestStatus");
    const btn = $("btnInjectChannelEvent");
    const channel = (sel && sel.value) || "FRAUD_OPS_OUTBOUND";
    const text = (txt && txt.value.trim()) || `Live ${channel} event recorded`;

    if (btn) btn.disabled = true;
    if (statusEl) statusEl.textContent = `Writing live ${channel} session to Vertex AI Memory Bank…`;
    try {
      const resp = await fetch(`/api/channel/${encodeURIComponent(channel)}/event`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          customer_id: DEFAULT_CUSTOMER_ID,
          text: text,
          intent: "LIVE_OPERATOR_INJECTION",
        }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "HTTP " + resp.status);
      if (data.snapshot) {
        STATE.channelsSummary = STATE.channelsSummary || {};
        STATE.channelsSummary.snapshot = data.snapshot;
        renderChannelEventStream(data.snapshot);
        renderRoster();
      }
      if (data.run_id) {
        await fetchAndRenderTrace(data.run_id);
      }
      const sid = pvText(data.ingested && data.ingested.session_id);
      if (statusEl) {
        statusEl.textContent = `Ingested ${channel} session ${sid} into Memory Bank · producer updated (${data.snapshot ? data.snapshot.total_sessions : "n/r"} total sessions).`;
      }
    } catch (err) {
      if (statusEl) statusEl.textContent = `Channel ingest failed: ${err.message}`;
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function triggerFraudCardControl() {
    const txt = $("channelIngestText");
    const statusEl = $("channelIngestStatus");
    const btn = $("btnFraudCardControl");
    const reason = (txt && txt.value.trim()) || "Velocity + impossible-travel anomaly across channels";

    if (btn) btn.disabled = true;
    if (statusEl) statusEl.textContent = "Running fraud_monitoring_agent (Golden 03 + 04 + FRAUD_OPS_OUTBOUND action)…";
    try {
      const resp = await fetch("/api/fraud/card-control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          customer_id: DEFAULT_CUSTOMER_ID,
          action: "CARD_BLOCK_AND_ESCALATE",
          reason: reason,
        }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "HTTP " + resp.status);
      if (data.snapshot) {
        STATE.channelsSummary = STATE.channelsSummary || {};
        STATE.channelsSummary.snapshot = data.snapshot;
        renderChannelEventStream(data.snapshot);
        renderRoster();
      }
      if (data.run_id) {
        await fetchAndRenderTrace(data.run_id);
      }
      const out = data.outcome || {};
      const susp = pvText(out.suspicious_activity_rows);
      const geo = pvText(out.impossible_travel_rows);
      if (statusEl) {
        statusEl.textContent = `fraud_monitoring_agent staged CARD_BLOCK_AND_ESCALATE (suspicious=${susp}, geo=${geo}) → consumer_credit_synthesizer_agent.`;
      }
    } catch (err) {
      if (statusEl) statusEl.textContent = `Fraud card control failed: ${err.message}`;
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function triggerAsyncAuditSweep() {
    try {
      const resp = await fetch("/api/audit-sweep/trigger", { method: "POST" });
      if (!resp.ok) return;
      const st = await resp.json();
      renderAuditSweepState(st);
      renderRoster();
    } catch (err) {
      console.error("triggerAsyncAuditSweep failed", err);
    }
  }

    // 8. Memory Bank Compaction (#btnRunCompaction)
    const btnCompaction = $("btnRunCompaction");
    if (btnCompaction) {
      btnCompaction.addEventListener("click", runMemoryCompaction);
    }

    // 9. Live 5-channel ingestion (B), fraud card control (C), and async 95s audit sweep (4)
    const btnInject = $("btnInjectChannelEvent");
    if (btnInject) btnInject.addEventListener("click", injectLiveChannelEvent);
    const btnFraud = $("btnFraudCardControl");
    if (btnFraud) btnFraud.addEventListener("click", triggerFraudCardControl);
    const btnSweep = $("btnTriggerAuditSweep");
    if (btnSweep) btnSweep.addEventListener("click", triggerAsyncAuditSweep);

    // Initial background data hydration for canonical CUST00001
    loadScenarios();
    loadChannelsAndRosterSummary(DEFAULT_CUSTOMER_ID);
    loadDeploymentIdentity(DEFAULT_CUSTOMER_ID);
    loadEvalScorecard();
    loadLiveTestbed(DEFAULT_CUSTOMER_ID);
    loadCardOversight(DEFAULT_CUSTOMER_ID);
    // Only operator turns belong in the Pipeline; page load also fires probes.
    fetchAndRenderTrace('latest?kind=chat');
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
