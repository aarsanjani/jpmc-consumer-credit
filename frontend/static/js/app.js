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
  const AGENTS = [
    {
      k: "writer",
      i: "06",
      name: "memory writer",
      ms: "0.9s",
      state: "done",
      refusals: 0,
      tools: 1,
      deny: 0,
      sql: 0,
      started: "+6.200s",
      ended: "+7.100s",
      pending: "Generation is asynchronous — memories appear after the run closes.",
      spans: [
        ["+6.200s", "tool", "memorybank.generate_memories(scope=CUST00001)", "640ms", ""],
        ["+6.840s", "think", "session held 2 contradictions and 3 must-not-store items", "260ms", "nest"],
        ["+7.100s", "out", "3 durable memories written · MEM00015 & MEM00017 supersede stale facts", "—", ""],
      ],
      foot: [
        ["tokens", "980 in / 260 out"],
        ["tools", "1"],
        ["refusals", "0"],
        ["ad-hoc sql", "0"],
      ],
    },
    {
      k: "actions",
      i: "05",
      name: "resolution & actions",
      ms: "1.6s",
      state: "done",
      refusals: 1,
      tools: 3,
      deny: 1,
      sql: 0,
      started: "+4.600s",
      ended: "+6.200s",
      spans: [
        ["+4.600s", "tool", "cards.freeze(CARD00002, CARD00004)", "180ms", ""],
        ["+4.780s", "tool", "wallet.revoke_tokens(device=DEV00001) · 3 tokens", "210ms", "nest"],
        ["+4.990s", "tool", "cards.issue_virtual(account=ACCT000003)", "150ms", "nest"],
        ["+5.140s", "deny", "did not close dispute — standing instruction: notify first", "4ms", ""],
        ["+6.200s", "out", "2 frozen · 3 revoked · VCN issued · courier rerouted to NYC home", "—", ""],
      ],
      foot: [
        ["tokens", "1,420 in / 380 out"],
        ["tools", "3"],
        ["refusals", "1"],
        ["ad-hoc sql", "0"],
      ],
    },
    {
      k: "compliance",
      i: "04",
      name: "policy & compliance",
      ms: "1.9s",
      state: "done",
      refusals: 1,
      tools: 3,
      deny: 1,
      sql: 0,
      started: "+2.700s",
      ended: "+4.600s",
      spans: [
        ["+2.700s", "tool", 'catalog.lookup_term("provisional credit") · owner: Disputes Policy', "142ms", ""],
        ["+2.842s", "think", "regulation_path = REG_Z → provisional credit does not apply", "610ms", "nest"],
        ["+3.452s", "tool", "bq.query(golden_08_filing_window) · {dispute_id: DSP00034}", "310ms", ""],
        ["+3.762s", "deny", "refused authorizations_deprecated_v1 — lifecycle DEPRECATED", "8ms", ""],
        ["+3.770s", "tool", 'catalog.lookup_term("Regulation Z") · 12 CFR 1026.13', "98ms", "nest"],
        ["+4.600s", "out", "Reg E → 23 Sep (business days) · Reg Z → none, withhold payment", "—", ""],
      ],
      foot: [
        ["tokens", "3,140 in / 890 out"],
        ["tools", "3"],
        ["refusals", "1"],
        ["ad-hoc sql", "0"],
      ],
    },
    {
      k: "triage",
      i: "03",
      name: "transaction & evidence",
      ms: "1.4s",
      state: "done",
      refusals: 1,
      tools: 2,
      deny: 1,
      sql: 0,
      started: "+1.300s",
      ended: "+2.700s",
      spans: [
        ["+1.300s", "tool", "bq.query(golden_03_suspicious_activity) · {card_id: CARD00002}", "640ms", ""],
        ["+1.940s", "think", "37 flagged · 5 LOST_STOLEN inside the travel window", "320ms", "nest"],
        ["+2.260s", "tool", "bq.query(golden_05_geo_impossible) · {customer_id: CUST00001}", "410ms", ""],
        ["+2.670s", "deny", "skipped cards.pan_full — catalog sensitivity RESTRICTED", "3ms", ""],
        ["+2.700s", "out", "37 of 17,954 · 2 cards implicated · Lisbon 05–08 Sep", "—", ""],
      ],
      foot: [
        ["tokens", "2,610 in / 740 out"],
        ["tools", "2"],
        ["refusals", "1"],
        ["ad-hoc sql", "0"],
      ],
    },
    {
      k: "context",
      i: "02",
      name: "customer & context",
      ms: "1.2s",
      state: "done",
      refusals: 1,
      tools: 2,
      deny: 1,
      sql: 0,
      started: "+0.400s",
      ended: "+1.600s",
      spans: [
        ["+0.400s", "tool", "memory.retrieve_memories(scope=CUST00001, top_k=5)", "240ms", ""],
        ["+0.640s", "think", "2 candidate travel facts, one superseded by a later session", "180ms", "nest"],
        ["+0.820s", "deny", "discarded MEM00003 — invalidated 08 Sep by MEM00015", "4ms", ""],
        ["+0.824s", "tool", "memory.retrieve_memories(topic=EXPLICIT_INSTRUCTIONS)", "88ms", "nest"],
        ["+1.600s", "out", "9 sessions · 5 channels · 14 facts · 1 standing instruction", "—", ""],
      ],
      foot: [
        ["tokens", "1,890 in / 420 out"],
        ["tools", "2"],
        ["refusals", "1"],
        ["ad-hoc sql", "0"],
      ],
    },
    {
      k: "orchestrator",
      i: "01",
      name: "orchestrator",
      ms: "0.4s",
      state: "done",
      refusals: 0,
      tools: 1,
      deny: 0,
      sql: 0,
      started: "+0.000s",
      ended: "+0.400s",
      spans: [
        ["+0.000s", "tool", "session.open(channel=IVR, customer=CUST00001)", "60ms", ""],
        ["+0.060s", "think", "lost-card-abroad intent · two cards on file · route to context", "300ms", "nest"],
        ["+0.400s", "out", "plan: context → triage → compliance → actions → writer", "—", ""],
      ],
      foot: [
        ["tokens", "740 in / 210 out"],
        ["tools", "1"],
        ["refusals", "0"],
        ["ad-hoc sql", "0"],
      ],
    },
  ];

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
        '    <div class="menu-head"><span>Run 0412 · 6 agents</span><span>Newest first</span></div>' +
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
        '<span class="stamp" id="liveLogStamp">Run 0412 · 7.1s</span>';
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
  // 2. 6-AGENT PIPELINE STEPS FOR LIVE REPLAY ANIMATION (#btnReplayRun)
  // =========================================================================
  const PIPELINE_STEPS = [
    {
      id: "agentItem1",
      name: "Orchestrator",
      duration: "0.4s",
      cumTime: "0.4s",
      desc: "Owns the conversation, routes the case",
    },
    {
      id: "agentItem2",
      name: "Customer & Context",
      duration: "1.2s",
      cumTime: "1.6s",
      desc: "Memory Bank · 9 sessions, 5 channels",
    },
    {
      id: "agentItem3",
      name: "Transaction & Evidence",
      duration: "1.4s",
      cumTime: "3.0s",
      desc: "Golden query 03 · 37 flagged of 17,954",
    },
    {
      id: "agentItem4",
      name: "Policy & Compliance",
      duration: "1.9s",
      cumTime: "4.9s",
      desc: "Reading glossary · resolving both paths",
    },
    {
      id: "agentItem5",
      name: "Resolution & Actions",
      duration: "1.6s",
      cumTime: "6.5s",
      desc: "Freeze · revoke · virtual card · courier",
    },
    {
      id: "agentItem6",
      name: "Memory Writer",
      duration: "0.9s",
      cumTime: "7.1s",
      desc: "Consolidates the session after close",
    },
  ];

  function clearReplayTimers() {
    STATE.replayTimers.forEach((t) => clearTimeout(t));
    STATE.replayTimers = [];
  }

  function setAgentItemState(stepIdx, mode) {
    const step = PIPELINE_STEPS[stepIdx];
    if (!step) return;
    const itemEl = $(step.id);
    if (!itemEl) return;

    itemEl.classList.remove("done", "active");
    const tagEl = itemEl.querySelector(".tag");
    const rdEl = itemEl.querySelector(".rd");
    if (rdEl) rdEl.textContent = step.desc;

    if (mode === "done") {
      itemEl.classList.add("done");
      if (tagEl) {
        tagEl.className = "tag ok";
        tagEl.textContent = step.duration;
      }
    } else if (mode === "active") {
      itemEl.classList.add("active");
      if (tagEl) {
        tagEl.className = "tag on";
        tagEl.textContent = "running";
      }
    } else {
      if (tagEl) {
        tagEl.className = "tag";
        tagEl.textContent = "queued";
      }
    }
  }

  function runReplayAnimation() {
    if (STATE.replayRunning) return;
    STATE.replayRunning = true;
    clearReplayTimers();

    const btn = $("btnReplayRun");
    const livePulse = getLivePulseEl();

    if (btn) {
      btn.disabled = true;
      btn.textContent = "Running 6 agents...";
    }
    if (livePulse) {
      livePulse.classList.remove("done");
    }
    setRunMetaText("Run 0412 · 22 Sep 2026 · replaying...");

    for (let i = 0; i < PIPELINE_STEPS.length; i++) {
      setAgentItemState(i, "queued");
      AGENTS[5 - i].state = "queue";
    }
    setAgentCountText("00 / 6 done");
    buildMenu();

    const stepDelayMs = 480;

    PIPELINE_STEPS.forEach((step, stepIdx) => {
      const timer = setTimeout(() => {
        for (let r = 0; r < PIPELINE_STEPS.length; r++) {
          if (r < stepIdx) {
            setAgentItemState(r, "done");
            AGENTS[5 - r].state = "done";
          } else if (r === stepIdx) {
            setAgentItemState(r, "active");
            AGENTS[5 - r].state = "run";
          } else {
            setAgentItemState(r, "queued");
            AGENTS[5 - r].state = "queue";
          }
        }
        buildMenu();
        pick(5 - stepIdx);
        setAgentCountText(`0${stepIdx} / 6 done`);
        setStampText(`Run 0412 · ${step.cumTime}`);
      }, stepIdx * stepDelayMs);
      STATE.replayTimers.push(timer);
    });

    const finishTimer = setTimeout(() => {
      for (let i = 0; i < 6; i++) {
        setAgentItemState(i, "done");
        AGENTS[i].state = "done";
      }
      setAgentCountText("06 / 6 done");
      if (livePulse) {
        livePulse.classList.add("done");
      }
      buildMenu();
      pick(0);
      setRunMetaText("Run 0412 · 22 Sep 2026 · simulation");
      setStampText("Run 0412 · 7.1s");

      if (btn) {
        btn.disabled = false;
        btn.textContent = "Replay run →";
      }
      STATE.replayRunning = false;
    }, PIPELINE_STEPS.length * stepDelayMs + 260);

    STATE.replayTimers.push(finishTimer);
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
              return `<div style="margin-bottom:4px;"><code>${id}</code>: ${fact} <small>${when}</small></div>`;
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
    setStampText("Run 0412 · WORKING");

    if ($("askAnswerTitle")) {
      $("askAnswerTitle").textContent = `Working case · ${customerId}`;
    }
    if ($("askAnswerTag")) {
      $("askAnswerTag").className = "tag on";
      $("askAnswerTag").textContent = "RUNNING 6 AGENTS";
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
        setStampText("Run 0412 · stream complete");
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
      setStampText("Run 0412 · SUPERVISOR LOGGED");
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

    // 1. Initialize default completed state across all 6 agents
    for (let i = 0; i < 6; i++) {
      setAgentItemState(i, "done");
      AGENTS[i].state = "done";
    }
    setAgentCountText("06 / 6 done");
    setRunMetaText("Run 0412 · 22 Sep 2026 · simulation");
    setStampText("Run 0412 · 7.1s");
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

    // Wire #agentItem1..#agentItem6 in the Agent Roster to select that agent in .livebar and open #drop
    for (let rosterIdx = 0; rosterIdx < 6; rosterIdx++) {
      const itemEl = $(`agentItem${rosterIdx + 1}`);
      if (itemEl) {
        itemEl.style.cursor = "pointer";
        itemEl.addEventListener("click", () => {
          pick(5 - rosterIdx);
          setTrace(true);
        });
      }
    }

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

        // 8. Memory Bank Compaction (#btnRunCompaction)
    const btnCompaction = $("btnRunCompaction");
    if (btnCompaction) {
      btnCompaction.addEventListener("click", runMemoryCompaction);
    }

    // Initial background data hydration for canonical CUST00001
    loadDeploymentIdentity(DEFAULT_CUSTOMER_ID);
    loadEvalScorecard();
    loadLiveTestbed(DEFAULT_CUSTOMER_ID);
    loadCardOversight(DEFAULT_CUSTOMER_ID);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
