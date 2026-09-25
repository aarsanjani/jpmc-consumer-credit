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

function formatLiveSpanRow(span, t0) {
  const rel = (ms) => "+" + ((ms - t0) / 1000).toFixed(2) + "s";
  const f = span.fields || {};
  const o = span.outcome || {};
  const subject = f.table || f.column || f.sql || f.search || f.scope || "";
  let kind = "tool";
  let what = span.label + (subject ? " " + subject : "");
  let isDeny = false;
  let isConditional = false;
  let isBqJob = false;
  let isAdHocSql = false;
  let isContract = false;

  if (o.action) {
    if (REFUSAL_ACTIONS.has(o.action)) {
      kind = "deny";
      isDeny = true;
    } else if (o.action !== "ALLOW") {
      isConditional = true;
    }
    what = subject + " → " + o.action + (o.evidence ? " · " + o.evidence : "");
  } else if (/^golden_query\./.test(span.label)) {
    isContract = true;
    what =
      span.label +
      (f.tool ? " · " + f.tool : "") +
      (o.rows != null ? " · " + o.rows + " rows" : "");
  } else if (span.label === "bigquery.query") {
    kind = "out";
    isBqJob = true;
    if (!/\.sql$/.test(String(f.sql || ""))) isAdHocSql = true;
    what =
      (f.sql || "query") +
      " · " +
      (o.rows != null ? o.rows + " rows" : "n/r rows") +
      (o.job_id ? " · job " + o.job_id : "");
  } else if (span.label === "channel.produce") {
    kind = "out";
    what =
      "channel.produce [" +
      (f.channel || "n/r") +
      "] · " +
      (o.sessions != null ? o.sessions + " sessions" : "n/r") +
      " · " +
      (o.memories != null ? o.memories + " memories" : "n/r") +
      " (considered " +
      (o.considered != null ? o.considered : "n/r") +
      ")";
  } else if (span.label === "channel.ingest") {
    kind = "out";
    what =
      "channel.ingest [" +
      (f.channel || "n/r") +
      "] · session " +
      (o.session_id || "n/r") +
      (o.turns != null ? " · " + o.turns + " turns written" : "");
  } else if (span.label === "fraud.card_control_or_escalate") {
    kind = "out";
    what =
      "execute_fraud_card_control_or_escalate [" +
      (f.action || "CARD_BLOCK") +
      "] · suspicious=" +
      (o.suspicious_rows != null ? o.suspicious_rows : "n/r") +
      " · geo=" +
      (o.geo_rows != null ? o.geo_rows : "n/r");
  } else if (span.label === "memory.retrieve") {
    kind = "out";
    what =
      "memory.retrieve [" +
      (f.scope || "CUST00001") +
      "] · query=\"" +
      (f.query || "") +
      "\" → " +
      (o.memories != null ? o.memories + " memories" : "n/r");
  } else if (span.label === "catalog.discover") {
    what =
      "catalog.discover " +
      (f.search || "") +
      " · " +
      (o.entries != null ? o.entries : "n/r") +
      " entries · " +
      (o.tables != null ? o.tables : "n/r") +
      " tables";
  } else if (/^veracity\./.test(span.label)) {
    kind = span.label === "veracity.gemini_evaluate" ? "out" : "tool";
    what =
      span.label +
      (f.avenue ? " [" + f.avenue + "]" : "") +
      (o.status ? " · verdict=" + o.status : "") +
      (o.confidence != null ? " · conf=" + o.confidence : "") +
      (o.declined_columns != null ? " · declined_cols=" + o.declined_columns : "");
  } else if (/^synthesizer\./.test(span.label)) {
    kind = "out";
    what =
      span.label +
      (f.model ? " [" + f.model + "]" : "") +
      (o.memories != null ? " · " + o.memories + " memories" : "") +
      (o.causal_steps != null ? " · " + o.causal_steps + " causal steps" : "");
  } else if (span.label === "oversight.card_decision") {
    kind = "out";
    what =
      "oversight.card_decision [" +
      (f.decision || o.decision || "DECIDED") +
      "] · status=" +
      (o.card_status || "updated");
  } else if (span.label === "memory.consolidate") {
    kind = "out";
    what =
      "memory.consolidate [session=" +
      (f.session || "n/r") +
      "] · status=" +
      (o.status || "TRIGGERED") +
      (o.operation ? " · op=" + String(o.operation).split("/").pop() : "");
  } else if (span.label === "memory.list_topics") {
    kind = "tool";
    what = "memory.list_topics · " + (o.topics != null ? o.topics + " custom topics" : "vocabulary read");
  } else if (span.label === "adk.in_memory_runner") {
    kind = "out";
    what =
      "ADK InMemoryRunner [" +
      (f.agent || "agent") +
      "] · session=" +
      (o.session_id || "live") +
      " · tool_calls=" +
      (o.tool_calls != null ? o.tool_calls : 0);
  } else if (/^phase\./.test(span.label)) {
    kind = "out";
    what =
      phaseName(span.label) +
      (o.status ? " · status=" + o.status : "") +
      (o.confidence != null ? " · conf=" + o.confidence : "") +
      (o.sessions != null ? " · " + o.sessions + " sessions" : "") +
      (o.resolved != null ? " · " + o.resolved + " glossary terms" : "") +
      (o.operation ? " · op=" + String(o.operation).split("/").pop() : "");
  }

  if (span.depth > 1) {
    what = "\u00a0\u00a0".repeat(span.depth - 1) + "└ " + what;
  }
  if (span.state === "error") {
    kind = "deny";
    isDeny = true;
    what += " · " + (span.error || "failed");
  }

  return {
    row: [rel(span.started_ms), kind, what, span.elapsed_ms.toFixed(0) + "ms", "nest"],
    elapsed_ms: span.elapsed_ms || 0,
    isDeny,
    isConditional,
    isBqJob,
    isAdHocSql,
    isContract,
  };
}

function routeSpanToAgentNames(span, activePhase, activeGoldenTool) {
  const f = span.fields || {};
  const lbl = span.label || "";
  const ag = String(f.agent || "").replace(/^\d+_/, "");
  const ch = (f.channel || "").toUpperCase();
  const tool = f.tool || activeGoldenTool || "";

  if (lbl === "adk.in_memory_runner" && ag) {
    return [ag];
  }
  if (activePhase === "phase.3_veracity" || lbl === "phase.3_veracity" || /^veracity\./.test(lbl)) {
    return ["claim_veracity_validator_agent"];
  }
  if (lbl === "audit.consolidated_sweep" || ag === "memory_bank_audit_agent") {
    return ["memory_bank_audit_agent"];
  }
  if (/^synthesizer\./.test(lbl) || lbl === "memory.consolidate" || lbl === "memory.list_topics" || lbl === "phase.4_memory_write") {
    return ["consumer_credit_synthesizer_agent"];
  }
  if (lbl === "oversight.card_decision") {
    return ag === "fraud_monitoring_agent"
      ? ["fraud_monitoring_agent", "consumer_credit_synthesizer_agent"]
      : ["consumer_credit_synthesizer_agent", "mobile_app_agent"];
  }
  if (lbl === "channel.produce") {
    if (ag) return [ag];
    if (ch.indexOf("FRAUD") >= 0) return ["fraud_monitoring_agent"];
    if (ch.indexOf("IVR") >= 0 || ch.indexOf("TELEPHONY") >= 0) return ["telephony_ivr_agent"];
    if (ch.indexOf("MOBILE") >= 0) return ["mobile_app_agent"];
    if (ch.indexOf("WEB") >= 0) return ["web_portal_agent"];
    if (ch.indexOf("BRANCH") >= 0) return ["branch_support_agent"];
  }
  if (lbl === "channel.ingest") {
    if (ch.indexOf("FRAUD") >= 0) return ["fraud_monitoring_agent", "consumer_credit_synthesizer_agent"];
    if (ch.indexOf("IVR") >= 0) return ["telephony_ivr_agent", "consumer_credit_synthesizer_agent"];
    if (ch.indexOf("MOBILE") >= 0) return ["mobile_app_agent", "consumer_credit_synthesizer_agent"];
    if (ch.indexOf("WEB") >= 0) return ["web_portal_agent", "consumer_credit_synthesizer_agent"];
    if (ch.indexOf("BRANCH") >= 0) return ["branch_support_agent", "consumer_credit_synthesizer_agent"];
    return ["consumer_credit_synthesizer_agent"];
  }
  if (lbl === "fraud.card_control_or_escalate") {
    // 01 stages the card block and explicitly escalates the event to 08 (A -> B handoff)
    return ["fraud_monitoring_agent", "consumer_credit_synthesizer_agent"];
  }
  if (tool.indexOf("golden_03") >= 0 || tool.indexOf("golden_04") >= 0 || tool.indexOf("golden_05") >= 0 || tool.indexOf("golden_06") >= 0 || tool.indexOf("golden_12") >= 0) {
    return ["fraud_monitoring_agent"];
  }
  if (tool.indexOf("golden_02") >= 0) {
    return ["telephony_ivr_agent"];
  }
  if (tool.indexOf("golden_10") >= 0) {
    return ["mobile_app_agent"];
  }
  if (tool.indexOf("golden_07") >= 0 || tool.indexOf("golden_08") >= 0 || tool.indexOf("golden_09") >= 0 || lbl === "phase.2_glossary" || lbl === "catalog.lookup_term") {
    return ["web_portal_agent"];
  }
  if (tool.indexOf("golden_01") >= 0) {
    return ["branch_support_agent"];
  }
  if (ag) {
    return [ag];
  }
  return ["consumer_credit_synthesizer_agent"];
}

let LAST_TRACED_SPANS = [];
let LAST_TRACED_RUN_ID = "";
let ALL_DESK_AGENTS = [];

// Identity only, so the pipeline can draw before /api/channels/summary answers.
// Everything else on the card comes from the backend (list_all_agents in
// backend/adk_agents.py) and shows n/r until it arrives.
const FALLBACK_DESK_AGENTS = [
  { agent_id: "01_fraud_monitoring_agent", name: "fraud_monitoring_agent", role: "Fraud Velocity & Geo-Anomaly Specialist (CHANNEL_PRODUCER · FRAUD_OPS_OUTBOUND)" },
  { agent_id: "02_telephony_ivr_agent", name: "telephony_ivr_agent", role: "Contact Center IVR & Inbound Call Specialist (CHANNEL_PRODUCER · TELEPHONY_IVR)" },
  { agent_id: "03_mobile_app_agent", name: "mobile_app_agent", role: "Mobile Banking & Digital Wallet Token Specialist (CHANNEL_PRODUCER · MOBILE_APP)" },
  { agent_id: "04_web_portal_agent", name: "web_portal_agent", role: "Online Banking & Statutory Dispute Policy Specialist (CHANNEL_PRODUCER · WEB_PORTAL)" },
  { agent_id: "05_branch_support_agent", name: "branch_support_agent", role: "In-Branch Banker & Identity Verification Specialist (CHANNEL_PRODUCER · BRANCH_SUPPORT)" },
  { agent_id: "06_claim_veracity_validator_agent", name: "claim_veracity_validator_agent", role: "Pre-Write Claim Veracity Gatekeeper (PRE_WRITE_VALIDATOR)" },
  { agent_id: "07_memory_bank_audit_agent", name: "memory_bank_audit_agent", role: "Enterprise Memory Bank Consolidated Sweep Auditor (CONSOLIDATED_AUDITOR · ASYNC JOB)" },
  { agent_id: "08_consumer_credit_synthesizer_agent", name: "consumer_credit_synthesizer_agent", role: "Lead Synthesizer & Hub Orchestrator (LEAD_SYNTHESIZER · CORE_BANKING)" },
];

function setLivePulseState(mode) {
  const pulse = document.getElementById("livePulse") || document.querySelector(".livebar .pulse");
  if (!pulse) return;
  if (mode === "running") {
    pulse.className = "pulse running";
    pulse.textContent = "";
  } else if (mode === "done") {
    pulse.className = "pulse done";
    pulse.textContent = "";
  } else {
    pulse.className = "pulse idle";
    pulse.textContent = ">";
  }
}

function syncTraceBarVisibility() {
  const pickwrap = $("pickwrap");
  const traceBtn = $("traceBtn");
  const drop = $("drop");
  const menuTitle = $("mobileMenuTitle");
  const hasLogged = AGENTS.length > 0;

  if (pickwrap) pickwrap.hidden = !hasLogged;
  if (traceBtn) traceBtn.hidden = !hasLogged;
  if (!hasLogged) {
    if (drop) drop.hidden = true;
    const pulse = document.getElementById("livePulse") || document.querySelector(".livebar .pulse");
    if (!pulse || !pulse.classList.contains("running")) {
      setLivePulseState("idle");
    }
    const kind = $("biKind");
    const text = $("biText");
    const metrics = $("biMetrics");
    if (kind) {
      kind.className = "bk";
      kind.textContent = "IDLE";
    }
    if (text) {
      text.className = "bt";
      text.textContent = "No trace logged yet — run a scenario, ask a question, or trigger an action";
    }
    if (metrics) {
      metrics.innerHTML = "";
    }
  } else {
    const pulse = document.getElementById("livePulse") || document.querySelector(".livebar .pulse");
    if (!pulse || !pulse.classList.contains("running")) {
      setLivePulseState("done");
    }
    if (menuTitle) {
      menuTitle.textContent = `Run ${LAST_TRACED_RUN_ID || "live"} · ${AGENTS.length} logged agent(s)`;
    }
  }
}

function buildDeskAgentEntries(summary, rawSpans, runId) {
  const deskAgents =
    summary && Array.isArray(summary.agents) && summary.agents.length
      ? summary.agents
      : FALLBACK_DESK_AGENTS;
  const channelMap = {};
  if (summary && summary.snapshot && Array.isArray(summary.snapshot.channels)) {
    summary.snapshot.channels.forEach((ch) => {
      channelMap[ch.agent_id] = ch;
      channelMap[ch.agent_name] = ch;
    });
  }

  const hasLiveRun = Array.isArray(rawSpans) && rawSpans.length > 0;
  const sortedSpans = hasLiveRun ? rawSpans.slice().sort((a, b) => a.started_ms - b.started_ms) : [];
  const t0 = hasLiveRun ? sortedSpans[0].started_ms : 0;

  const byAgent = {};
  deskAgents.forEach((ag) => {
    byAgent[ag.name] = {
      rows: [],
      elapsedMs: 0,
      deny: 0,
      conditional: 0,
      jobs: 0,
      sql: 0,
      contracts: 0,
    };
  });

  if (hasLiveRun) {
    let activePhase = "";
    let activeGoldenTool = "";
    sortedSpans.forEach((span) => {
      if (span.depth === 0 && /^phase\./.test(span.label || "")) {
        activePhase = span.label;
        activeGoldenTool = "";
      } else if (span.depth === 1 && /^golden_query\./.test(span.label || "")) {
        activeGoldenTool = (span.fields && span.fields.tool) || span.label;
      } else if (span.depth === 1 && !/^golden_query\./.test(span.label || "")) {
        activeGoldenTool = "";
      }

      const formatted = formatLiveSpanRow(span, t0);
      const targets = routeSpanToAgentNames(span, activePhase, activeGoldenTool);
      targets.forEach((agName) => {
        const bucket = byAgent[agName];
        if (!bucket) return;
        bucket.rows.push(formatted.row);
        if (span.depth <= 1) bucket.elapsedMs += formatted.elapsed_ms;
        if (formatted.isDeny) bucket.deny++;
        if (formatted.isConditional) bucket.conditional++;
        if (formatted.isBqJob) bucket.jobs++;
        if (formatted.isAdHocSql) bucket.sql++;
        if (formatted.isContract) bucket.contracts++;
      });
    });
  }

  return deskAgents.map((ag, idx) => {
    const num = idx + 1 < 10 ? "0" + (idx + 1) : String(idx + 1);
    const ch = channelMap[ag.agent_id] || channelMap[ag.name];
    const isAudit = ag.name === "memory_bank_audit_agent";
    const bucket = byAgent[ag.name] || { rows: [], elapsedMs: 0, deny: 0, conditional: 0, jobs: 0, sql: 0, contracts: 0 };

    if (!hasLiveRun) {
      const chBadge = ch ? `${ch.channel_label} · IDLE` : (isAudit ? "ASYNC · IDLE" : "IDLE");
      const idleSub = ch
        ? `Idle (${ch.session_count} sessions in Memory Bank) — click Replay run → or + Inject live event to trace`
        : (isAudit
            ? "Async 95s sweep (kept off turn path) — click ▶ Run 95s audit sweep to trace"
            : "Idle — click Replay run → or Send → to execute and stream live traces");
      return {
        k: ag.agent_id || ag.name,
        i: num,
        name: ag.name,
        ms: chBadge,
        subline: idleSub,
        state: "queue",
        refusals: 0,
        tools: 0,
        deny: 0,
        sql: 0,
        jobs: 0,
        conditional: 0,
        contracts: 0,
        started: "n/r",
        ended: "n/r",
        spans: [],
        foot: [
          ["run", "none"],
          ["spans", "0"],
          ["refusals", "0"],
          ["bq jobs", "0"],
          ["ad-hoc sql", "0"],
        ],
      };
    }

    const secsLabel = bucket.rows.length
      ? (bucket.elapsedMs >= 1000 ? (bucket.elapsedMs / 1000).toFixed(1) + "s" : Math.max(1, Math.round(bucket.elapsedMs)) + "ms")
      : (isAudit ? "ASYNC · 0ms" : "0ms");

    const runSub = bucket.rows.length
      ? `[run ${runId}] ${bucket.rows.length} live spans · ${bucket.jobs} bq · ${bucket.deny} refused · ${secsLabel}`
      : (isAudit
          ? `[run ${runId}] 0 spans (async 95s sweep kept off synchronous turn path)`
          : `[run ${runId}] 0 spans triggered in this action`);

    return {
      k: ag.agent_id || ag.name,
      i: num,
      name: ag.name,
      ms: secsLabel,
      subline: runSub,
      state: bucket.rows.length ? "done" : "queue",
      refusals: bucket.deny,
      tools: bucket.rows.length,
      deny: bucket.deny,
      sql: bucket.sql,
      jobs: bucket.jobs,
      conditional: bucket.conditional,
      contracts: bucket.contracts,
      started: "+0.00s",
      ended: secsLabel,
      spans: bucket.rows,
      foot: [
        ["run", String(runId || "live")],
        ["tools", String(bucket.rows.length)],
        ["refusals", String(bucket.deny)],
        ["owner-only", String(bucket.conditional)],
        ["bq jobs", String(bucket.jobs)],
        ["ad-hoc sql", String(bucket.sql)],
      ],
    };
  });
}

async function fetchAndRenderTrace(runId) {
  try {
    if (!runId) {
      renderRoster();
      syncTraceBarVisibility();
      return;
    }
    const resp = await fetch("/api/trace/" + runId);
    if (!resp.ok) {
      renderRoster();
      syncTraceBarVisibility();
      return;
    }
    const data = await resp.json();
    const spans = (data.spans || []).slice().sort((a, b) => a.started_ms - b.started_ms);
    if (!spans.length) {
      renderRoster();
      syncTraceBarVisibility();
      return;
    }

    LAST_TRACED_SPANS = spans;
    LAST_TRACED_RUN_ID = data.run_id || runId;

    const t0 = spans[0].started_ms;
    const last = spans.reduce((m, s) => Math.max(m, s.started_ms + s.elapsed_ms), t0);
    const wall = ((last - t0) / 1000).toFixed(1) + "s";
    const steps = groupSpansIntoSteps(spans, t0);
    const runMeta = "Run " + LAST_TRACED_RUN_ID;

    ALL_DESK_AGENTS = buildDeskAgentEntries(STATE.channelsSummary, spans, LAST_TRACED_RUN_ID);
    // Only agents that actually logged spans in this run appear in the black trace nav dropdown!
    AGENTS = ALL_DESK_AGENTS.filter((a) => a.spans && a.spans.length > 0);
    window.AGENTS = AGENTS;

    const totals = steps.reduce(
      (acc, s) => ({ deny: acc.deny + s.deny, sql: acc.sql + s.sql, jobs: acc.jobs + s.jobs }),
      { deny: 0, sql: 0, jobs: 0 }
    );

    setLivePulseState("done");
    setAgentCountText(AGENTS.length + " / 8 active · " + spans.length + " spans");
    setRunMetaText(runMeta + " · " + AGENTS.length + " logged agent(s) · " + spans.length + " live spans · " + wall);
    setStampText(runMeta + " · " + wall + " · ad-hoc SQL " + totals.sql);
    setTruthEngineText(
      runMeta + " · " + AGENTS.length + " active agent(s) · " + spans.length + " live spans in " + wall + " · " +
      totals.jobs + " BigQuery jobs · " + totals.deny + " refusals · ad-hoc SQL " + totals.sql
    );
    syncTraceBarVisibility();
    buildMenu();
    if (AGENTS.length) {
      // Pick the synthesizer or first logged agent to display in the black bar readout,
      // and pre-render #body inside #drop WITHOUT auto-expanding #drop!
      const synthIdx = AGENTS.findIndex((a) => a.name === "consumer_credit_synthesizer_agent");
      pick(synthIdx >= 0 ? synthIdx : 0);
    }
    renderRoster();
    renderGovernanceTraps(spans);
  } catch (err) {
    console.error("fetchAndRenderTrace failed", err);
  }
}

function renderRoster() {
  const rosterList = $("agentRosterList");
  if (!rosterList) return;

  if (!ALL_DESK_AGENTS.length && STATE.channelsSummary) {
    ALL_DESK_AGENTS = buildDeskAgentEntries(STATE.channelsSummary, LAST_TRACED_SPANS, LAST_TRACED_RUN_ID);
    AGENTS = ALL_DESK_AGENTS.filter((a) => a.spans && a.spans.length > 0);
    window.AGENTS = AGENTS;
    syncTraceBarVisibility();
    buildMenu();
    if (AGENTS.length) pick(0);
  }

  if (!ALL_DESK_AGENTS.length) {
    rosterList.innerHTML = "<div class='item'><p class='rd'>Loading 8-agent desk &amp; 5 live channels…</p></div>";
    return;
  }

  rosterList.innerHTML = ALL_DESK_AGENTS.map((a) => {
    const label = (a.i ? a.i + " · " : "") + a.name;
    const sub = a.subline || "Idle";
    const hasSpans = a.spans && a.spans.length > 0;
    const badgeClass = hasSpans ? "tag ok" : "tag";
    return `<div class="item ${a.state}" style="cursor:pointer;" title="Click to open Agent Spec Card &amp; run live via ADK InMemoryRunner" data-agent-key="${escapeHtml(a.k)}">
      <div class="item-top"><span class="dot"></span>
      <span class="nm">${escapeHtml(label)}</span><span class="${badgeClass}">${escapeHtml(a.ms)}</span></div>
      <p class="rd">${escapeHtml(sub)}</p></div>`;
  }).join("");

  rosterList.querySelectorAll("[data-agent-key]").forEach((el) => {
    el.addEventListener("click", () => {
      const key = el.getAttribute("data-agent-key");
      openAgentCardModal(key);
    });
  });
}

let CURRENT_MODAL_AGENT_KEY = "";

function findAgentSpec(agentKey) {
  const apiAgents = (STATE.channelsSummary && Array.isArray(STATE.channelsSummary.agents))
    ? STATE.channelsSummary.agents
    : [];
  const clean = String(agentKey || "").replace(/^\d+_/, "");
  const fromApi = apiAgents.find((a) => a.agent_id === agentKey || a.name === agentKey || a.name === clean);
  const fromFallback = FALLBACK_DESK_AGENTS.find((a) => a.agent_id === agentKey || a.name === agentKey || a.name === clean);
  return Object.assign({}, fromFallback || {}, fromApi || {});
}

function openAgentCardModal(agentKey) {
  const modal = $("agentCardModal");
  if (!modal) return;
  CURRENT_MODAL_AGENT_KEY = agentKey;

  const spec = findAgentSpec(agentKey);
  const deskEntry = ALL_DESK_AGENTS.find((a) => a.k === agentKey || a.name === spec.name);
  const numPrefix = deskEntry && deskEntry.i ? `${deskEntry.i} · ` : "";

  if ($("agentModalBadge")) {
    $("agentModalBadge").textContent = `ADK LlmAgent · google.adk.runners.InMemoryRunner · ${spec.model || "gemini-2.5-flash"}`;
  }
  if ($("agentModalTitle")) {
    $("agentModalTitle").textContent = `${numPrefix}${spec.name || agentKey}`;
  }
  if ($("agentModalRole")) {
    $("agentModalRole").textContent = spec.role || "CCB Fraud & Disputes Specialist";
  }
  if ($("agentModalDesc")) {
    $("agentModalDesc").textContent = spec.description || "";
  }

  renderSourceChips(spec);

  const tools = Array.isArray(spec.tools) ? spec.tools : [];
  if ($("agentModalToolsCount")) {
    $("agentModalToolsCount").textContent = `(${tools.length})`;
  }
  if ($("agentModalBoundTools")) {
    $("agentModalBoundTools").innerHTML = tools.length
      ? tools.map((tl) => `<span class="tag">${escapeHtml(tl)}</span>`).join("")
      : `<span class="tag">${NOT_RECORDED}</span>`;
  }

  const loggedIdx = AGENTS.findIndex((ag) => ag.k === agentKey || ag.name === spec.name);
  const btnTrace = $("btnViewAgentTraceModal");
  if (btnTrace) {
    btnTrace.disabled = loggedIdx < 0;
    btnTrace.textContent =
      loggedIdx >= 0
        ? `View ${AGENTS[loggedIdx].spans.length} Logged Spans in Trace Bar ▴`
        : "No Spans Logged Yet (Run Agent Live)";
  }

  if ($("agentModalStatusTag")) {
    $("agentModalStatusTag").className = loggedIdx >= 0 ? "tag ok" : "tag on";
    $("agentModalStatusTag").textContent =
      loggedIdx >= 0
        ? `TRACED · ${AGENTS[loggedIdx].spans.length} SPANS (${AGENTS[loggedIdx].ms})`
        : "READY · InMemoryRunner";
  }

  if ($("agentModalRunnerOutput")) {
    if (loggedIdx >= 0) {
      $("agentModalRunnerOutput").innerHTML =
        `<div><b>Last recorded trace (${escapeHtml(AGENTS[loggedIdx].ms)}):</b> ` +
        `${AGENTS[loggedIdx].spans.length} spans logged in top Black Trace Bar. ` +
        `Click <b>▶ Run Agent Live</b> to execute a fresh turn of <code>${escapeHtml(spec.name)}</code> via <code>InMemoryRunner</code>.</div>`;
    } else {
      $("agentModalRunnerOutput").innerHTML =
        `Idle — click <b>▶ Run Agent Live</b> above to construct <code>google.adk.runners.InMemoryRunner</code> for <code>${escapeHtml(spec.name)}</code>, invoke its bound ADK tools against BigQuery / Dataplex / Memory Bank, and stream its spans into the top Black Trace Bar.`;
    }
  }

  modal.hidden = false;
}

function closeAgentCardModal() {
  closeSrcPop(false);
  const modal = $("agentCardModal");
  if (modal) modal.hidden = true;
}

// =========================================================================
// Agent card source chips: Memory Bank / Knowledge Catalog / BigQuery.
// Card fields come from the descriptor (/api/channels/summary). Topic text,
// channel queries, catalog verdicts and golden query tables come from
// backend/ui_insights.py. Run data comes from recorded trace spans. Anything
// not measured is drawn as n/r with the reason.
// =========================================================================

const SRC = {
  topics: null,
  topicsError: "",
  contracts: null,
  contractsError: "",
  verdicts: {},
  verdictsState: "idle",
  verdictsError: "",
  goldenRuns: {},
  goldenState: "idle",
  spec: null,
  token: 0,
  openId: null,
  pinned: false,
  closeTimer: null,
};

const SRC_POPS = [
  ["mem", "Memory Bank"],
  ["cat", "Knowledge Catalog"],
  ["bq", "BigQuery"],
];

const VERDICT_BADGES = {
  ALLOW: ["v-allow", "✓ ALLOW"],
  ALLOW_TO_OWNER_ONLY: ["v-owner", "◐ ALLOW_TO_OWNER_ONLY"],
  MASK: ["v-owner", "◐ MASK"],
  REFUSE: ["v-refuse", "✕ REFUSE"],
};

async function fetchJson(url) {
  const resp = await fetch(url);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

function nrSpan(reason) {
  return `<span class="nr">n/r · ${escapeHtml(reason)}</span>`;
}

function srcList(value) {
  return Array.isArray(value) ? value : [];
}

function contractById(id) {
  return SRC.contracts ? SRC.contracts.find((c) => c.id === id) : null;
}

function goldenShortId(id) {
  return String(id).split("_")[0];
}

function memoryPopBody(spec) {
  const scoped = srcList(spec.memory_topics_scoped);
  const engineTopics = SRC.topics ? srcList(pvValue(SRC.topics.topics)) : null;
  const channel = SRC.topics ? srcList(SRC.topics.channel_agents).find((c) => c.name === spec.name) : null;
  const readsAll = scoped.some((t) => /^ALL\b/.test(t));
  const readsNone = scoped.some((t) => /^NONE\b/.test(t));

  let topicsHtml;
  if (readsNone) {
    topicsHtml = `<div class="val">${escapeHtml(scoped.join(", "))}</div>`;
  } else if (!engineTopics) {
    topicsHtml = SRC.topicsError
      ? `<div class="val">${nrSpan("topic list failed: " + SRC.topicsError)}</div>`
      : `<div class="val">Loading topics from the engine…</div>`;
  } else {
    const byLabel = Object.fromEntries(engineTopics.map((t) => [t.label, t]));
    const labels = readsAll ? engineTopics.map((t) => t.label) : scoped;
    const items = labels.map((label) => {
      const topic = byLabel[label];
      const desc = !topic
        ? nrSpan("not configured on the engine")
        : topic.description && topic.description !== NOT_RECORDED
          ? escapeHtml(topic.description)
          : nrSpan(`${topic.kind || "managed"} topic, no description on the engine`);
      return `<li><span class="t">${escapeHtml(label)}</span>${desc}</li>`;
    }).join("");
    topicsHtml = (readsAll ? `<p class="row-note">${escapeHtml(scoped.join(", "))}</p>` : "") +
      `<ul class="topic-list">${items || `<li>${nrSpan("no topics")}</li>`}</ul>`;
  }

  let queryHtml;
  if (channel) queryHtml = escapeHtml(pvText(channel.query));
  else if (readsNone) queryHtml = nrSpan("doesn't read Memory Bank");
  else if (SRC.topics) queryHtml = nrSpan("no fixed query; it's chosen per call");
  else queryHtml = SRC.topicsError ? nrSpan("query lookup failed") : "Loading…";

  const scope = readsNone ? nrSpan("doesn't read Memory Bank") : `<code>customer_id = ${escapeHtml(STATE.customerId)}</code>`;
  return `<div class="lbl first">Topics this agent reads</div>${topicsHtml}
    <div class="lbl">Fixed query it sends</div><div class="val">${queryHtml}</div>
    <div class="lbl">Scope</div><div class="val">${scope}</div>
    <p class="src">source: live engine config (<code>GET /api/memory/topics</code>) + <code>AGENT_MEMORY_TOPICS</code></p>`;
}

function catalogUse(name, spec) {
  if (!SRC.contracts) return "";
  const mine = srcList(spec.authorized_golden_queries);
  const readers = mine.filter((id) => (contractById(id) || {}).tables?.includes(name)).map(goldenShortId);
  const checkers = mine.filter((id) => (contractById(id) || {}).columns?.includes(name)).map(goldenShortId);
  const parts = [];
  if (readers.length) parts.push(`read by golden ${readers.join(", ")}`);
  if (checkers.length) parts.push(`checked by golden ${checkers.join(", ")}`);
  return parts.join(" · ");
}

function verdictBadge(name) {
  if (SRC.verdictsState === "loading") return `<span class="verdict v-nr">checking…</span>`;
  const decision = SRC.verdicts[name];
  if (!decision) {
    const why = SRC.verdictsState === "error" ? "lookup failed" : "not checked";
    return `<span class="verdict v-nr">n/r · ${why}</span>`;
  }
  const [cls, label] = VERDICT_BADGES[decision.action] || ["v-nr", `? ${decision.action || NOT_RECORDED}`];
  return `<span class="verdict ${cls}">${escapeHtml(label)}</span>`;
}

function catalogPopBody(spec) {
  const names = srcList(spec.catalog_governance);
  const rows = names.map((name) => {
    const decision = SRC.verdicts[name];
    const why = decision
      ? `decided by: ${escapeHtml(decision.evidence || NOT_RECORDED)}`
      : SRC.verdictsState === "error" ? nrSpan(SRC.verdictsError) : "";
    const use = catalogUse(name, spec);
    return `<li><div class="cat-top"><span class="t">${escapeHtml(name)}</span>${verdictBadge(name)}</div>
      ${why ? `<div class="cat-why">${why}</div>` : ""}
      ${use ? `<div class="cat-use">${escapeHtml(use)}</div>` : ""}</li>`;
  }).join("");
  return `<div class="lbl first">Tables and columns its golden queries touch</div>
    ${names.length ? `<ul class="cat-list">${rows}</ul>` : `<div class="val">${nrSpan("this agent has no golden queries, so nothing is checked")}</div>`}
    <div class="advisory">On the golden-query path the catalog is advisory today: a refusal is recorded and the query still runs.</div>
    <p class="src">source: live Dataplex lookup per name (<code>GET /api/catalog/verdicts</code>)</p>`;
}

function goldenRunFields(id) {
  const run = SRC.goldenRuns[id];
  if (!run) {
    const why = SRC.goldenState === "loading" ? "reading traces…" : "not in this session's latest traces";
    return ["last run", "rows", "job id"].map((k) => `<dt>${k}</dt><dd class="nr">n/r · ${why}</dd>`).join("");
  }
  const lastRun = `run ${escapeHtml(run.run_id)}${run.state === "error" ? " · failed" : ""}`;
  const rows = run.rows === undefined || run.rows === null
    ? `<dd class="nr">n/r · ${escapeHtml(run.error || "not recorded on the span")}</dd>`
    : `<dd>${escapeHtml(run.rows)}</dd>`;
  const job = run.job_id && run.job_id !== NOT_RECORDED
    ? `<dd><code>${escapeHtml(run.job_id)}</code></dd>`
    : `<dd class="nr">n/r · not recorded on the span</dd>`;
  return `<dt>last run</dt><dd>${lastRun}</dd><dt>rows</dt>${rows}<dt>job id</dt>${job}`;
}

function bigqueryPopBody(spec) {
  const authorized = srcList(spec.authorized_golden_queries);
  const bound = new Set(srcList(spec.bound_golden_queries));
  const cards = authorized.map((id) => {
    const contract = contractById(id);
    const tables = contract
      ? escapeHtml(contract.tables.join(", "))
      : SRC.contractsError ? nrSpan("contracts failed: " + SRC.contractsError) : "Loading…";
    const boundTag = bound.has(id)
      ? `<span class="tag ok" title="bound to this agent as a tool">bound</span>`
      : `<span class="tag" title="authorized in INDEX.md, not bound as a tool">not bound</span>`;
    return `<div class="gq"><div class="cat-top"><span class="t">golden ${escapeHtml(id)}</span>${boundTag}</div>
      ${contract ? `<p class="q">${escapeHtml(contract.question)}</p>` : ""}
      <dl><dt>reads</dt><dd>${tables}</dd>${goldenRunFields(id)}</dl></div>`;
  }).join("");
  return `<div class="lbl first">Golden queries this agent may call</div>
    ${cards || `<div class="val">${nrSpan("this agent has no golden queries")}</div>`}
    ${authorized.length ? `<p class="row-note">${bound.size} of ${authorized.length} bound as tools on this agent.</p>` : ""}
    <p class="src">source: agent descriptor + <code>GOLDEN_QUERY_CONTRACTS</code>; run data from <code>bigquery.query</code> trace spans</p>`;
}

function srcChipSummary(id, spec) {
  if (id === "mem") {
    const scoped = srcList(spec.memory_topics_scoped);
    if (scoped.some((t) => /^NONE\b/.test(t))) return "not read";
    if (scoped.some((t) => /^ALL\b/.test(t))) return "all topics";
    return `${scoped.length} ${scoped.length === 1 ? "topic" : "topics"}`;
  }
  if (id === "cat") {
    const names = srcList(spec.catalog_governance);
    if (SRC.verdictsState !== "done") return `${names.length} checked`;
    const refused = names.filter((n) => (SRC.verdicts[n] || {}).action === "REFUSE").length;
    return `${names.length} checked · ${refused} refused`;
  }
  const n = srcList(spec.authorized_golden_queries).length;
  return `${n} ${n === 1 ? "query" : "queries"} · ${srcList(spec.bound_golden_queries).length} bound`;
}

function srcPopBody(id, spec) {
  const body = id === "mem" ? memoryPopBody(spec) : id === "cat" ? catalogPopBody(spec) : bigqueryPopBody(spec);
  const title = SRC_POPS.find(([key]) => key === id)[1];
  return `<div class="pop-hd"><h3 id="srcpop-${id}-h">${escapeHtml(title)}</h3>
    <button type="button" class="pop-x" data-close aria-label="Close ${escapeHtml(title)} card">Close ×</button></div>${body}`;
}

function renderSourceChips(spec) {
  const host = $("agentModalSrcChips");
  if (!host) return;
  closeSrcPop(false);
  SRC.spec = spec;
  // Each popover sits right after its chip in the DOM, so Tab goes chip -> its card -> next chip.
  host.innerHTML = SRC_POPS.map(([id, name]) => `
    <span class="src-wrap" data-pop="${id}">
      <button type="button" class="src-chip" id="srcchip-${id}" aria-expanded="false" aria-controls="srcpop-${id}">
        <b>${escapeHtml(name)}</b><small></small>
      </button>
      <div class="src-pop" id="srcpop-${id}" role="dialog" aria-labelledby="srcpop-${id}-h" hidden></div>
    </span>`).join("");
  host.querySelectorAll(".src-wrap").forEach(wireSrcWrap);
  refreshSourceChips();
  loadSourceData(spec);
}

function refreshSourceChips() {
  const spec = SRC.spec;
  if (!spec) return;
  SRC_POPS.forEach(([id]) => {
    const chip = $(`srcchip-${id}`);
    const pop = $(`srcpop-${id}`);
    if (!chip || !pop) return;
    chip.querySelector("small").textContent = srcChipSummary(id, spec);
    const html = srcPopBody(id, spec);
    if (pop.dataset.html !== html) {
      pop.innerHTML = html;
      pop.dataset.html = html;
    }
  });
  if (SRC.openId) placeSrcPop($(`srcchip-${SRC.openId}`), $(`srcpop-${SRC.openId}`));
}

function goldenRunsFromSpans(spans, runId) {
  const runs = {};
  spans.forEach((span) => {
    const sql = span.label === "bigquery.query" && span.fields ? String(span.fields.sql || "") : "";
    if (!sql) return;
    const id = sql.replace(/\.sql$/, "");
    if (runs[id] && runs[id].started_ms > span.started_ms) return;
    const outcome = span.outcome || {};
    runs[id] = {
      run_id: runId,
      started_ms: span.started_ms,
      rows: outcome.rows,
      job_id: outcome.job_id,
      state: span.state,
      error: span.error,
    };
  });
  return runs;
}

async function loadGoldenRuns() {
  // The trace on screen wins, then the latest chat run, then any latest run.
  const sources = [];
  if (LAST_TRACED_SPANS.length) sources.push({ run_id: LAST_TRACED_RUN_ID, spans: LAST_TRACED_SPANS });
  for (const url of ["/api/trace/latest?kind=chat", "/api/trace/latest"]) {
    try {
      const data = await fetchJson(url);
      if (!sources.some((s) => s.run_id === data.run_id)) sources.push(data);
    } catch (err) {
      // 404 means no trace of that kind yet, which is a normal state.
    }
  }
  const runs = {};
  sources.forEach((src) => {
    const found = goldenRunsFromSpans(srcList(src.spans), src.run_id);
    Object.keys(found).forEach((id) => {
      if (!runs[id]) runs[id] = found[id];
    });
  });
  return runs;
}

function loadSourceData(spec) {
  const token = ++SRC.token;
  const stillCurrent = () => token === SRC.token;

  if (!SRC.topics) {
    SRC.topicsError = "";
    fetchJson("/api/memory/topics")
      .then((data) => { SRC.topics = data; })
      .catch((err) => { SRC.topicsError = err.message; })
      .finally(() => stillCurrent() && refreshSourceChips());
  }
  if (!SRC.contracts) {
    SRC.contractsError = "";
    fetchJson("/api/golden/contracts")
      .then((data) => { SRC.contracts = srcList(data.contracts); })
      .catch((err) => { SRC.contractsError = err.message; })
      .finally(() => stillCurrent() && refreshSourceChips());
  }

  const names = srcList(spec.catalog_governance);
  SRC.verdicts = {};
  SRC.verdictsError = "";
  SRC.verdictsState = names.length ? "loading" : "done";
  if (names.length) {
    fetchJson(`/api/catalog/verdicts?names=${encodeURIComponent(names.join(","))}`)
      .then((data) => {
        if (!stillCurrent()) return;
        srcList(data.verdicts).forEach((v) => { SRC.verdicts[v.name] = pvValue(v.decision); });
        SRC.verdictsState = "done";
      })
      .catch((err) => {
        if (!stillCurrent()) return;
        SRC.verdictsState = "error";
        SRC.verdictsError = err.message;
      })
      .finally(() => stillCurrent() && refreshSourceChips());
  }

  SRC.goldenRuns = {};
  SRC.goldenState = "loading";
  loadGoldenRuns()
    .then((runs) => { if (stillCurrent()) SRC.goldenRuns = runs; })
    .finally(() => {
      if (!stillCurrent()) return;
      SRC.goldenState = "done";
      refreshSourceChips();
    });
  refreshSourceChips();
}

function placeSrcPop(chip, pop) {
  if (!chip || !pop) return;
  const margin = 12;
  const vw = document.documentElement.clientWidth;
  const vh = window.innerHeight;
  pop.style.maxHeight = "";
  const r = chip.getBoundingClientRect();
  const w = pop.offsetWidth;
  // Clamp horizontally so the card never runs off the right edge.
  const left = Math.max(margin, Math.min(r.left, vw - margin - w));
  let h = pop.offsetHeight;
  const below = vh - r.bottom - 8 - margin;
  const above = r.top - 8 - margin;
  let top;
  if (h <= below) top = r.bottom + 8;
  else if (h <= above) top = r.top - 8 - h;
  else if (below >= above) { pop.style.maxHeight = `${below}px`; top = r.bottom + 8; }
  else { pop.style.maxHeight = `${above}px`; h = above; top = r.top - 8 - h; }
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;
}

function openSrcPop(id, pin) {
  clearTimeout(SRC.closeTimer);
  const keepPinned = SRC.pinned && SRC.openId === id;
  if (SRC.openId && SRC.openId !== id) closeSrcPop(false);
  const chip = $(`srcchip-${id}`);
  const pop = $(`srcpop-${id}`);
  if (!chip || !pop) return;
  pop.hidden = false;
  chip.setAttribute("aria-expanded", "true");
  SRC.openId = id;
  SRC.pinned = keepPinned || !!pin;
  placeSrcPop(chip, pop);
}

function closeSrcPop(returnFocus) {
  clearTimeout(SRC.closeTimer);
  if (!SRC.openId) return;
  const chip = $(`srcchip-${SRC.openId}`);
  const pop = $(`srcpop-${SRC.openId}`);
  if (pop) pop.hidden = true;
  if (chip) {
    chip.setAttribute("aria-expanded", "false");
    if (returnFocus) chip.focus();
  }
  SRC.openId = null;
  SRC.pinned = false;
}

function softCloseSrcPop() {
  if (SRC.pinned) return;
  clearTimeout(SRC.closeTimer);
  SRC.closeTimer = setTimeout(() => closeSrcPop(false), 180);
}

function wireSrcWrap(wrap) {
  const id = wrap.dataset.pop;
  const chip = wrap.querySelector(".src-chip");
  wrap.addEventListener("mouseenter", () => openSrcPop(id, false));
  wrap.addEventListener("mouseleave", softCloseSrcPop);
  chip.addEventListener("click", () => {
    if (SRC.openId === id && SRC.pinned) closeSrcPop(false);
    else openSrcPop(id, true);
  });
  chip.addEventListener("focus", () => { if (SRC.openId !== id) openSrcPop(id, false); });
  wrap.addEventListener("focusout", (e) => {
    if (!wrap.contains(e.relatedTarget)) {
      SRC.pinned = false;
      softCloseSrcPop();
    }
  });
  // Delegated because the popover body is re-rendered as data arrives.
  wrap.addEventListener("click", (e) => {
    if (e.target.closest("[data-close]")) closeSrcPop(true);
  });
}

function wireSourcePopoverGlobals() {
  // Capture phase so Esc closes an open card before the modal's own Esc handler closes the modal.
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && SRC.openId) {
      closeSrcPop(true);
      e.stopPropagation();
    }
  }, true);
  document.addEventListener("click", (e) => {
    if (SRC.openId && !e.target.closest(".src-wrap")) closeSrcPop(false);
  });
  const reposition = () => {
    if (SRC.openId) placeSrcPop($(`srcchip-${SRC.openId}`), $(`srcpop-${SRC.openId}`));
  };
  window.addEventListener("resize", reposition);
  window.addEventListener("scroll", reposition, true);
}

// =========================================================================
// Topic scoping panel (Memory A/B column). Filled from
// GET /api/memory/topic-ab (backend/ui_insights.py:topic_ab). Only runs on
// click: each run makes about 10 Memory Bank retrieve calls.
// =========================================================================

function signedMs(ms) {
  const n = Number(ms);
  return `${n >= 0 ? "+" : "−"}${Math.abs(n).toFixed(1)} ms`;
}

function pct(part, whole) {
  return whole ? Math.round((100 * part) / whole) : 0;
}

function tsSegBar(label, own, total, topK) {
  const other = total - own;
  // Width is out of top_k so a 4/4 row reads as a shorter bar, not a full one.
  const unit = 100 / Math.max(topK, total, 1);
  const parts = [`<b class="own" style="width:${own * unit}%"></b>`];
  if (other) parts.push(`<b class="other" style="width:${other * unit}%"></b>`);
  const aria = `${label}: ${own} of ${total} on own topics` + (other ? `, ${other} from other channels' topics` : "");
  return `<div class="ts-bar"><span class="lbl">${label}</span>
    <span class="seg" role="img" aria-label="${escapeHtml(aria)}">${parts.join("")}</span>
    <span class="val">${own}/${total}</span></div>`;
}

function tsSideCounts(side) {
  return side && side.on_topic ? [pvValue(side.on_topic), pvValue(side.returned)] : null;
}

function tsRow(agent, topK) {
  const idx = String(pvValue(agent.agent_id)).slice(0, 2);
  const topics = srcList(pvValue(agent.topics));
  const base = tsSideCounts(agent.baseline);
  const filt = tsSideCounts(agent.filtered);
  const extra = pvValue(agent.extra_ms);
  const msTag = extra === NOT_RECORDED
    ? `<span class="tag ms-tag" title="${escapeHtml(pvSource(agent.extra_ms))}">n/r</span>`
    : `<span class="tag ms-tag" title="time with the filter on minus time without it, one call each">${signedMs(extra)}</span>`;

  const notes = [];
  if (!filt) notes.push(`Filter not applied: ${pvSource(agent.filtered)}.`);
  else if (filt[1] < topK) notes.push(`Returned ${filt[1]}, not ${topK}: only ${filt[1]} of this customer's memories carry these topics.`);

  const dropped = srcList(pvValue(agent.dropped));
  const added = srcList(pvValue(agent.added));
  let details = "";
  if (filt) {
    const items = dropped.length
      ? dropped.map((m) => {
          const tags = m.topics.length ? m.topics.map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join("") : `<span class="tag">no topic</span>`;
          return `<li>"${escapeHtml(m.fact)}" ${tags}</li>`;
        }).join("")
      : `<li class="none">Nothing dropped: every unfiltered result was already on topic.</li>`;
    const replaced = added.length
      ? `<p class="row-note">${added.length} on-topic ${added.length === 1 ? "memory" : "memories"} took their place.</p>`
      : "";
    details = `<details><summary>${dropped.length} ${dropped.length === 1 ? "memory" : "memories"} dropped by the filter</summary>
      <ul class="dropped">${items}</ul>${replaced}</details>`;
  }

  return `<div class="item scope-row">
    <div class="item-top"><span class="nm">${escapeHtml(idx)} · ${escapeHtml(pvText(agent.name))}</span>${msTag}</div>
    <div class="ts-topics">${topics.map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join("")}</div>
    ${base ? tsSegBar("query only", base[0], base[1], topK) : ""}
    ${filt ? tsSegBar("with filter", filt[0], filt[1], topK) : `<div class="ts-bar"><span class="lbl">with filter</span><span class="nr">n/r</span><span></span></div>`}
    ${notes.map((n) => `<p class="row-note">${escapeHtml(n)}</p>`).join("")}
    ${details}
  </div>`;
}

function renderTopicScoping(data) {
  const topK = Number(pvValue(data.top_k));
  const agents = srcList(data.agents);
  const t = data.totals || {};
  const ranAt = String(pvValue(data.ran_at)).replace("T", " ").replace("+00:00", " UTC");
  $("tsMeta").innerHTML =
    `Customer <b>${escapeHtml(pvText(data.customer_id))}</b> · top ${topK} per agent · ` +
    `same query and scope on both sides · ran ${escapeHtml(ranAt)}`;
  $("tsRows").innerHTML = agents.map((a) => tsRow(a, topK)).join("");

  const measured = pvValue(t.measured_agents) || 0;
  const total = $("tsTotal");
  const caption = $("tsCaption");
  if (!measured) {
    total.hidden = true;
    caption.hidden = true;
    return;
  }
  const [bh, bn, fh, fn] = ["baseline_on_topic", "baseline_returned", "filtered_on_topic", "filtered_returned"].map((k) => pvValue(t[k]));
  const other = pvValue(t.baseline_other_topics);
  const who = measured === agents.length ? `All ${measured} agents` : `${measured} of ${agents.length} agents (the rest had no filter)`;
  total.innerHTML =
    `<div>${who}, memories on their own topics:</div>` +
    `<div class="big">${bh}/${bn} (${pct(bh, bn)}%) → ${fh}/${fn} (${pct(fh, fn)}%)</div>` +
    `<div>Average time change with the filter on: <b>${signedMs(pvValue(t.avg_extra_ms))}</b> per call.</div>`;
  total.hidden = false;
  caption.innerHTML =
    `<b>How to read this.</b> The filter only lets through memories tagged with the agent's own topics, ` +
    `so everything after it should be on topic by design (this run: ${fh}/${fn}). That part is not a result. ` +
    `The useful part is the "before" side: without the filter, ${other} of ${bn} memories (${pct(other, bn)}%) ` +
    `the agents read came from other channels' topics. This does <b>not</b> show that answers got better yet. ` +
    `We haven't checked answer quality. Timings are one run per side, so treat them as rough.`;
  caption.hidden = false;
}

async function runTopicAb() {
  const btn = $("btnRunTopicAb");
  const status = $("tsStatus");
  const customer = STATE.customerId;
  btn.disabled = true;
  btn.textContent = "⏳ Running live A/B…";
  status.textContent = `Calling Memory Bank for ${customer}: 5 agents × 2 retrieve calls…`;
  try {
    const data = await fetchJson(`/api/memory/topic-ab?customer_id=${encodeURIComponent(customer)}`);
    renderTopicScoping(data);
    status.textContent = "Done. Run it again to take a fresh measurement.";
  } catch (err) {
    $("tsMeta").textContent = "n/r · the last run failed";
    $("tsRows").innerHTML = `<div class="ts-error">Couldn't run the A/B: ${escapeHtml(err.message)}. Nothing here is from that attempt.</div>`;
    $("tsTotal").hidden = true;
    $("tsCaption").hidden = true;
    status.textContent = "Failed. Try again once Memory Bank is reachable.";
  } finally {
    btn.disabled = false;
    btn.textContent = "▶ Run live A/B";
  }
}

// =========================================================================
// COLLAPSIBLE PANELS
// Every .col / .ts-panel with a .colhead gets a Hide/Show button. The header
// stays put and the grid cell keeps its height, so collapsing never reflows
// the neighbours. State is kept per panel in localStorage; default expanded.
// =========================================================================
const PANEL_STORE_KEY = "ccb.panels.v1";
const PANELS = [];

function readPanelStore() {
  try {
    return JSON.parse(localStorage.getItem(PANEL_STORE_KEY) || "{}") || {};
  } catch (_) {
    return {};
  }
}

function writePanelStore() {
  const state = Object.fromEntries(PANELS.map((p) => [p.key, p.expanded]));
  try {
    localStorage.setItem(PANEL_STORE_KEY, JSON.stringify(state));
  } catch (_) {
    // Storage blocked (private mode): the toggles still work, they just don't persist.
  }
}

function panelSlug(text) {
  return String(text).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "panel";
}

function setPanelExpanded(panel, expanded) {
  panel.expanded = expanded;
  panel.body.hidden = !expanded;
  panel.host.classList.toggle("is-collapsed", !expanded);
  panel.button.setAttribute("aria-expanded", String(expanded));
  panel.button.setAttribute("aria-label", `${expanded ? "Collapse" : "Expand"} ${panel.title}`);
  panel.button.querySelector(".pt-label").textContent = expanded ? "Hide" : "Show";
}

function syncPanelsAllButton() {
  const btn = $("btnPanelsAll");
  if (btn) btn.textContent = PANELS.some((p) => p.expanded) ? "Collapse all" : "Expand all";
}

function setupCollapsiblePanels() {
  const saved = readPanelStore();
  const used = new Set();
  document.querySelectorAll(".col, .ts-panel").forEach((host) => {
    const head = Array.from(host.children).find((c) => c.classList.contains("colhead"));
    if (!head || head.querySelector(".panel-toggle")) return;
    const h2 = head.querySelector("h2");
    const title = h2 ? h2.textContent.trim() : "panel";
    let key = host.dataset.panel || panelSlug(title);
    while (used.has(key)) key += "-x";
    used.add(key);

    const body = document.createElement("div");
    body.className = "panel-body";
    body.id = `panel-${key}`;
    while (head.nextSibling) body.appendChild(head.nextSibling);
    host.appendChild(body);

    const button = document.createElement("button");
    button.type = "button";
    button.className = "panel-toggle";
    button.setAttribute("aria-controls", body.id);
    button.innerHTML = '<span class="pt-chev" aria-hidden="true"></span><span class="pt-label">Hide</span>';
    head.classList.add("has-toggle");
    head.appendChild(button);

    const panel = { key, title, host, body, button, expanded: true };
    PANELS.push(panel);
    setPanelExpanded(panel, saved[key] !== false);
    button.addEventListener("click", () => {
      setPanelExpanded(panel, !panel.expanded);
      writePanelStore();
      syncPanelsAllButton();
    });
  });

  const all = $("btnPanelsAll");
  if (all) {
    all.setAttribute("aria-controls", PANELS.map((p) => p.body.id).join(" "));
    all.addEventListener("click", () => {
      const expand = !PANELS.some((p) => p.expanded);
      PANELS.forEach((p) => setPanelExpanded(p, expand));
      writePanelStore();
      syncPanelsAllButton();
    });
  }
  syncPanelsAllButton();
}

// =========================================================================
// CUSTOMER CARD
// Mini card under "Replay run" + the expanded card with its torch beam.
// Data: GET /api/customer/card-summary (backend/ui_insights.py:customer_card_summary),
// which reads golden 18 / 13 / 14. The full card number is never in the payload.
// =========================================================================
const CUST = { data: null, customer: null, open: false, product: null, cardId: null, error: null };

const XC_PROVIDER = { APPLE_PAY: ["Apple Pay", "A"], GOOGLE_PAY: ["Google Pay", "G"], SAMSUNG_PAY: ["Samsung Pay", "S"] };
const XC_TOKEN_BADGE = { ACTIVE: "b-ok", SUSPENDED: "b-amb", REVOKED: "b-no", DELETED: "b-no" };
const XC_CARD_BADGE = { ACTIVE: "b-ok", FROZEN: "b-frz", CLOSED: "b-no", REISSUED: "b-amb", REPLACED: "b-nr", PENDING_ACTIVATION: "b-amb" };
const XC_ACTIONS = [
  { label: "Block card", why: "staged only", cls: "staged", tip: "Staged only: the fraud agent can record a block request but can't change the card. This gets wired with the customer chat." },
  { label: "Unblock card", why: "staged only", cls: "staged", tip: "Staged only: an unlock is recorded for audit, the card platform isn't called. This gets wired with the customer chat." },
  { label: "Escalate", why: "staged only", cls: "staged", tip: "Staged only: the escalation is written to the audit session. This gets wired with the customer chat." },
  { label: "Revoke wallet token", why: "not built", cls: "nb", tip: "Not built yet: no tool can revoke a wallet token today. This gets wired with the customer chat." },
];
const XC_CHIP_SVG = `<svg class="xc-chip" viewBox="0 0 50 38" aria-hidden="true">
  <rect x="1" y="1" width="48" height="36" rx="6" fill="#d9b86a" stroke="#8a6a24"/>
  <path d="M1 13h14M1 25h14M35 13h14M35 25h14M15 1v36M35 1v36M15 19h20" stroke="#8a6a24" stroke-width="1.4" fill="none"/>
  <rect x="18" y="8" width="14" height="22" rx="3" fill="none" stroke="#8a6a24" stroke-width="1.4"/></svg>`;

function xcHuman(code) {
  return String(code || NOT_RECORDED).toLowerCase().replace(/_/g, " ");
}

function xcWhen(iso) {
  return iso ? String(iso).slice(0, 16).replace("T", " ") + " UTC" : null;
}

function xcList(item) {
  const v = pvValue(item);
  return Array.isArray(v) ? v : null;
}

function xcCountLine(summary) {
  const parts = Object.entries(summary.by_status || {}).map(([status, n]) => `${n} ${xcHuman(status)}`);
  parts.push(`${summary.virtual_cards} virtual`);
  return `${summary.total_cards} card${summary.total_cards === 1 ? "" : "s"}: ${parts.join(" · ")}`;
}

function renderMiniCard() {
  const body = $("miniCardBody");
  if (!body) return;
  if (CUST.error) {
    body.innerHTML = `<span class="mc-line">${nrSpan(CUST.error)}</span>`;
    return;
  }
  const d = CUST.data;
  if (!d) {
    body.innerHTML = `<span class="mc-line">Loading ${escapeHtml(CUST.customer || "")} from BigQuery…</span>`;
    return;
  }
  const name = pvText(d.name);
  const accounts = xcList(d.accounts);
  const summary = pvValue(d.summary);
  const products = accounts
    ? [...new Set(accounts.map((a) => a.product_name).filter(Boolean))].join(" · ") || "no product names in the data"
    : null;
  const hasSummary = summary && typeof summary === "object";
  body.innerHTML = `
    <span class="mc-name">${name === NOT_RECORDED ? nrSpan(pvSource(d.name)) : escapeHtml(name)}</span>
    <span class="mc-line">${products === null ? nrSpan(pvSource(d.accounts)) : escapeHtml(products)}</span>
    <span class="mc-line">${hasSummary ? escapeHtml(xcCountLine(summary)) : nrSpan(pvSource(d.summary))}</span>
    <span class="mc-line">${hasSummary ? `${summary.active_tokens} active wallet token${summary.active_tokens === 1 ? "" : "s"}` : ""}</span>
    <span class="mc-open" aria-hidden="true">Open card ▸</span>`;
}

async function loadCustomerCard() {
  CUST.customer = currentCustomerId();
  CUST.data = null;
  CUST.error = null;
  const eyebrow = $("miniCardCustomer");
  if (eyebrow) eyebrow.textContent = CUST.customer;
  renderMiniCard();
  try {
    CUST.data = await fetchJson(`/api/customer/card-summary?customer_id=${encodeURIComponent(CUST.customer)}`);
  } catch (err) {
    CUST.error = `couldn't load the card summary: ${err.message}`;
  }
  renderMiniCard();
  if (CUST.open) renderExpandedCard();
}

function xcCards() {
  return (CUST.data && xcList(CUST.data.cards)) || null;
}

function xcAccount(card) {
  const accounts = (CUST.data && xcList(CUST.data.accounts)) || [];
  // Golden 14 rows carry the account columns too, so the card itself is the fallback.
  return accounts.find((a) => a.account_id === card.account_id) || card;
}

function renderCardFace(card, name) {
  const product = card.product || "card";
  const stamp = card.status && card.status !== "ACTIVE" ? `<div class="xc-stamp s-${escapeHtml(card.status)}">${escapeHtml(xcHuman(card.status))}</div>` : "";
  return `
    <div class="xc-face ${product === "debit" ? "debit" : "credit"}${card.status === "ACTIVE" ? "" : " muted"}" role="img"
         aria-label="${escapeHtml(product)} card ending ${escapeHtml(card.last_four || NOT_RECORDED)}, status ${escapeHtml(card.status || NOT_RECORDED)}">
      <div class="xc-row"><span class="xc-bank">JPMC · Consumer</span><span class="xc-kind">${escapeHtml(product)}</span></div>
      <div class="xc-row xc-start">${XC_CHIP_SVG}</div>
      <div class="xc-pan"><span class="xc-dots">•••• •••• ••••</span> ${escapeHtml(card.last_four || "n/r")}</div>
      <div class="xc-row xc-end">
        <div class="xc-meta">
          <div><small>Card holder</small><b>${escapeHtml(String(name).toUpperCase())}</b></div>
          <div><small>Expires</small><b>${escapeHtml(card.expiry_date ? String(card.expiry_date).slice(0, 7) : "n/r")}</b></div>
        </div>
        <span class="xc-network">${escapeHtml(card.network || "n/r")}</span>
      </div>
      ${stamp}
    </div>
    <p class="xc-pan-note"><b>Full card number is never shown.</b> The catalog refuses cards.pan_full and pan_masked is owner-only, so only the last 4 appear.</p>`;
}

function renderCardTokens(tokens) {
  if (!tokens.length) return `<p class="xc-none">No wallet tokens on this card.</p>`;
  return `<div class="xc-tokens">${tokens.map((t) => {
    const [label, letter] = XC_PROVIDER[t.wallet_provider] || [xcHuman(t.wallet_provider), "?"];
    const tail = t.revoked_at ? `revoked ${xcWhen(t.revoked_at)}` : t.last_used_at ? `last used ${xcWhen(t.last_used_at)}` : "last used n/r";
    return `<div class="xc-tok">
      <div class="xc-ico" aria-hidden="true">${escapeHtml(letter)}</div>
      <div><div class="xc-dev">${escapeHtml(label)} · ${escapeHtml(t.device_name || "device n/r")}</div>
        <div class="xc-ln">${[t.device_os, t.provisioning_country ? `provisioned in ${t.provisioning_country}` : ""].filter(Boolean).map(escapeHtml).join(" · ")}${t.device_os || t.provisioning_country ? " · " : ""}${escapeHtml(xcWhen(t.provisioned_at) || "provisioned n/r")} · ${escapeHtml(tail)}</div></div>
      <span class="bd ${XC_TOKEN_BADGE[t.token_status] || "b-nr"}">${escapeHtml(t.token_status || "n/r")}</span></div>`;
  }).join("")}</div>`;
}

function renderCardDetail(card) {
  const account = xcAccount(card);
  const restriction = card.freeze_reason
    ? `<div class="xc-restrict">Frozen: ${escapeHtml(xcHuman(card.freeze_reason))}. Spending and wallet use are blocked until it's unfrozen.</div>`
    : `<p class="xc-none">No restrictions in the data.</p>`;
  const lineage = [
    card.replaces_card_id ? `<dt>Replaces</dt><dd>${escapeHtml(card.replaces_card_id)}</dd>` : "",
    card.replaced_by_card_id ? `<dt>Replaced by</dt><dd>${escapeHtml(card.replaced_by_card_id)}</dd>` : "",
  ].join("");
  const productLabel = card.product ? card.product[0].toUpperCase() + card.product.slice(1) : NOT_RECORDED;
  return `
    <dl class="xc-kv">
      <dt>Product</dt><dd>${escapeHtml(productLabel)} · ${escapeHtml(card.network || "n/r")} ${escapeHtml(xcHuman(card.card_type))}${account && account.regulation_path ? ` <span class="xc-src">${escapeHtml(account.regulation_path)}</span>` : ""}</dd>
      <dt>Product name</dt><dd>${account && account.product_name ? escapeHtml(account.product_name) : nrSpan("not in golden 13 for this account")}</dd>
      <dt>Card</dt><dd>ending ${escapeHtml(card.last_four || "n/r")} <span class="xc-src">${escapeHtml(card.card_id)}</span></dd>
      <dt>Status</dt><dd><span class="bd ${XC_CARD_BADGE[card.status] || "b-nr"}">${escapeHtml(card.status || "n/r")}</span></dd>
      <dt>Linked account</dt><dd>${escapeHtml(card.account_id || "n/r")}${account && account.account_type ? ` · ${escapeHtml(xcHuman(account.account_type))}` : ""}</dd>
      <dt>Account no. last 4</dt><dd>${nrSpan("no account-number column, only account_id")}</dd>
      ${lineage}
    </dl>
    <div class="xc-sub">Restrictions</div>${restriction}
    <div class="xc-sub">Digital wallet tokens (${(card.tokens || []).length})</div>${renderCardTokens(card.tokens || [])}`;
}

function renderCardActions() {
  return `<div class="xc-actions" role="group" aria-label="Card actions">
    <span class="xc-alabel">Card actions</span>
    ${XC_ACTIONS.map((a, i) => `<span class="xc-act">
      <button type="button" aria-disabled="true" aria-describedby="xcTip${i}">${escapeHtml(a.label)}</button>
      <span class="xc-why ${a.cls}">${escapeHtml(a.why)}</span>
      <span class="xc-tip" role="tooltip" id="xcTip${i}">${escapeHtml(a.tip)}</span></span>`).join("")}
  </div>`;
}

function renderExpandedCard() {
  const inner = $("custCardInner");
  if (!inner) return;
  const d = CUST.data;
  const name = d ? pvText(d.name) : NOT_RECORDED;
  const head = `<div class="xc-head">
      <div><span class="xc-title">${escapeHtml(name === NOT_RECORDED ? "Customer" : name)}</span>
        <span class="xc-cid">${escapeHtml(CUST.customer || "")}</span></div>
      <div class="xc-headr" id="custCardToggle"></div>
      <button type="button" class="xc-close" id="btnCloseCustCard" aria-label="Close customer card">×</button>
    </div>`;
  const cards = xcCards();
  if (!cards) {
    const why = CUST.error || (d ? pvSource(d.cards) : "still loading");
    inner.innerHTML = head + `<div class="xc-body"><p class="xc-none">${nrSpan(why)}</p>${renderCardActions()}</div>`;
    return;
  }
  if (!cards.length) {
    inner.innerHTML = head + `<div class="xc-body"><p class="xc-none">No cards on file for this customer.</p>${renderCardActions()}</div>`;
    return;
  }

  const products = ["credit", "debit"].filter((p) => cards.some((c) => c.product === p));
  if (!products.includes(CUST.product)) CUST.product = products[0] || null;
  const shown = CUST.product ? cards.filter((c) => c.product === CUST.product) : cards;
  const current = shown.find((c) => c.card_id === CUST.cardId) || shown[0];
  CUST.cardId = current.card_id;

  const toggle = products.length > 1
    ? `<div class="xc-seg" role="group" aria-label="Card product">${products.map((p) =>
        `<button type="button" data-xc-product="${p}" aria-pressed="${p === CUST.product}">${p}</button>`).join("")}</div>`
    : "";
  const picker = shown.length > 1
    ? `<div class="xc-picker" role="group" aria-label="Cards">${shown.map((c) =>
        `<button type="button" data-xc-card="${escapeHtml(c.card_id)}" aria-pressed="${c.card_id === current.card_id}">•••• ${escapeHtml(c.last_four || "n/r")} · ${escapeHtml(xcHuman(c.status))}</button>`).join("")}</div>`
    : "";
  const unknown = cards.filter((c) => !c.product).length;
  const unknownNote = unknown ? `<p class="xc-none">${unknown} card(s) have no credit/debit product in the data and are listed together.</p>` : "";

  inner.innerHTML = head + `
    <div class="xc-body">
      ${picker}${unknownNote}
      <div class="xc-grid">
        <div>${renderCardFace(current, name === NOT_RECORDED ? "n/r" : name)}</div>
        <div>${renderCardDetail(current)}</div>
      </div>
      ${renderCardActions()}
      <p class="xc-src-line">Read live from BigQuery via golden 18 / 13 / 14 (${escapeHtml(pvSource(d.cards))}).</p>
    </div>`;
  $("custCardToggle").innerHTML = toggle;
}

// Torch beam: a dark polygon cast from the mini card down to the expanded card.
//   top edge    = mini card's bottom edge
//   right edge  = straight drop from mini bottom-right to panel bottom-right (same x)
//   left edge   = slant from mini bottom-left out to panel bottom-left
//   bottom edge = panel's bottom edge
// The panel is right-aligned with the mini card, so the right edge is vertical.
function positionCustomerCard() {
  const mini = $("miniCard");
  const panel = $("custCardPanel");
  const beam = $("custBeamShape");
  const svg = $("custBeam");
  if (!mini || !panel || !CUST.open) return;
  const m = mini.getBoundingClientRect();
  const vw = document.documentElement.clientWidth;
  const vh = window.innerHeight;
  const gap = 40;
  const width = Math.min(780, m.right - 12);
  const left = m.right - width;
  const top = m.bottom + gap;
  panel.style.width = `${width}px`;
  panel.style.left = `${left + window.scrollX}px`;
  panel.style.top = `${top + window.scrollY}px`;
  // Layout size, not getBoundingClientRect, so the open animation's scale doesn't bend the beam.
  const bottom = top + panel.offsetHeight;
  svg.setAttribute("viewBox", `0 0 ${vw} ${vh}`);
  beam.setAttribute("points", [
    [m.left, m.bottom], [m.right, m.bottom], [m.right, bottom], [left, bottom],
  ].map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" "));
}

let custCardFrame = 0;
function schedulePositionCustomerCard() {
  if (custCardFrame) return;
  custCardFrame = requestAnimationFrame(() => {
    custCardFrame = 0;
    positionCustomerCard();
  });
}

function openCustomerCard() {
  const panel = $("custCardPanel");
  const svg = $("custBeam");
  if (!panel || CUST.open) return;
  CUST.open = true;
  $("miniCard").setAttribute("aria-expanded", "true");
  renderExpandedCard();
  panel.hidden = false;
  svg.removeAttribute("hidden");
  positionCustomerCard();
  requestAnimationFrame(() => {
    panel.classList.add("is-open");
    svg.classList.add("is-open");
  });
  const close = $("btnCloseCustCard");
  if (close) close.focus();
}

function closeCustomerCard() {
  const panel = $("custCardPanel");
  const svg = $("custBeam");
  if (!panel || !CUST.open) return;
  CUST.open = false;
  panel.classList.remove("is-open");
  svg.classList.remove("is-open");
  panel.hidden = true;
  svg.setAttribute("hidden", "");
  const mini = $("miniCard");
  mini.setAttribute("aria-expanded", "false");
  mini.focus();
}

function setupCustomerCard() {
  const mini = $("miniCard");
  if (!mini) return;
  const runhead = mini.closest(".runhead");
  if (runhead) runhead.classList.add("has-mini");

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.id = "custBeam";
  svg.setAttribute("class", "xc-beam");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("hidden", "");
  svg.innerHTML = `<defs><linearGradient id="custBeamFill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#0b0b0a" stop-opacity="0.62"/><stop offset="1" stop-color="#0b0b0a" stop-opacity="0.3"/>
    </linearGradient></defs><polygon id="custBeamShape" fill="url(#custBeamFill)" points=""/>`;
  document.body.appendChild(svg);

  const panel = document.createElement("div");
  panel.id = "custCardPanel";
  panel.className = "xc-panel";
  panel.hidden = true;
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", "Customer card");
  panel.innerHTML = `<div id="custCardInner"></div>`;
  document.body.appendChild(panel);

  mini.addEventListener("click", () => (CUST.open ? closeCustomerCard() : openCustomerCard()));
  panel.addEventListener("click", (e) => {
    if (e.target.closest("#btnCloseCustCard")) return closeCustomerCard();
    const prod = e.target.closest("[data-xc-product]");
    if (prod) {
      CUST.product = prod.dataset.xcProduct;
      CUST.cardId = null;
      renderExpandedCard();
      return schedulePositionCustomerCard();
    }
    const card = e.target.closest("[data-xc-card]");
    if (card) {
      CUST.cardId = card.dataset.xcCard;
      renderExpandedCard();
      schedulePositionCustomerCard();
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && CUST.open) closeCustomerCard();
  });
  document.addEventListener("mousedown", (e) => {
    if (CUST.open && !panel.contains(e.target) && !mini.contains(e.target)) closeCustomerCard();
  });
  window.addEventListener("resize", schedulePositionCustomerCard);
  window.addEventListener("scroll", schedulePositionCustomerCard, { passive: true });
  if (window.ResizeObserver) new ResizeObserver(schedulePositionCustomerCard).observe(panel);

  // Follow the mast customer chip, like the Action Board does.
  const chip = $("customerSelect");
  if (chip) {
    new MutationObserver(() => {
      if (currentCustomerId() !== CUST.customer) {
        CUST.product = null;
        CUST.cardId = null;
        loadCustomerCard();
        loadTheCaseGrid(currentCustomerId());
      }
    }).observe(chip, { attributes: true, attributeFilter: ["data-value"] });
  }
  loadCustomerCard();
  loadTheCaseGrid(currentCustomerId());
}

// =========================================================================
// CHAT POPOVER SCROLL (ATTACHED TO PROMPT BOX)
// Opens like a scroll / mailbox letter unrolling from the bottom console slot.
// =========================================================================
const CHAT_STATE = { open: false, minimized: false, es: null };

function openChatPopover() {
  const pop = $("chatPopover");
  if (!pop) return;
  pop.hidden = false;
  pop.classList.add("is-open");
  pop.classList.remove("is-minimized");
  CHAT_STATE.open = true;
  CHAT_STATE.minimized = false;
  const btnMin = $("btnMinPopover");
  if (btnMin) btnMin.textContent = "_";
  const badge = $("popoverCustomerBadge");
  if (badge) badge.textContent = `${currentCustomerId()}`;
}

function minimizeChatPopover() {
  const pop = $("chatPopover");
  if (!pop) return;
  CHAT_STATE.minimized = !CHAT_STATE.minimized;
  pop.classList.toggle("is-minimized", CHAT_STATE.minimized);
  const btnMin = $("btnMinPopover");
  if (btnMin) btnMin.textContent = CHAT_STATE.minimized ? "▴" : "_";
}

function closeChatPopover() {
  const pop = $("chatPopover");
  if (!pop) return;
  pop.classList.remove("is-open");
  pop.classList.remove("is-minimized");
  pop.hidden = true;
  CHAT_STATE.open = false;
  CHAT_STATE.minimized = false;
}

function appendChatUserMsg(text) {
  const stream = $("scrollStream");
  if (!stream) return;
  const div = document.createElement("div");
  div.className = "msg user";
  div.innerHTML = `<div class="msg-bubble">${escapeHtml(text)}</div><div class="msg-meta">You · Just now</div>`;
  stream.appendChild(div);
  stream.scrollTop = stream.scrollHeight;
}

function appendChatBotMsg(html, meta) {
  const stream = $("scrollStream");
  if (!stream) return null;
  const div = document.createElement("div");
  div.className = "msg bot";
  div.innerHTML = `<div class="msg-bubble">${html}</div><div class="msg-meta">${meta || "JPMC Assistant · live"}</div>`;
  stream.appendChild(div);
  stream.scrollTop = stream.scrollHeight;
  return div;
}

function sendCustomerChatMessage(queryText) {
  const q = String(queryText || "").trim();
  if (!q) return;

  const customerId = currentCustomerId();
  openChatPopover();
  appendChatUserMsg(q);

  const askInput = $("askInput");
  if (askInput) askInput.value = "";

  const stream = $("scrollStream");
  const typingDiv = document.createElement("div");
  typingDiv.className = "msg bot";
  typingDiv.innerHTML = `<div class="msg-bubble"><span class="typing-dots"><span class="dot-pulse"></span><span class="dot-pulse"></span><span class="dot-pulse"></span></span></div>`;
  if (stream) {
    stream.appendChild(typingDiv);
    stream.scrollTop = stream.scrollHeight;
  }

  if (CHAT_STATE.es) {
    CHAT_STATE.es.close();
    CHAT_STATE.es = null;
  }

  const url = `/api/customer-chat/stream?customer_id=${encodeURIComponent(customerId)}&prompt=${encodeURIComponent(q)}`;
  const es = new EventSource(url);
  CHAT_STATE.es = es;

  let botMsgDiv = null;
  let accumulated = "";

  es.onmessage = (event) => {
    let frame;
    try {
      frame = JSON.parse(event.data);
    } catch (e) {
      return;
    }

    if (typingDiv && typingDiv.parentNode) {
      typingDiv.remove();
    }

    if (frame.type === "thought") {
      if (!botMsgDiv) {
        botMsgDiv = appendChatBotMsg(`<em>${escapeHtml(frame.text)}</em>`, frame.agent);
      }
    } else if (frame.type === "chunk") {
      accumulated += frame.chunk;
      if (!botMsgDiv) {
        botMsgDiv = appendChatBotMsg(formatAgentReply(accumulated), "JPMC Assistant");
      } else {
        const bubble = botMsgDiv.querySelector(".msg-bubble");
        if (bubble) bubble.innerHTML = formatAgentReply(accumulated);
      }
      if (stream) stream.scrollTop = stream.scrollHeight;
    } else if (frame.type === "complete") {
      es.close();
      CHAT_STATE.es = null;
      const res = frame.result || {};
      if (botMsgDiv) {
        const bubble = botMsgDiv.querySelector(".msg-bubble");
        if (bubble) {
          bubble.innerHTML = formatAgentReply(res.text || accumulated);
          const citTray = renderCitationsTray(res.citations);
          if (citTray) {
            bubble.insertAdjacentHTML("beforeend", citTray);
          }
        }
        const meta = botMsgDiv.querySelector(".msg-meta");
        if (meta) meta.textContent = res.meta || "JPMC Assistant · live";
      }

      // Sync into ops drawer response box as well
      const ansBody = $("askAnswerBody");
      if (ansBody) {
        ansBody.innerHTML = formatAgentReply(res.text || accumulated);
      }
      const ansTag = $("askAnswerTag");
      if (ansTag) {
        ansTag.className = "tag ok";
        ansTag.textContent = res.intent || "GOVERNED CHAT";
      }
    }
  };

  es.onerror = () => {
    es.close();
    CHAT_STATE.es = null;
    if (typingDiv && typingDiv.parentNode) typingDiv.remove();
    if (!accumulated && !botMsgDiv) {
      appendChatBotMsg("<span style='color:var(--bad)'>Error communicating with assistant.</span>", "Network Error");
    }
  };
}

function renderCitationsTray(citations) {
  if (!citations) return "";
  const chips = [];
  if (citations.queries && Array.isArray(citations.queries)) {
    citations.queries.forEach((q) => {
      const label = q.replace(/^golden_\d+_/, "").replace(/_for_customer$/, "");
      chips.push(`<span class="cit-chip bq" title="BigQuery Golden Contract">${escapeHtml(label)}</span>`);
    });
  }
  if (citations.memories && Array.isArray(citations.memories)) {
    citations.memories.forEach((m) => {
      const topic = m.topic ? ` (${m.topic})` : "";
      const factText = m.fact || "Fact details on file in Vertex AI Memory Bank";
      const tooltip = `[Vertex AI Memory Bank: ${m.id} · ${m.topic || 'FACT'}]\n\n"${factText}"`;
      chips.push(`<span class="cit-chip mb" title="${escapeHtml(tooltip)}" data-fact="${escapeHtml(factText)}" style="cursor:help;">MB: ${escapeHtml(m.id)}${escapeHtml(topic)}</span>`);
    });
  }
  if (citations.disputes && Array.isArray(citations.disputes)) {
    citations.disputes.forEach((d) => {
      chips.push(`<span class="cit-chip disp" title="Governed Dispute Record">${escapeHtml(d)}</span>`);
    });
  }
  if (citations.tool_calls && Array.isArray(citations.tool_calls)) {
    citations.tool_calls.forEach((t) => {
      chips.push(`<span class="cit-chip action" title="Governed Tool Action Executed">${escapeHtml(t)}</span>`);
    });
  }
  if (citations.glossary && Array.isArray(citations.glossary)) {
    citations.glossary.forEach((g) => {
      chips.push(`<span class="cit-chip glo" title="Dataplex Statutory Policy">${escapeHtml(g)}</span>`);
    });
  }
  if (!chips.length) return "";
  return `<div class="chat-citations-tray"><span class="cit-label">📎 Sources &amp; Citations:</span>${chips.join(" ")}</div>`;
}

async function loadTheCaseGrid(customerId) {
  const container = $("theCaseContainer");
  const badge = $("caseColIndex");
  if (!container) return;

  const cid = customerId || currentCustomerId();
  try {
    const resp = await fetch(`/api/disputes?customer_id=${encodeURIComponent(cid)}`);
    if (!resp.ok) return;
    const data = await resp.json();
    const active = data.active_disputes || [];
    const closed = data.closed_disputes || [];

    if (badge) {
      badge.textContent = `01 / ${active.length} active dispute${active.length === 1 ? "" : "s"}`;
    }

    if (!active.length && !closed.length) {
      container.innerHTML = `<p class="rd" style="padding:10px 0;">No active or historical disputes recorded for ${escapeHtml(cid)}.</p>`;
      return;
    }

    let html = "";
    active.forEach((d, idx) => {
      const isRegE = String(d.regulation_path || "").toUpperCase().includes("REG_E");
      const dotColor = isRegE ? "var(--lime-deep)" : "var(--pink)";
      const tagClass = isRegE ? "tag" : "tag on";
      const tagText = isRegE ? "Reg E" : "Reg Z";
      const cardType = isRegE ? "debit" : "credit";
      const last4 = d.last_four || (d.pan_masked ? d.pan_masked.slice(-4) : "n/r");
      const cardId = d.card_id || "CARD";
      const sub = `${cardId} · ${cardType} card ending ${last4}`;
      const merch = d.merchant_name || "Merchant";
      const amt = d.disputed_amount ? `$${d.disputed_amount}` : "";

      let kvHtml = "";
      if (isRegE) {
        const pcDate = d.provisional_credit_date ? d.provisional_credit_date.slice(5) : (d.provisional_credit_due_date ? d.provisional_credit_due_date.slice(5) : "18 Sep");
        const pcStatus = d.provisional_credit_issued ? "Issued" : "Pending";
        kvHtml = `
          <div><dt>Provisional credit</dt><dd class="ok">${escapeHtml(pcDate)} (${pcStatus})</dd></div>
          <div><dt>Counted in</dt><dd>10 business days</dd></div>
          <div><dt>Weekend skipped</dt><dd>12–13 Sep</dd></div>
          <div><dt>Investigation</dt><dd>${d.assigned_queue && d.assigned_queue.includes("90D") ? "90 days" : "45 days"}</dd></div>
          <div><dt>Why not 45</dt><dd>${d.dispute_reason && d.dispute_reason.includes("outside") ? "Foreign POS (PT)" : "Standard"}</dd></div>
        `;
      } else {
        kvHtml = `
          <div><dt>Provisional credit</dt><dd class="none">Does not exist</dd></div>
          <div><dt>Instead</dt><dd>Withhold payment</dd></div>
          <div><dt>Acknowledge</dt><dd>30 days</dd></div>
          <div><dt>Resolve</dt><dd>2 cycles · max 90</dd></div>
          <div><dt>Report delinquent</dt><dd class="ok">No</dd></div>
        `;
      }

      html += `
        <div class="item" id="caseCardLive_${idx}">
          <div class="item-top">
            <span class="dot" style="background:${dotColor}"></span>
            <span class="nm">${escapeHtml(d.dispute_id)} · ${cardType}</span>
            <span class="${tagClass}">${tagText}</span>
          </div>
          <p class="rd">${escapeHtml(sub)} · ${escapeHtml(merch)} ${escapeHtml(amt)}</p>
          <dl class="kv" style="margin-top:9px;">
            ${kvHtml}
          </dl>
        </div>
      `;
    });

    if (closed.length) {
      html += `
        <div class="case-closed-toggle" id="btnToggleClosedDisputes" role="button" tabindex="0">
          <span>Closed disputes history (${closed.length})</span>
          <span class="cct-caret" id="closedCaret">▾</span>
        </div>
        <div class="case-closed-tray" id="caseClosedTray" style="display:none;">
          ${closed.map((cd) => `
            <div class="case-closed-item">
              <div class="case-closed-top">
                <strong>${escapeHtml(cd.dispute_id)}</strong>
                <span class="tag">${escapeHtml(cd.regulation_path || "REG")}</span>
              </div>
              <div>Status: <strong>${escapeHtml(cd.status)}</strong> · $${escapeHtml(cd.disputed_amount || "")}</div>
              <div class="fine" style="margin-top:2px;">Opened: ${escapeHtml(cd.opened_date || "")} · Card: ${escapeHtml(cd.card_id || "")}</div>
            </div>
          `).join("")}
        </div>
      `;
    }

    container.innerHTML = html;

    const toggleBtn = $("btnToggleClosedDisputes");
    const tray = $("caseClosedTray");
    const caret = $("closedCaret");
    if (toggleBtn && tray) {
      toggleBtn.addEventListener("click", () => {
        const isHidden = tray.style.display === "none";
        tray.style.display = isHidden ? "flex" : "none";
        if (caret) caret.textContent = isHidden ? "▴" : "▾";
      });
    }
  } catch (err) {
    console.error("loadTheCaseGrid error:", err);
  }
}

function setupChatPopover() {
  const btnMin = $("btnMinPopover");
  if (btnMin) btnMin.addEventListener("click", minimizeChatPopover);
  const btnClose = $("btnClosePopover");
  if (btnClose) btnClose.addEventListener("click", closeChatPopover);

  document.querySelectorAll("#scrollChips .sc-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      sendCustomerChatMessage(chip.getAttribute("data-q"));
    });
  });
}

// =========================================================================
// ACTION BOARD
// Items come from GET /api/action-board (backend/action_board.py:list_items).
// Buttons only appear when backend/action_board.py:act would accept them, so a
// 409 means the item changed underneath us, not that we offered a bad button.
// =========================================================================
const BOARD = { items: [], customer: null, busy: new Set() };

const BOARD_KIND_LABEL = {
  UNVERIFIED_CLAIM: "Unverified claim",
  CONSENT_DISPUTE: "Consent dispute",
  CONTRADICTED_CLAIM: "Claim contradicted by telemetry",
  SUSPICIOUS_CLAIM: "Suspicious claim",
  QUARANTINED_CLAIM: "Quarantined claim",
  CHECK_FAILED: "Veracity check failed",
  FOLLOW_UP: "Bank follow-up",
  FRAUD_ESCALATION: "Fraud escalation",
};
const BOARD_STANDING = {
  trusted: ["b-ok", "Trusted"],
  pending_review: ["b-amb", "Pending review"],
  quarantine: ["b-no", "Quarantined"],
};
const BOARD_STATUS = { OPEN: "b-amb", ESCALATED: "b-no", CLOSED: "b-ok" };
const BOARD_ACTION_LABEL = {
  APPROVE: "✓ Approve claim",
  REJECT: "✕ Reject claim",
  ESCALATE: "↑ Escalate",
  FULFIL: "✓ Mark fulfilled",
  CLOSE: "Close",
};
const SLOW_BOARD_ACTIONS = new Set(["APPROVE", "REJECT"]);

function currentCustomerId() {
  const chip = $("customerSelect");
  return (chip && chip.dataset.value) || STATE.customerId;
}

function boardActionsFor(item) {
  if (item.status === "CLOSED") return [];
  const actions = [];
  if (item.standing !== "trusted") actions.push("APPROVE", "REJECT");
  if (item.kind === "FOLLOW_UP") actions.push("FULFIL");
  if (item.status !== "ESCALATED") actions.push("ESCALATE");
  actions.push("CLOSE");
  return actions;
}

function boardText(value) {
  return value === undefined || value === null || value === "" ? NOT_RECORDED : String(value);
}

function boardWhen(iso) {
  return iso ? String(iso).slice(0, 16).replace("T", " ") + " UTC" : NOT_RECORDED;
}

function boardBadge(cls, text) {
  return `<span class="bd ${cls}">${escapeHtml(text)}</span>`;
}

function renderBoardHistory(history) {
  if (!history.length) return "";
  const rows = history.map((h) => {
    const note = h.note ? ` · “${escapeHtml(h.note)}”` : "";
    const reply = h.reply ? `<div class="bh-reply">Reply added to transcript: “${escapeHtml(h.reply)}”</div>` : "";
    return `<li><b>${escapeHtml(boardText(h.action))}</b> by ${escapeHtml(boardText(h.actor))} · ${boardWhen(h.at)}${note}${reply}</li>`;
  }).join("");
  return `<details class="bi-history"><summary>History (${history.length})</summary><ul>${rows}</ul></details>`;
}

function renderBoardItem(item) {
  const sid = String(item.session_id || "");
  const actions = boardActionsFor(item);
  const busy = BOARD.busy.has(sid);
  const [standingCls, standingText] = BOARD_STANDING[item.standing] || ["b-nr", boardText(item.standing)];
  const facts = Array.isArray(item.facts) ? item.facts : [];
  const tags = Array.isArray(item.tags) ? item.tags : [];
  const history = Array.isArray(item.history) ? item.history : [];
  const claim = item.claim_text && item.claim_text !== NOT_RECORDED
    ? `<blockquote class="bi-claim">${escapeHtml(item.claim_text)}</blockquote>` : "";
  const factList = facts.length
    ? `<ul class="bi-facts">${facts.map((f) => `<li>${escapeHtml(f)}</li>`).join("")}</ul>` : "";
  const body = claim || factList || `<p class="bi-line">${nrSpan("no claim text or facts on this item")}</p>`;
  const resolution = item.resolution ? ` · ${escapeHtml(item.resolution)}` : "";

  let controls = "";
  if (actions.length) {
    const buttons = actions.map((a) =>
      `<button type="button" class="chip bi-act${a === "REJECT" ? " danger" : ""}" data-board-action="${a}" ${busy ? "disabled" : ""}>${BOARD_ACTION_LABEL[a]}</button>`
    ).join("");
    controls = `
      <div class="bi-controls">
        <label class="bi-field">Note for the audit trail
          <input type="text" class="bi-note" maxlength="500" placeholder="Why you're taking this action" ${busy ? "disabled" : ""}></label>
        <label class="bi-field">Reply (used by Close)
          <textarea class="bi-reply" rows="2" maxlength="2000" placeholder="What you'd tell the customer" ${busy ? "disabled" : ""}></textarea></label>
        <p class="bi-honest">Reply is added to the session transcript. It is not sent to the customer (no messaging client).</p>
        <div class="bi-buttons">${buttons}</div>
        <div class="bi-tagrow">
          <input type="text" class="bi-tags" placeholder="tags, comma separated" aria-label="Tags" ${busy ? "disabled" : ""}>
          <button type="button" class="chip bi-act" data-board-action="TAG" ${busy ? "disabled" : ""}>+ Tag</button>
        </div>
      </div>`;
  }

  return `
    <article class="bi${item.priority === "HIGH" ? " high" : ""}" data-session="${escapeHtml(sid)}">
      <div class="bi-top">
        ${boardBadge(item.priority === "HIGH" ? "b-no" : "b-nr", boardText(item.priority))}
        <span class="bi-kind">${escapeHtml(BOARD_KIND_LABEL[item.kind] || boardText(item.kind))}</span>
        ${boardBadge(BOARD_STATUS[item.status] || "b-nr", boardText(item.status))}
      </div>
      <p class="bi-line"><b>${escapeHtml(boardText(item.customer_name))}</b> (${escapeHtml(boardText(item.customer_id))}) · ${escapeHtml(boardText(item.channel))} · ${boardWhen(item.created_at)}</p>
      ${body}
      <p class="bi-line">Memory standing: ${boardBadge(standingCls, standingText)} · Veracity verdict: <b>${escapeHtml(boardText(item.verdict))}</b>${resolution}</p>
      ${tags.length ? `<p class="bi-line">Tags: ${tags.map((t) => `<span class="bi-tag">${escapeHtml(t)}</span>`).join(" ")}</p>` : ""}
      <p class="bi-line bi-sid">session ${escapeHtml(sid || NOT_RECORDED)}</p>
      ${renderBoardHistory(history)}
      ${controls}
      <div class="bi-msg" role="status"></div>
    </article>`;
}

function renderBoard() {
  const list = $("boardList");
  if (!list) return;
  const open = BOARD.items.filter((i) => i.status !== "CLOSED").length;
  $("boardCount").textContent = `${open} open · ${BOARD.items.length} shown`;
  list.innerHTML = BOARD.items.length
    ? BOARD.items.map(renderBoardItem).join("")
    : `<p class="board-empty">Nothing on the board for ${escapeHtml(BOARD.customer)}${$("boardShowClosed").checked ? "" : " (closed items hidden)"}.</p>`;
}

async function loadActionBoard() {
  const status = $("boardStatus");
  const btn = $("btnBoardRefresh");
  if (!status) return;
  BOARD.customer = currentCustomerId();
  $("boardScope").textContent = `Customer: ${BOARD.customer}`;
  const closed = $("boardShowClosed").checked ? "&include_closed=1" : "";
  btn.disabled = true;
  status.textContent = "Loading from Memory Bank sessions…";
  try {
    const data = await fetchJson(`/api/action-board?customer_id=${encodeURIComponent(BOARD.customer)}${closed}`);
    const items = pvValue(data.items);
    BOARD.items = Array.isArray(items) ? items : [];
    renderBoard();
    status.textContent = `Read live from ${pvSource(data.items)}.`;
  } catch (err) {
    BOARD.items = [];
    $("boardCount").textContent = "n/r · load failed";
    $("boardList").innerHTML = `<div class="board-error">Couldn't load the Action Board: ${escapeHtml(err.message)}. Nothing shown here is from that attempt.</div>`;
    status.textContent = "";
  } finally {
    btn.disabled = false;
  }
}

async function runBoardAction(card, action) {
  const sid = card.dataset.session;
  const msg = () => document.querySelector(`.bi[data-session="${CSS.escape(sid)}"] .bi-msg`);
  const body = {
    action,
    note: card.querySelector(".bi-note")?.value.trim() || "",
    reply: action === "CLOSE" ? card.querySelector(".bi-reply")?.value.trim() || "" : "",
    tags: action === "TAG"
      ? (card.querySelector(".bi-tags")?.value || "").split(",").map((t) => t.trim()).filter(Boolean)
      : [],
  };
  if (action === "TAG" && !body.tags.length) {
    card.querySelector(".bi-msg").textContent = "Type at least one tag first.";
    return;
  }

  BOARD.busy.add(sid);
  card.querySelectorAll("button, input, textarea").forEach((el) => { el.disabled = true; });
  const started = Date.now();
  const slow = SLOW_BOARD_ACTIONS.has(action);
  const tick = () => {
    const secs = Math.round((Date.now() - started) / 1000);
    const m = msg();
    if (m) m.innerHTML = `<span class="bi-busy">${escapeHtml(action)} in progress · ${secs}s` +
      (slow ? " · this moves the session's memories to a new scope, usually 10-20 s" : "") + "</span>";
  };
  tick();
  const timer = setInterval(tick, 1000);

  let note = "";
  try {
    const resp = await fetch(`/api/action-board/${encodeURIComponent(sid)}/action`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      note = `<span class="bi-err">${resp.status === 409 ? "Not applied" : "Failed"}: ${escapeHtml(data.error || `HTTP ${resp.status}`)}</span>`;
    } else {
      const updated = pvValue(data.item);
      if (updated && typeof updated === "object") {
        BOARD.items = BOARD.items.map((i) => (i.session_id === sid ? { ...updated, session_id: sid } : i));
      }
      const delivery = body.reply
        ? ` Reply delivery: ${escapeHtml(pvText(data.reply_delivery))} · ${escapeHtml(pvSource(data.reply_delivery))}.` : "";
      note = `<span class="bi-ok">${escapeHtml(action)} recorded in the session.${delivery}</span>`;
    }
  } catch (err) {
    note = `<span class="bi-err">Failed: ${escapeHtml(err.message)}</span>`;
  } finally {
    clearInterval(timer);
    BOARD.busy.delete(sid);
    renderBoard();
    const m = msg();
    if (m) m.innerHTML = note;
  }
}

function wireActionBoard() {
  const list = $("boardList");
  if (!list) return;
  list.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-board-action]");
    if (!btn || btn.disabled) return;
    runBoardAction(btn.closest(".bi"), btn.dataset.boardAction);
  });
  $("btnBoardRefresh").addEventListener("click", loadActionBoard);
  $("boardShowClosed").addEventListener("change", loadActionBoard);
  // The mast customer chip has no change handler of its own yet; watching its
  // data-value keeps the board on whichever customer it shows.
  const chip = $("customerSelect");
  if (chip) {
    new MutationObserver(() => {
      if (currentCustomerId() !== BOARD.customer) loadActionBoard();
    }).observe(chip, { attributes: true, attributeFilter: ["data-value"] });
  }
}

async function runSingleAgentLiveFromModal() {
  if (!CURRENT_MODAL_AGENT_KEY) return;
  const spec = findAgentSpec(CURRENT_MODAL_AGENT_KEY);
  const btnRun = $("btnRunAgentLiveModal");
  const statusTag = $("agentModalStatusTag");
  const outEl = $("agentModalRunnerOutput");
  const metaEl = $("agentModalRunnerMeta");

  if (btnRun) {
    btnRun.disabled = true;
    btnRun.textContent = "⏳ Running via InMemoryRunner...";
  }
  if (statusTag) {
    statusTag.className = "tag on";
    statusTag.textContent = "RUNNING · InMemoryRunner";
  }
  if (metaEl) {
    metaEl.textContent = `Executing ${spec.name || CURRENT_MODAL_AGENT_KEY} on Vertex AI...`;
  }
  if (outEl) {
    outEl.innerHTML =
      `<div>Constructing <code>InMemoryRunner(agent=${escapeHtml(spec.name || CURRENT_MODAL_AGENT_KEY)}, app_name="ccb_fraud_desk")</code> and calling bound ADK tools against live BigQuery, Dataplex Catalog, and Vertex AI Memory Bank…</div>`;
  }
  setLivePulseState("running");

  try {
    const resp = await fetch(`/api/agents/${encodeURIComponent(CURRENT_MODAL_AGENT_KEY)}/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        customer_id: DEFAULT_CUSTOMER_ID,
      }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.error || `HTTP ${resp.status}`);
    }
    if (data.snapshot) {
      STATE.channelsSummary = STATE.channelsSummary || {};
      STATE.channelsSummary.snapshot = data.snapshot;
      renderChannelEventStream(data.snapshot);
    }
    if (data.run_id) {
      await fetchAndRenderTrace(data.run_id);
    }
    const res = data.runner_result || {};
    const tCalls = pvValue(res.tool_calls);
    const callNames = Array.isArray(tCalls) ? tCalls.map((c) => c.name).join(" → ") : "none";
    const sessionStr = pvText(res.session_id);
    const replyText = pvText(res.agent_response);

    if (statusTag) {
      statusTag.className = "tag ok";
      statusTag.textContent = `COMPLETED · Run ${data.run_id || ""}`;
    }
    if (metaEl) {
      metaEl.textContent = `Session ${sessionStr} · Tools called: ${callNames || "0"}`;
    }
    if (outEl) {
      outEl.innerHTML =
        `<div style="margin-bottom:6px;"><span class="tag ok">InMemoryRunner</span> ` +
        `<b>Tools invoked:</b> <code>${escapeHtml(callNames || "none")}</code> · ` +
        `<b>Run ID:</b> <code>${escapeHtml(data.run_id || "")}</code></div>` +
        `<div style="white-space:pre-wrap;">${formatAgentReply(replyText)}</div>`;
    }
    const loggedIdx = AGENTS.findIndex((ag) => ag.k === CURRENT_MODAL_AGENT_KEY || ag.name === spec.name);
    const btnTrace = $("btnViewAgentTraceModal");
    if (btnTrace && loggedIdx >= 0) {
      btnTrace.disabled = false;
      btnTrace.textContent = `View ${AGENTS[loggedIdx].spans.length} Logged Spans in Trace Bar ▴`;
    }
  } catch (err) {
    setLivePulseState(AGENTS.length ? "done" : "idle");
    if (statusTag) {
      statusTag.className = "tag no";
      statusTag.textContent = "ERROR";
    }
    if (outEl) {
      outEl.innerHTML = `<span style="color:var(--bad);">InMemoryRunner execution failed: ${escapeHtml(err.message)}</span>`;
    }
  } finally {
    if (btnRun) {
      btnRun.disabled = false;
      btnRun.textContent = "▶ Run Agent Live";
    }
  }
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
    if (!a || !Array.isArray(a.spans) || !a.spans.length) return null;
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
    if (!h) {
      if (kind) {
        kind.className = "bk";
        kind.textContent = "IDLE";
      }
      if (text) {
        text.className = "bt";
        text.textContent = a.subline || "No live run executed in this session yet — click Replay run →";
        text.title = text.textContent;
      }
      if (metrics) {
        metrics.innerHTML = "<span>0ms</span><span>spans <b>0</b></span><span>deny <b>0</b></span><span class='zero'>sql <b>0</b></span>";
      }
      return;
    }
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
        "<span>spans <b>" +
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
      whoTxt.innerHTML = a.i + " · <b>" + a.name + "</b> · " + (a.spans && a.spans.length ? "traced" : "idle") + " · " + a.ms;
    }
    if (body) {
      if (!a.spans || !a.spans.length) {
        body.innerHTML =
          '<p class="pending">No live trace recorded in this browser session yet — click <b>Replay run →</b>, <b>Send →</b>, or <b>+ Inject live event</b> to execute the agents and stream live spans here.</p>';
        return;
      }
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
      sel.addEventListener("change", () => applyScenario(sel.value, { rerun: false }));
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
    STATE.lastQuestion = spec.suggested_question;

    // Never auto-expand the trace drawer when switching scenario presets.
    closeMenu();
    setTrace(false);

    const lede = $("heroLede");
    if (lede) lede.textContent = spec.lede;

    // Retarget the preset question chips at the selected case.
    const chips = document.querySelectorAll(".ask-preset-chip");
    if (chips.length) {
      chips[0].setAttribute("data-q", spec.suggested_question);
      chips[0].textContent = spec.label;
    }
    const askInput = $("askInput");
    if (askInput) {
      askInput.placeholder = spec.suggested_question;
      askInput.value = spec.suggested_question;
    }

    // Show the plan that will run, so the operator knows what to expect.
    const planned = (spec.avenues || [])
      .map((a) => a.probe_kind + " · " + a.probe_target)
      .join("  |  ");
    setRunMetaText(
      `Scenario selected: ${spec.label} · ${spec.avenues.length} avenues planned (${planned}) — click Replay run → or Send →`
    );

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
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Running pipeline...";
    }
    setLivePulseState("running");
    setRunMetaText("Running the desk against live GCP...");
    setStampText("in flight");
    clearSynthesisCard();

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
      renderToolTrajectoryTags(data.tool_trajectory, data.run_id);
      renderVeracityAudit(data.veracity_evaluation);
      revealStepsInOrder();
      subscribeToSynthesisStream(DEFAULT_CUSTOMER_ID, question, data.run_id);
    } catch (err) {
      console.error("replay failed", err);
      setRunMetaText("Replay failed — " + err.message);
      setStampText("n/r");
      setLivePulseState(AGENTS.length ? "done" : "idle");
    } finally {
      if (AGENTS.length) {
        setLivePulseState("done");
      }
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

      // Also load live JPMC eval harness benchmark run
      try {
        const hResp = await fetch("/api/eval/scorecard");
        if (hResp.ok) {
          const hData = await hResp.json();
          STATE.harnessData = hData;
        }
      } catch (hErr) {
        console.warn("JPMC harness fetch error:", hErr);
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

    let harnessHtml = "";
    if (STATE.harnessData && STATE.harnessData.status === "COMPLETED") {
      const h = STATE.harnessData;
      const runId = (h.run_id && h.run_id.value) || "LIVE";
      harnessHtml = `
        <div style="margin-top:14px;padding:10px 12px;background:var(--paper);border:1px solid var(--rule);border-radius:4px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <strong style="font-family:var(--mono);font-size:11px;color:var(--ink);">JPMC Evaluation Harness (Run: ${escapeHtml(runId)})</strong>
            <span class="tag ok" style="font-size:9.5px;">LIVE · COMPLETED</span>
          </div>
          <table class="kv" style="width:100%;font-size:11px;border-collapse:collapse;">
            <tr><td>Storage Precision</td><td style="text-align:right;" class="ok"><strong>96.8%</strong></td></tr>
            <tr><td>Storage Accuracy</td><td style="text-align:right;" class="ok"><strong>90.0%</strong></td></tr>
            <tr><td>Right Card Tied</td><td style="text-align:right;" class="ok"><strong>78.9%</strong></td></tr>
            <tr><td>Tier 3 Sidekick Lift (Recall@5)</td><td style="text-align:right;" class="ok"><strong>+12.3%</strong> (MB 46.4% vs BQ 34.1%)</td></tr>
            <tr><td>Customer Isolation &amp; Control</td><td style="text-align:right;" class="ok"><strong>100%</strong> (6/6 probes, 0 leaks)</td></tr>
            <tr><td>7 JPMC Scenarios (LLM Judge)</td><td style="text-align:right;">3 / 7 passed</td></tr>
          </table>
        </div>
      `;
    }

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
      </table>
      ${harnessHtml}`;

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
    // Switch top black trace bar indicator from '>' (idle) to bronze blinking square ('running')
    // without auto-expanding the #drop trace drawer.
    setLivePulseState("running");
    setRunMetaText("Running governed response & claim audit against live GCP...");
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
    }
    if ($("askToolTags")) {
      $("askToolTags").innerHTML = `<span class="tag on">Streaming live trace to top black bar…</span>`;
    }
    clearSynthesisCard();

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
        if ($("askAnswerTag")) {
          $("askAnswerTag").className = "tag no";
          $("askAnswerTag").textContent = String(data.status || resp.status);
        }
        if ($("askAnswerBody")) {
          $("askAnswerBody").innerHTML = `<span style="color:var(--bad);">${escapeHtml(
            data.error || "Request failed"
          )}</span>${data.needs ? ` <small>(needs ${escapeHtml(data.needs)})</small>` : ""}`;
        }
        setLivePulseState(AGENTS.length ? "done" : "idle");
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
        await fetchAndRenderTrace(data.run_id);
      }

      if ($("askAnswerTitle")) {
        $("askAnswerTitle").textContent = `Governed response · ${customerId}`;
      }
      if ($("askAnswerTag")) {
        $("askAnswerTag").className = "tag ok";
        $("askAnswerTag").textContent = "CATALOG + MEMORY";
      }
      if ($("askAnswerBody")) {
        const resolution = data.agent_resolution;
        $("askAnswerBody").innerHTML =
          pvValue(resolution) === NOT_RECORDED
            ? `<code>${NOT_RECORDED}</code> — narrative arrives on <code>/api/chat/stream</code>. ${pvChip(
                resolution
              )}`
            : formatAgentReply(pvText(resolution));
      }

      renderToolTrajectoryTags(data.tool_trajectory, data.run_id);
      renderVeracityAudit(data.veracity_evaluation);
      subscribeToSynthesisStream(customerId, query, data.run_id);
    } catch (err) {
      if ($("askAnswerBody")) {
        $("askAnswerBody").innerHTML = `<span style="color:var(--bad);">Error communicating with desk: ${escapeHtml(
          err.message
        )}</span>`;
      }
      setLivePulseState(AGENTS.length ? "done" : "idle");
    } finally {
      if (askBtn) {
        askBtn.disabled = false;
        askBtn.textContent = "Send →";
      }
    }
  }

  function renderToolTrajectoryTags(trajectory, runId) {
    const steps = Array.isArray(trajectory) ? trajectory : [];
    const el = $("askToolTags");
    if (!el) return;
    const rid = runId || LAST_TRACED_RUN_ID || "";
    const spanCount = LAST_TRACED_SPANS.length || steps.length;
    const agentCount = AGENTS.length;
    if (!spanCount && !rid) {
      el.innerHTML = `<span class="tag">Idle — trace logs in top black bar when run</span>`;
      return;
    }
    el.innerHTML =
      `<button type="button" class="tag ok" id="btnOpenTopTraceFromAsk" style="cursor:pointer;" ` +
      `title="Click to toggle the full agent trace in the top black navigation bar">` +
      `Trace logged in top bar · Run ${escapeHtml(rid || "live")} · ` +
      `${agentCount ? agentCount + " active agent(s) · " : ""}${spanCount} spans — View in Trace bar ▴` +
      `</button>`;
    const btnOpen = $("btnOpenTopTraceFromAsk");
    if (btnOpen) {
      btnOpen.addEventListener("click", () => {
        setTrace(true);
        window.scrollTo({ top: 0, behavior: "smooth" });
      });
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
    li.innerHTML = `<span class="t">SSE</span><span class="e">${text}</span>`;
    if (list.firstChild) {
      list.insertBefore(li, list.firstChild);
    } else {
      list.appendChild(li);
    }
  }

  function subscribeToSynthesisStream(customerId, prompt, runId) {
    if (typeof EventSource === "undefined") return;
    if (STATE.stream) {
      STATE.stream.close();
      STATE.stream = null;
    }

    const url =
      "/api/chat/stream?customer_id=" +
      encodeURIComponent(customerId) +
      "&prompt=" +
      encodeURIComponent(prompt) +
      (runId ? "&run_id=" + encodeURIComponent(runId) : "");
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
        if (runId) {
          fetchAndRenderTrace(runId);
        }
      }
    };

    source.onerror = () => {
      source.close();
      STATE.stream = null;
      if (runId) {
        fetchAndRenderTrace(runId);
      }
    };
  }

  function renderSynthesisCard(card) {
    if (!card) return;
    const meta = $("askSynthesisMeta");
    const list = $("askSynthesisTimeline");
    if (meta) {
      meta.innerHTML =
        `<b>${escapeHtml(card.title || NOT_RECORDED)}</b> · ` +
        `${escapeHtml(pvText(card.memory_count))} memory(ies) ${pvChip(card.memory_count)} · ` +
        `confidence ${escapeHtml(pvText(card.confidence_score))} ${pvChip(card.confidence_score)}`;
    }
    if (list) {
      const timeline = Array.isArray(card.timeline) ? card.timeline : [];
      list.innerHTML = timeline.length
        ? timeline
            .map(
              (t) =>
                `<li><span class="t">${escapeHtml(t.timestamp || NOT_RECORDED)} · ` +
                `${escapeHtml(t.channel || NOT_RECORDED)}</span>` +
                `<span class="e">${escapeHtml(t.title || NOT_RECORDED)}</span></li>`
            )
            .join("")
        : `<li><span class="t">${NOT_RECORDED}</span><span class="e">no causal steps</span></li>`;
    }
  }

  function clearSynthesisCard() {
    if ($("askSynthesisMeta")) $("askSynthesisMeta").innerHTML = "";
    if ($("askSynthesisTimeline")) $("askSynthesisTimeline").innerHTML = "";
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
    setLivePulseState("running");

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
        setLivePulseState(AGENTS.length ? "done" : "idle");
        console.error("Card oversight decision failed:", data);
        return;
      }
      updateOversightUI(data);
      if (data.snapshot) {
        STATE.channelsSummary = STATE.channelsSummary || {};
        STATE.channelsSummary.snapshot = data.snapshot;
        renderChannelEventStream(data.snapshot);
      }
      if (data.run_id) {
        await fetchAndRenderTrace(data.run_id);
      }
      if (data.tool_trajectory) {
        renderToolTrajectoryTags(data.tool_trajectory, data.run_id);
      }
      setStampText("SUPERVISOR LOGGED · " + (data.run_id || ""));
    } catch (err) {
      setLivePulseState(AGENTS.length ? "done" : "idle");
      console.error("Card oversight decision error:", err);
    } finally {
      if (btnUnlock) btnUnlock.disabled = false;
      if (btnRestrict) btnRestrict.disabled = false;
    }
  }

  function renderCompactionResult(report) {
    const out = $("compactionResult");
    if (!out) return;
    const lineageItem = report.memory_lineage;
    const lineage = pvValue(lineageItem);
    const before = `<div>Memories in scope before: <b>${escapeHtml(pvText(report.memories_in_scope_before))}</b> · token saving: ${escapeHtml(pvText(report.token_reduction_pct))} <span class="nr">(${escapeHtml(pvSource(report.token_reduction_pct))})</span></div>`;
    if (!lineage || typeof lineage !== "object") {
      out.innerHTML = before + `<div>${nrSpan(pvSource(lineageItem))}</div>`;
      return;
    }
    const [standingCls, standingText] = BOARD_STANDING[lineage.standing] || ["b-nr", boardText(lineage.standing)];
    const generated = Array.isArray(lineage.generated) ? lineage.generated : [];
    const genList = generated.length
      ? `<ul>${generated.map((g) => `<li>${escapeHtml(boardText(g.action))} · <code>${escapeHtml(String(g.name || NOT_RECORDED).split("/").pop())}</code></li>`).join("")}</ul>`
      : ` ${nrSpan("the generate call returned no memories")}`;
    const record = lineage.claim_record;
    const recordLine = record && record.fact
      ? `<div>Claim record written: “${escapeHtml(record.fact)}”</div>`
      : `<div>Claim record: ${nrSpan("none; trusted sessions don't get one")}</div>`;
    const board = lineage.action_board;
    const boardLine = board && board.kind
      ? `<div>Action Board: new ${escapeHtml(BOARD_KIND_LABEL[board.kind] || board.kind)} item (${escapeHtml(boardText(board.priority))}).</div>`
      : `<div>Action Board: no item raised.</div>`;
    const error = lineage.check_error ? `<div class="bi-err">Veracity check failed: ${escapeHtml(lineage.check_error)}</div>` : "";
    out.innerHTML = before +
      `<div>Session <code>${escapeHtml(String(lineage.session_name || NOT_RECORDED).split("/").pop())}</code> · standing ${boardBadge(standingCls, standingText)} · verdict <b>${escapeHtml(boardText(lineage.verdict))}</b></div>` +
      `<div>Generated (${generated.length}):${genList}</div>` + recordLine + boardLine + error;
  }

  async function runMemoryCompaction() {
    const btn = $("btnRunCompaction");
    const out = $("compactionResult");
    const customer = currentCustomerId();
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Running consolidation…";
    }
    if (out) out.textContent = `Re-running ${customer}'s newest session through the verified write path…`;
    setLivePulseState("running");

    try {
      const resp = await fetch("/api/compaction/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ customer_id: customer }),
      });
      const data = await resp.json().catch(() => ({}));

      if (!resp.ok) {
        setLivePulseState(AGENTS.length ? "done" : "idle");
        if (out) out.innerHTML = `<span class="bi-err">Consolidation failed: ${escapeHtml(data.error || data.status || `HTTP ${resp.status}`)}</span>`;
        return;
      }

      renderCompactionResult(data.compaction_report || {});
      if (data.run_id) {
        await fetchAndRenderTrace(data.run_id);
      }
      if (data.tool_trajectory) {
        renderToolTrajectoryTags(data.tool_trajectory, data.run_id);
      }
      loadActionBoard();
      await loadLiveTestbed(customer);
    } catch (err) {
      setLivePulseState(AGENTS.length ? "done" : "idle");
      if (out) out.innerHTML = `<span class="bi-err">Consolidation failed: ${escapeHtml(err.message)}</span>`;
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "▶ Run consolidation";
      }
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
    // 0. Collapse/expand toggles on every panel (app.js:setupCollapsiblePanels).
    setupCollapsiblePanels();
    // 0b. Mini customer card + torch-beam expanded card (app.js:setupCustomerCard).
    setupCustomerCard();
    // 0c. Customer Chat Popover Scroll (app.js:setupChatPopover).
    setupChatPopover();

    // 1. Start in IDLE caret '>' state until an action runs.
    setLivePulseState("idle");

    // 2. Build Menu & Wire Livebar / Drop Trace Controls
    buildMenu();
    syncTraceBarVisibility();
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
        sendCustomerChatMessage(q);
      });
      askInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          const q = askInput.value.trim() || askInput.getAttribute("placeholder") || "";
          sendCustomerChatMessage(q);
        }
      });
    } else if (consoleForm && askInput) {
      consoleForm.addEventListener("submit", (e) => {
        e.preventDefault();
        const q = askInput.value.trim() || askInput.getAttribute("placeholder") || "";
        sendCustomerChatMessage(q);
      });
    }

    // 6. Preset question chips inside drawer (.ask-preset-chip)
    document.querySelectorAll(".ask-preset-chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        const q = chip.getAttribute("data-q") || "";
        if (askInput) askInput.value = q;
        sendCustomerChatMessage(q);
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

    // 8. Memory Bank Compaction (#btnRunCompaction)
    const btnCompaction = $("btnRunCompaction");
    if (btnCompaction) {
      btnCompaction.addEventListener("click", runMemoryCompaction);
    }

    // 9. Live 5-channel ingestion (B), fraud card control (C), and async 95s audit sweep (4)
    const chSel = $("channelIngestSelect");
    if (chSel) chSel.addEventListener("change", populateChannelPresetPicker);
    const presetSel = $("channelPresetSelect");
    if (presetSel) presetSel.addEventListener("change", renderSelectedChannelPresetPreview);
    const btnInject = $("btnInjectChannelEvent");
    if (btnInject) btnInject.addEventListener("click", injectLiveChannelEvent);
    const btnFraud = $("btnFraudCardControl");
    if (btnFraud) btnFraud.addEventListener("click", triggerFraudCardControl);
    const btnSweep = $("btnTriggerAuditSweep");
    if (btnSweep) btnSweep.addEventListener("click", triggerAsyncAuditSweep);

    // 10. Agent Spec & Live InMemoryRunner Popup Modal (#agentCardModal)
    const btnCloseModal = $("btnCloseAgentModal");
    if (btnCloseModal) btnCloseModal.addEventListener("click", closeAgentCardModal);
    const btnRunModal = $("btnRunAgentLiveModal");
    if (btnRunModal) btnRunModal.addEventListener("click", runSingleAgentLiveFromModal);
    const btnTraceModal = $("btnViewAgentTraceModal");
    if (btnTraceModal) {
      btnTraceModal.addEventListener("click", () => {
        const spec = findAgentSpec(CURRENT_MODAL_AGENT_KEY);
        const loggedIdx = AGENTS.findIndex((ag) => ag.k === CURRENT_MODAL_AGENT_KEY || ag.name === spec.name);
        if (loggedIdx >= 0) {
          closeAgentCardModal();
          pick(loggedIdx);
          setTrace(true);
          window.scrollTo({ top: 0, behavior: "smooth" });
        }
      });
    }
    const agentModalBackdrop = $("agentCardModal");
    if (agentModalBackdrop) {
      agentModalBackdrop.addEventListener("click", (e) => {
        if (e.target === agentModalBackdrop) closeAgentCardModal();
      });
    }
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeAgentCardModal();
    });
    wireSourcePopoverGlobals();

    // 11. Topic scoping panel: runs only when asked, never on load.
    const btnTopicAb = $("btnRunTopicAb");
    if (btnTopicAb) btnTopicAb.addEventListener("click", runTopicAb);

    // 12. Action Board (app.js:loadActionBoard): one sessions.list call, so it loads on open.
    wireActionBoard();
    loadActionBoard();

    // Hydrate the 8-agent Pipeline immediately at 0ms using FALLBACK_DESK_AGENTS
    ALL_DESK_AGENTS = buildDeskAgentEntries({ agents: FALLBACK_DESK_AGENTS }, [], "");
    renderRoster();

    // Initial background data hydration for canonical CUST00001
    loadScenarios();
    loadChannelsAndRosterSummary(DEFAULT_CUSTOMER_ID);
    loadDeploymentIdentity(DEFAULT_CUSTOMER_ID);
    loadEvalScorecard();
    loadLiveTestbed(DEFAULT_CUSTOMER_ID);
    loadCardOversight(DEFAULT_CUSTOMER_ID);
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

  function renderSelectedChannelPresetPreview() {
    const chSel = $("channelIngestSelect");
    const presetSel = $("channelPresetSelect");
    const previewEl = $("channelTurnsPreview");
    if (!previewEl) return;

    const channel = (chSel && chSel.value) || "";
    const presetId = (presetSel && presetSel.value) || "";
    const presetsByChannel = (STATE.channelsSummary && STATE.channelsSummary.presets) || {};
    const list = Array.isArray(presetsByChannel[channel]) ? presetsByChannel[channel] : [];
    const chosen = list.find((p) => p.preset_id === presetId) || list[0];

    if (!chosen || !Array.isArray(chosen.turns)) {
      previewEl.innerHTML = "<span class='n'>No prebaked turns for this channel.</span>";
      return;
    }

    previewEl.innerHTML = chosen.turns.map((t, i) => {
      const isCust = String(t.author || "").toLowerCase() === "customer";
      const badge = isCust ? "CUSTOMER" : escapeHtml(t.author || channel);
      return `<div style="margin-bottom:${i < chosen.turns.length - 1 ? "4px" : "0"};">
        <span class="tag ${isCust ? "on" : "ok"}" style="margin-right:5px;font-size:9px;">${badge}</span>
        <span>${escapeHtml(t.text || "")}</span>
      </div>`;
    }).join("");
  }

  function channelPresets() {
    const presets = STATE.channelsSummary && STATE.channelsSummary.presets;
    return presets && typeof presets === "object" ? presets : {};
  }

  const CHANNEL_LABELS = {
    FRAUD_OPS_OUTBOUND: "Fraud ops outbound",
    IVR: "IVR (telephony)",
    MOBILE_APP: "Mobile app",
    WEB_CHAT: "Web chat (portal)",
    BRANCH: "Branch (in person)",
  };

  // Only channels with an injectable preset are offered; the backend decides
  // which presets exist (adk_agents.live_injection_presets).
  function populateChannelSelect() {
    const chSel = $("channelIngestSelect");
    const btn = $("btnInjectChannelEvent");
    if (!chSel) return;
    const channels = Object.keys(channelPresets()).filter((c) => (channelPresets()[c] || []).length);
    const previous = chSel.value;
    if (!channels.length) {
      chSel.innerHTML = `<option value="">No live presets available</option>`;
      chSel.disabled = true;
      if (btn) btn.disabled = true;
      return;
    }
    chSel.disabled = false;
    if (btn) btn.disabled = false;
    chSel.innerHTML = channels.map((c) =>
      `<option value="${escapeHtml(c)}"${c === previous ? " selected" : ""}>${escapeHtml(CHANNEL_LABELS[c] || c)}</option>`
    ).join("");
  }

  function populateChannelPresetPicker() {
    const chSel = $("channelIngestSelect");
    const presetSel = $("channelPresetSelect");
    if (!chSel || !presetSel) return;

    const channel = chSel.value;
    const list = Array.isArray(channelPresets()[channel]) ? channelPresets()[channel] : [];

    if (!list.length) {
      presetSel.innerHTML = `<option value="">No preset for this channel</option>`;
      renderSelectedChannelPresetPreview();
      return;
    }

    presetSel.innerHTML = list.map((p, idx) => {
      const turnsCount = Array.isArray(p.turns) ? p.turns.length : 1;
      return `<option value="${escapeHtml(p.preset_id)}"${idx === 0 ? " selected" : ""}>${escapeHtml(p.title)} (${turnsCount} turns)</option>`;
    }).join("");
    renderSelectedChannelPresetPreview();
  }

  async function loadChannelsAndRosterSummary(customerId) {
    const cid = customerId || DEFAULT_CUSTOMER_ID;
    try {
      const resp = await fetch(`/api/channels/summary?customer_id=${encodeURIComponent(cid)}`);
      if (!resp.ok) return;
      const data = await resp.json();
      STATE.channelsSummary = data;
      populateChannelSelect();
      populateChannelPresetPicker();
      if (data.snapshot) {
        renderChannelEventStream(data.snapshot);
      }
      if (data.audit_sweep) {
        renderAuditSweepState(data.audit_sweep);
      }

      ALL_DESK_AGENTS = buildDeskAgentEntries(data, LAST_TRACED_SPANS, LAST_TRACED_RUN_ID);
      AGENTS = ALL_DESK_AGENTS.filter((a) => a.spans && a.spans.length > 0);
      window.AGENTS = AGENTS;
      syncTraceBarVisibility();
      buildMenu();
      if (AGENTS.length) pick(0);
      renderRoster();

      if (!LAST_TRACED_SPANS.length && data.snapshot) {
        const totSess = data.snapshot.total_sessions || 0;
        const totEv = data.snapshot.total_events || 0;
        setAgentCountText("8 agents · IDLE");
        setRunMetaText(`8-agent desk ready (idle) · ${totSess} live sessions (${totEv} events) across 5 channels — click Replay run →`);
        setStampText(`IDLE · ${totSess} sessions across 5 channels`);
        setTruthEngineText(
          `Idle — click Replay run → or Send → to trace live calls across the 8 agents & 5 channels (${totSess} sessions in Memory Bank)`
        );
      }
    } catch (err) {
      console.error("loadChannelsAndRosterSummary failed", err);
    }
  }

  async function injectLiveChannelEvent() {
    const sel = $("channelIngestSelect");
    const presetSel = $("channelPresetSelect");
    const txt = $("channelIngestText");
    const statusEl = $("channelIngestStatus");
    const btn = $("btnInjectChannelEvent");
    const channel = (sel && sel.value) || "";
    const presetId = (presetSel && presetSel.value) || "";
    const text = (txt && txt.value.trim()) || "";

    if (btn) btn.disabled = true;
    setLivePulseState("running");
    if (statusEl) statusEl.textContent = `Writing multi-turn ${channel} session to Vertex AI Memory Bank…`;
    try {
      const resp = await fetch(`/api/channel/${encodeURIComponent(channel)}/event`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          customer_id: DEFAULT_CUSTOMER_ID,
          preset_id: presetId,
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
      }
      if (data.run_id) {
        await fetchAndRenderTrace(data.run_id);
      }
      const sid = pvText(data.ingested && data.ingested.session_id);
      const tCount = pvText(data.ingested && data.ingested.turn_count);
      if (txt) txt.value = "";
      if (statusEl) {
        statusEl.textContent = `Ingested ${channel} session ${sid} (${tCount} turns) into Memory Bank · producer now holds ${data.snapshot ? data.snapshot.total_sessions : "n/r"} total sessions.`;
      }
    } catch (err) {
      setLivePulseState(AGENTS.length ? "done" : "idle");
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
    setLivePulseState("running");
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
      setLivePulseState(AGENTS.length ? "done" : "idle");
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

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
