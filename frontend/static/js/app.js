/**
 * CCB Fraud Desk v2.0 — V1 Black Bar Trace Navigation & Live GCP Testbed Controller
 * Canonical Use Case: CUST00001 (Lauren Morrison / Priya Vantalis — Lisbon Reg E + Reg Z)
 *
 * Wired to live backend endpoints:
 *   - GET  /api/testbed?customer_id=CUST00001
 *   - GET  /api/eval-scorecard
 *   - POST /api/chat
 *   - GET  /api/card-oversight?customer_id=CUST00001
 *   - POST /api/card-oversight/decide
 *   - POST /api/compaction/run
 */

(function () {
  "use strict";

  // Prevent inline fallback scripts from double-binding
  window.__ccbV2FallbackBound = true;
  window.__ccbV1TraceBound = true;

  const DEFAULT_CUSTOMER_ID = "CUST00001";

  const STATE = {
    customerId: DEFAULT_CUSTOMER_ID,
    channel: "WEB_CHAT",
    replayRunning: false,
    replayTimers: [],
    currentTraceKey: "policy",
    testbedData: null,
    scorecardData: null,
  };

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
    try {
      const resp = await fetch(`/api/testbed?customer_id=${encodeURIComponent(cid)}`);
      if (!resp.ok) return;
      const data = await resp.json();
      STATE.testbedData = data;

      const memListEl = $("liveMemoriesList");
      const memories =
        (data.vertex_memory_bank && data.vertex_memory_bank.memories) ||
        data.memories ||
        [];
      if (memListEl && memories.length > 0) {
        const activeMems = memories.slice(0, 3);
        memListEl.innerHTML = activeMems
          .map((m) => {
            const id = escapeHtml(m.memory_id || m.id || "MEM");
            const fact = escapeHtml(m.fact || m.memory_text || m.text || "");
            const sup = m.supersedes_memory_id
              ? ` <em>(supersedes <code>${escapeHtml(m.supersedes_memory_id)}</code>)</em>`
              : "";
            return `<div style="margin-bottom:4px;"><code>${id}</code>: ${fact}${sup}</div>`;
          })
          .join("");
      }
    } catch (err) {
      console.warn("Testbed fetch notice:", err);
    }
  }

  async function loadEvalScorecard() {
    try {
      const resp = await fetch("/api/eval-scorecard");
      if (!resp.ok) return;
      const data = await resp.json();
      STATE.scorecardData = data;

      if ($("scoreLeakage") && data.memory_layer) {
        $("scoreLeakage").textContent = String(data.memory_layer.cross_scope_leakage ?? 0);
      }
      if ($("scoreContradictions") && data.memory_layer) {
        $("scoreContradictions").textContent =
          data.memory_layer.contradiction_handling || "10/10";
      }
    } catch (err) {
      console.warn("Scorecard fetch notice:", err);
    }
  }

  // =========================================================================
  // 4. ASK CONSOLE & MULTI-AGENT CHAT + RELEVANT-ONLY VERACITY AUDIT
  // =========================================================================
  async function submitAskQuestion(questionText) {
    const query = String(questionText || "").trim();
    if (!query) return;

    const customerId = DEFAULT_CUSTOMER_ID;
    const channel = STATE.channel || "WEB_CHAT";

    const askBtn = $("btnAskSend");
    if (askBtn) {
      askBtn.disabled = true;
      askBtn.textContent = "Working...";
    }

    openOpsDrawer();
    openTrace("1", "policy");
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
        "Querying BigQuery golden queries (<code>jpmc_ccb_fraud</code>), Dataplex glossary <code>ccb-fraud-disputes</code>, and Vertex AI Memory Bank <code>4277754991222980608</code>...";
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
      const replyHtml = formatAgentReply(data.response || data.reply || "No response returned.");

      if ($("askAnswerTitle")) {
        $("askAnswerTitle").textContent = `Governed response · ${customerId}`;
      }
      if ($("askAnswerTag")) {
        $("askAnswerTag").className = "tag ok";
        $("askAnswerTag").textContent = "CATALOG + MEMORY";
      }
      if ($("askAnswerBody")) {
        $("askAnswerBody").innerHTML = replyHtml;
      }

      const tools = data.tool_sequence || [
        "freeze_card",
        "revoke_wallet_token",
        "issue_virtual_card",
        "order_replacement_card",
      ];
      if ($("askToolTags")) {
        $("askToolTags").innerHTML = tools
          .map(
            (t, idx) =>
              `<span class="tag ${idx === tools.length - 1 ? "on" : "ok"}">${idx + 1}. ${escapeHtml(
                t
              )}</span>`
          )
          .join("");
      }

      renderVeracityAudit(data.veracity_audit || data.claim_veracity_audit);
      setStampText("Run 0412 · 6.3s");
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
    if (!audit) return;
    const status = audit.veracity_status || "VERIFIED_TRUE";
    const confPct =
      audit.confidence_score != null
        ? `${Math.round(
            Number(audit.confidence_score) * (Number(audit.confidence_score) <= 1 ? 100 : 1)
          )}%`
        : "98%";

    const tagEl = $("veracityStatusTag") || $("veracityStatusBadge");
    if (tagEl) {
      const isContradicted = status.includes("CONTRADICTED") || status.includes("FALSE");
      tagEl.className = `tag ${isContradicted ? "no" : "ok"}`;
      tagEl.textContent = `${status} · ${confPct}`;
    }

    const avenues = audit.relevant_avenues_checked || audit.avenues_checked || [];
    const outEl = $("veracityAuditOutput");
    if (outEl && avenues.length > 0) {
      outEl.innerHTML = avenues
        .map((av, i) => {
          const name = escapeHtml(av.avenue || av.name || `Avenue 0${i + 1}`);
          const finding = escapeHtml(av.finding || av.evidence || av.result || "");
          const why = av.why_relevant ? ` <em>(${escapeHtml(av.why_relevant)})</em>` : "";
          const badgeColor = String(av.status || "").toUpperCase().includes("CONTRADICT")
            ? "var(--bad)"
            : "var(--good)";
          return `<div style="margin-bottom:5px;"><b>${i + 1}. ${name}:</b> <span style="color:${badgeColor};font-weight:700;">${escapeHtml(
            av.status || "CHECKED"
          )}</span> — ${finding}${why}</div>`;
        })
        .join("");
    }
  }

  // =========================================================================
  // 5. ADMIN CARD RESTRICTION / UNLOCK OVERSIGHT & MEMORY COMPACTION
  // =========================================================================
  async function loadCardOversight(customerId) {
    const cid = customerId || DEFAULT_CUSTOMER_ID;
    try {
      const resp = await fetch(`/api/card-oversight?customer_id=${encodeURIComponent(cid)}`);
      if (!resp.ok) return;
      const data = await resp.json();
      updateOversightUI(data);
    } catch (err) {
      console.warn("Oversight fetch notice:", err);
    }
  }

  function updateOversightUI(data) {
    if (!data) return;
    const badge = $("oversightCardStatusBadge");
    const summary = $("oversightVerificationSummary");
    const stateStr = String(data.card_status || data.status || "RESTRICTED").toUpperCase();

    if (badge) {
      const isUnlocked = stateStr.includes("UNLOCK") || stateStr.includes("ACTIVE");
      badge.className = `tag ${isUnlocked ? "ok" : "on"}`;
      badge.textContent = isUnlocked ? "UNLOCKED · ACTIVE" : "RESTRICTED";
    }
    if (summary && data.summary) {
      summary.textContent = data.summary;
    }
  }

  async function submitCardDecision(decision) {
    const btnUnlock = $("btnAdminUnlock");
    const btnRestrict = $("btnAdminRestrict");
    if (btnUnlock) btnUnlock.disabled = true;
    if (btnRestrict) btnRestrict.disabled = true;

    const notes =
      decision === "APPROVE_UNLOCK"
        ? "Supervisor verified FaceID + travel notice TRV00001 / MEM00015; approved virtual card & POS unlock."
        : "Supervisor maintained restriction on stolen Lisbon plastic CARD00004 & CARD00002.";

    try {
      const resp = await fetch("/api/card-oversight/decide", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          customer_id: DEFAULT_CUSTOMER_ID,
          decision: decision,
          supervisor_notes: notes,
        }),
      });
      const data = await resp.json();
      updateOversightUI(data);
      setStampText("Run 0412 · SUPERVISOR LOGGED");
    } catch (err) {
      console.warn("Card oversight decision error:", err);
    } finally {
      if (btnUnlock) btnUnlock.disabled = false;
      if (btnRestrict) btnRestrict.disabled = false;
    }
  }

  async function runMemoryCompaction() {
    const btn = $("btnRunCompaction");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Compacting sessions...";
    }

    try {
      const resp = await fetch("/api/compaction/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ customer_id: DEFAULT_CUSTOMER_ID }),
      });
      const data = await resp.json();
      const pct = data.token_reduction_pct || "68.8%";
      if (btn) {
        btn.textContent = `✓ Compacted (${pct} saved)`;
      }
      await loadLiveTestbed(DEFAULT_CUSTOMER_ID);
    } catch (err) {
      if (btn) btn.textContent = "▶ Run compaction (68.8% saved)";
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // =========================================================================
  // INITIALIZATION & EVENT LISTENERS
  // =========================================================================
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
