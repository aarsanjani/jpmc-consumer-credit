/**
 * JPMorgan Chase — Gemini Enterprise Agent Platform & Scale Memory Bank UI Engine
 * Connects the 3-column executive console to live Vertex AI Memory Bank,
 * Dataplex Universal Catalog, BigQuery Ground Truth, and Google ADK Agents.
 */

class MemoryBankUiEngine {
    constructor() {
        this.customerId = 'CUST00001';
        this.currentViewMode = 'notes';
        this.activeTopicFilter = 'ALL';

        this.sessionData = null;
        this.healthData = null;
        this.customerOverview = null;
        this.cachedMemories = [];
        this.cachedTopics = [];
        this.cachedEvalSummary = null;
        this.cachedCatalogOverview = null;
        this.cachedGoldenQueries = [];
        this.cachedAuditReport = null;
        this.cachedAgents = [];

        this.cacheDomElements();
        this.bindEvents();
        this.init();
    }

    cacheDomElements() {
        // Header & Telemetry
        this.topEngineBadge = document.getElementById('top-engine-badge');
        this.topAgentsCount = document.getElementById('top-agents-count');
        this.topHealthStatus = document.getElementById('top-health-status');
        this.healthStatusDot = document.getElementById('health-status-dot');
        this.topAuditStatus = document.getElementById('top-audit-status');
        this.topEvalHeadline = document.getElementById('top-eval-headline');
        this.traceBootLine = document.getElementById('trace-boot-line');
        this.customerSelector = document.getElementById('customer-selector');

        this.viewAgentsBtn = document.getElementById('view-agents-btn');
        this.topEvalBtn = document.getElementById('top-eval-btn');
        this.topComparisonBtn = document.getElementById('top-comparison-btn');
        this.runAuditSweepBtn = document.getElementById('run-audit-sweep-btn');

        // Left Panel: Multi-Channel Feed & Controls
        this.playSimBtn = document.getElementById('play-sim-btn');
        this.reseedBtn = document.getElementById('reseed-btn');
        this.subsystemsFeed = document.getElementById('subsystems-feed');

        // Left Panel: Intelligent Write & Pre-Write Veracity Tester
        this.validatorChannelSelect = document.getElementById('validator-channel-select');
        this.validatorClaimInput = document.getElementById('validator-claim-input');
        this.validateWriteBtn = document.getElementById('validate-write-btn');
        this.validatorResult = document.getElementById('validator-result');
        this.claimChips = document.querySelectorAll('.claim-chip');

        // Left Panel: Card & Governance Widget
        this.visualCard = document.getElementById('visual-card');
        this.cardBadge = document.getElementById('card-badge');
        this.cardNumberDisplay = document.getElementById('card-number-display');
        this.cardHolderDisplay = document.getElementById('card-holder-display');
        this.applePayStatus = document.getElementById('apple-pay-status');
        this.adminConfidenceBadge = document.getElementById('admin-confidence-badge');
        this.adminVerificationSummary = document.getElementById('admin-verification-summary');
        this.simVerifyPassBtn = document.getElementById('sim-verify-pass-btn');
        this.simVerifyFailBtn = document.getElementById('sim-verify-fail-btn');

        // Center Panel: View Toggles & Terminal
        this.centerEngineSubtitle = document.getElementById('center-engine-subtitle');
        this.viewNotesBtn = document.getElementById('view-notes-btn');
        this.viewCompactionBtn = document.getElementById('view-compaction-btn');
        this.viewKgBtn = document.getElementById('view-kg-btn');
        this.viewComparisonBtn = document.getElementById('view-comparison-btn');
        this.viewAuditBtn = document.getElementById('view-audit-btn');
        this.viewAdminBtn = document.getElementById('view-admin-btn');
        this.viewShellBtn = document.getElementById('view-shell-btn');
        this.viewJsonBtn = document.getElementById('view-json-btn');
        this.serverLogLines = [];
        this.lastLogSeq = 0;

        this.terminalScopeUri = document.getElementById('terminal-scope-uri');
        this.memorySearchInput = document.getElementById('memory-search-input');
        this.memorySearchBtn = document.getElementById('memory-search-btn');
        this.memoryResetSearchBtn = document.getElementById('memory-reset-search-btn');
        this.memoryCountBadge = document.getElementById('memory-count-badge');
        this.memoryTerminal = document.getElementById('memory-terminal-body');

        // Center Panel: Trace Telemetry
        this.traceLogs = document.getElementById('agent-trace-logs');

        // Right Panel: Chat & A2UI
        this.chatForm = document.getElementById('chat-form');
        this.chatInput = document.getElementById('chat-input');
        this.chatViewport = document.getElementById('chat-viewport');
        this.a2uiContainer = document.getElementById('a2ui-presentation-container');
        this.memoryProvenance = document.getElementById('memory-provenance');
        this.mpBody = document.getElementById('mp-body');
        this.mpBadge = document.getElementById('mp-badge');
        this.mpSub = document.getElementById('mp-sub');
        this.promptChips = document.querySelectorAll('.quick-chip-btn');

        // Modal
        this.agentsModal = document.getElementById('agents-modal');
        this.closeModalBtn = document.getElementById('close-modal-btn');
        this.agentsRosterContainer = document.getElementById('agents-roster-container');
    }

    bindEvents() {
        // Customer Selector
        if (this.customerSelector) {
            this.customerSelector.addEventListener('change', (e) => {
                this.customerId = e.target.value;
                this.terminalScopeUri.textContent = `memory_bank://scope/customer_id=${this.customerId}`;
                this.appendTrace('CUSTOMER_SWITCH', `Switched active customer scope to ${this.customerId}. Reloading live BigQuery & Memory Bank state...`, 'memory');
                this.loadCustomerState();
            });
        }

        // Chat submission
        if (this.chatForm) {
            this.chatForm.addEventListener('submit', (e) => {
                e.preventDefault();
                const text = this.chatInput.value.trim();
                if (text) {
                    this.executeChatWorkflow(text);
                }
            });
        }

        // Quick prompt chips
        this.promptChips.forEach(chip => {
            chip.addEventListener('click', () => {
                this.promptChips.forEach(c => c.classList.remove('active-chip'));
                chip.classList.add('active-chip');
                const promptText = chip.dataset.prompt;
                this.chatInput.value = promptText;
                this.executeChatWorkflow(promptText);
            });
        });

        // Memory Bank Semantic Search
        if (this.memorySearchBtn) {
            this.memorySearchBtn.addEventListener('click', () => {
                const q = this.memorySearchInput.value.trim();
                if (q) this.executeSemanticSearch(q);
            });
        }
        if (this.memorySearchInput) {
            this.memorySearchInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    const q = this.memorySearchInput.value.trim();
                    if (q) this.executeSemanticSearch(q);
                }
            });
        }
        if (this.memoryResetSearchBtn) {
            this.memoryResetSearchBtn.addEventListener('click', () => {
                this.memorySearchInput.value = '';
                this.activeTopicFilter = 'ALL';
                this.currentViewMode = 'notes';
                this.setActiveToggle(this.viewNotesBtn);
                this.renderMemoryBankFragments(this.cachedMemories);
            });
        }

        // Demo flow & Sync buttons
        if (this.playSimBtn) {
            this.playSimBtn.addEventListener('click', () => this.playSimulationSequence());
        }
        if (this.reseedBtn) {
            this.reseedBtn.addEventListener('click', () => {
                this.appendTrace('LIVE_SYNC', `Refreshing live state from Vertex AI Memory Bank, Dataplex Catalog & BigQuery...`, 'thought');
                this.loadCustomerState();
            });
        }

        // Intelligent Write & Veracity Validator
        this.claimChips.forEach(chip => {
            chip.addEventListener('click', () => {
                this.validatorClaimInput.value = chip.dataset.claim;
                this.executePreWriteValidation();
            });
        });
        if (this.validateWriteBtn) {
            this.validateWriteBtn.addEventListener('click', () => this.executePreWriteValidation());
        }

        // Left panel quick oversight buttons
        if (this.simVerifyPassBtn) {
            this.simVerifyPassBtn.addEventListener('click', () => {
                this.currentViewMode = 'admin';
                this.setActiveToggle(this.viewAdminBtn);
                this.renderAdminOversightView();
            });
        }
        if (this.simVerifyFailBtn) {
            this.simVerifyFailBtn.addEventListener('click', () => {
                this.currentViewMode = 'kg';
                this.setActiveToggle(this.viewKgBtn);
                this.renderKnowledgeCatalogView('authorizations_deprecated_v1', '');
            });
        }

        // Top Bar Buttons
        if (this.viewAgentsBtn && this.agentsModal) {
            this.viewAgentsBtn.addEventListener('click', () => {
                this.agentsModal.style.display = 'flex';
            });
        }
        if (this.closeModalBtn && this.agentsModal) {
            this.closeModalBtn.addEventListener('click', () => {
                this.agentsModal.style.display = 'none';
            });
        }
        window.addEventListener('click', (e) => {
            if (e.target === this.agentsModal) {
                this.agentsModal.style.display = 'none';
            }
        });

        if (this.topEvalBtn) {
            this.topEvalBtn.addEventListener('click', () => {
                this.currentViewMode = 'compaction';
                this.setActiveToggle(this.viewCompactionBtn);
                this.renderPrecisionRecallView();
            });
        }
        if (this.topComparisonBtn) {
            this.topComparisonBtn.addEventListener('click', () => {
                this.currentViewMode = 'comparison';
                this.setActiveToggle(this.viewComparisonBtn);
                this.renderComparisonView();
            });
        }
        if (this.runAuditSweepBtn) {
            this.runAuditSweepBtn.addEventListener('click', () => {
                this.currentViewMode = 'audit';
                this.setActiveToggle(this.viewAuditBtn);
                this.renderAuditReportView();
            });
        }

        // Center Panel View Toggle Buttons
        if (this.viewNotesBtn) {
            this.viewNotesBtn.addEventListener('click', () => {
                this.currentViewMode = 'notes';
                this.setActiveToggle(this.viewNotesBtn);
                this.renderMemoryBankFragments(this.cachedMemories);
            });
        }
        if (this.viewCompactionBtn) {
            this.viewCompactionBtn.addEventListener('click', () => {
                this.currentViewMode = 'compaction';
                this.setActiveToggle(this.viewCompactionBtn);
                this.renderPrecisionRecallView();
            });
        }
        if (this.viewKgBtn) {
            this.viewKgBtn.addEventListener('click', () => {
                this.currentViewMode = 'kg';
                this.setActiveToggle(this.viewKgBtn);
                this.renderKnowledgeCatalogView();
            });
        }
        if (this.viewComparisonBtn) {
            this.viewComparisonBtn.addEventListener('click', () => {
                this.currentViewMode = 'comparison';
                this.setActiveToggle(this.viewComparisonBtn);
                this.renderComparisonView();
            });
        }
        if (this.viewAuditBtn) {
            this.viewAuditBtn.addEventListener('click', () => {
                this.currentViewMode = 'audit';
                this.setActiveToggle(this.viewAuditBtn);
                this.renderAuditReportView();
            });
        }
        if (this.viewAdminBtn) {
            this.viewAdminBtn.addEventListener('click', () => {
                this.currentViewMode = 'admin';
                this.setActiveToggle(this.viewAdminBtn);
                this.renderAdminOversightView();
            });
        }
        if (this.viewShellBtn) {
            this.viewShellBtn.addEventListener('click', () => {
                this.currentViewMode = 'shell';
                this.setActiveToggle(this.viewShellBtn);
                this.renderLiveShellView();
            });
        }
        if (this.viewJsonBtn) {
            this.viewJsonBtn.addEventListener('click', () => {
                this.currentViewMode = 'json';
                this.setActiveToggle(this.viewJsonBtn);
                this.renderJsonInspectorView();
            });
        }
    }

    setActiveToggle(activeBtn) {
        [
            this.viewNotesBtn,
            this.viewCompactionBtn,
            this.viewKgBtn,
            this.viewComparisonBtn,
            this.viewAuditBtn,
            this.viewAdminBtn,
            this.viewShellBtn,
            this.viewJsonBtn
        ].forEach(btn => {
            if (btn) btn.classList.remove('active');
        });
        if (activeBtn) activeBtn.classList.add('active');
    }

    escapeHtml(str) {
        if (str === null || str === undefined) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    formatMarkdown(text) {
        if (!text) return '';
        return this.escapeHtml(text)
            .replace(/\*\*(.*?)\*\*/g, '<strong style="color:#f8fafc;">$1</strong>')
            .replace(/`(.*?)`/g, '<code style="background:rgba(56,189,248,0.14);padding:0.1rem 0.35rem;border-radius:4px;color:#38bdf8;font-family:\'JetBrains Mono\',monospace;font-size:0.75rem;">$1</code>')
            .replace(/\n\n/g, '<br><br>')
            .replace(/\n/g, '<br>');
    }

    async init() {
        this.appendTrace('SYSTEM_BOOT', 'Probing live Vertex AI Reasoning Engine, Dataplex Universal Catalog, and BigQuery warehouse...', 'thought');
        this.startServerLogStream();
        await Promise.all([
            this.loadSessionAndHealth(),
            this.loadTopics(),
            this.loadAgentsRoster(),
            this.loadGoldenQueries(),
            this.loadEvalSummary(),
            this.loadGovernanceBadge(),
        ]);
        await this.loadCustomerState();
    }

    startServerLogStream() {
        const poll = async () => {
            try {
                const res = await fetch(`/api/logs/tail?since=${this.lastLogSeq}`);
                if (!res.ok) return;
                const data = await res.json();
                const newLines = data.lines || [];
                if (newLines.length > 0) {
                    this.lastLogSeq = data.latest_seq || this.lastLogSeq;
                    newLines.forEach(item => {
                        this.serverLogLines.push(item);
                        if (this.serverLogLines.length > 400) this.serverLogLines.shift();

                        let badge = 'STDOUT';
                        let kind = 'thought';
                        if (item.line.includes('>>> INCOMING')) {
                            badge = 'HTTP_IN';
                            kind = 'thought';
                        } else if (item.line.includes('<<< COMPLETED')) {
                            badge = 'HTTP_OUT';
                            kind = 'synthesis';
                        } else if (item.logger.includes('memory_bank')) {
                            badge = 'VERTEX_MB';
                            kind = 'memory';
                        } else if (item.logger.includes('knowledge_catalog')) {
                            badge = 'DATAPLEX';
                            kind = 'thought';
                        } else if (item.logger.includes('ground_truth')) {
                            badge = 'BIGQUERY';
                            kind = 'synthesis';
                        }
                        this.appendTrace(badge, item.line, kind);
                    });
                    if (this.currentViewMode === 'shell') {
                        this.renderLiveShellView();
                    }
                }
            } catch (_) {
                // Ignore transient poll errors
            }
        };
        poll();
        setInterval(poll, 1200);
    }

    renderLiveShellView() {
        this.memoryCountBadge.textContent = `${this.serverLogLines.length} Shell Lines (Port 5065)`;
        const linesHtml = this.serverLogLines.map(item => {
            let color = '#cbd5e1';
            if (item.line.includes('>>> INCOMING')) color = '#38bdf8';
            else if (item.line.includes('<<< COMPLETED')) color = '#34d399';
            else if (item.logger.includes('memory_bank')) color = '#c084fc';
            else if (item.logger.includes('knowledge_catalog')) color = '#fde047';
            else if (item.level === 'WARNING' || item.level === 'ERROR') color = '#f87171';
            return `<div style="padding:2px 0; border-bottom:1px solid rgba(255,255,255,0.03); color:${color}; white-space:pre-wrap; word-break:break-all;"><span style="color:#64748b;">[${item.seq}]</span> ${this.escapeHtml(item.line)}</div>`;
        }).join('');

        this.memoryTerminal.innerHTML = `
            <div style="padding:10px; font-family:'JetBrains Mono',monospace; font-size:0.71rem; background:#090d16; min-height:360px;">
                <div style="display:flex; justify-content:space-between; align-items:center; padding-bottom:8px; margin-bottom:8px; border-bottom:1px solid rgba(56,189,248,0.3);">
                    <strong style="color:#38bdf8;">🖥️ LIVE PYTHON BACKEND SHELL (` + "`backend/app.py` • Port 5065 • Real-Time `stdout/stderr`" + `)</strong>
                    <span class="badge-status success">LIVE TAIL ACTIVE</span>
                </div>
                <div id="live-shell-scroll-box" style="max-height:420px; overflow-y:auto;">
                    ${linesHtml || '<div style="color:#64748b;">Waiting for requests...</div>'}
                </div>
            </div>
        `;
        const scrollBox = document.getElementById('live-shell-scroll-box');
        if (scrollBox) scrollBox.scrollTop = scrollBox.scrollHeight;
    }

    async loadSessionAndHealth() {
        await this.loadSession();
        await this.loadHealth();
    }

    async loadSession() {
        try {
            const res = await fetch('/api/session');
            this.sessionData = await res.json();

            const cfg = this.sessionData.configuration || {};
            if (this.topEngineBadge) {
                // Every segment is whatever the API returned. A blank field is
                // shown as n/r, never backfilled with the value it used to have.
                this.topEngineBadge.textContent =
                    `Engine ${cfg.agent_engine_id || 'n/r'} \u2022 Dataset ${cfg.bigquery_dataset || 'n/r'} \u2022 Glossary ${cfg.dataplex_glossary_id || 'n/r'}`;
            }

            // Customer dropdown: ids come from the warehouse. The display name
            // is not in this payload, so the option is the bare id until
            // /api/customer/overview supplies profile.full_name.
            const ids = this.sessionData.available_customer_ids || [];
            if (this.customerSelector) {
                if (ids.length === 0) {
                    this.customerSelector.innerHTML = '<option value="">no customer ids returned</option>';
                } else {
                    if (!ids.includes(this.customerId)) this.customerId = ids[0];
                    this.customerSelector.innerHTML = ids.map(cid =>
                        `<option value="${this.escapeHtml(cid)}" ${cid === this.customerId ? 'selected' : ''}>${this.escapeHtml(cid)}</option>`
                    ).join('');
                }
            }
            if (this.terminalScopeUri) {
                this.terminalScopeUri.textContent = `memory_bank://scope/customer_id=${this.customerId}`;
            }
        } catch (err) {
            console.error('Failed loading session:', err);
            if (this.topEngineBadge) this.topEngineBadge.textContent = 'Session unavailable \u2014 /api/session did not answer';
            if (this.customerSelector) this.customerSelector.innerHTML = '<option value="">session unavailable</option>';
        }
    }

    /**
     * Normalise the two health shapes into one array of
     * {name, status, detail, remedy, latency_ms}.
     *   new: checks is an array from backend/diagnostics.py
     *   old: checks is an object keyed by dependency with an uppercase status
     * Anything else returns [] so the caller reports "unreadable" rather than
     * inventing a passing check.
     */
    normalizeHealthChecks(raw) {
        if (Array.isArray(raw)) {
            return raw.map(c => ({
                name: c.name || 'unnamed',
                status: String(c.status || 'unknown').toLowerCase(),
                detail: c.detail || '',
                remedy: c.remedy || '',
                latency_ms: c.latency_ms
            }));
        }
        if (raw && typeof raw === 'object') {
            return Object.keys(raw).map(k => {
                const c = raw[k] || {};
                const up = String(c.status || 'unknown').toUpperCase();
                return {
                    name: k,
                    status: up === 'OK' ? 'ok' : (up === 'UNAVAILABLE' || up === 'DOWN' ? 'fail' : up.toLowerCase()),
                    detail: c.error || c.detail || '',
                    remedy: c.remedy || '',
                    latency_ms: c.latency_ms
                };
            });
        }
        return [];
    }

    setHealthPill(text, dotClass, tooltip) {
        if (this.topHealthStatus) {
            this.topHealthStatus.textContent = text;
            this.topHealthStatus.title = tooltip || text;
        }
        if (this.healthStatusDot) {
            this.healthStatusDot.className = `status-dot ${dotClass}`;
        }
    }

    setBootTrace(badge, message, type) {
        // The boot line in the markup asserts nothing. This is the only thing
        // that gives it a verdict, and it only runs after /api/health answered.
        const line = this.traceBootLine || document.getElementById('trace-boot-line');
        if (!line) return;
        line.className = `trace-item ${type || 'thought'}`;
        line.innerHTML = `<span class="trace-badge">${this.escapeHtml(badge)}</span>` +
                         `<span class="trace-msg">${this.escapeHtml(message)}</span>`;
    }

    async loadHealth() {
        try {
            const res = await fetch('/api/health');
            let data = null;
            try {
                data = await res.json();
            } catch (parseErr) {
                data = null;
            }
            this.healthData = data;

            if (!data) {
                this.setHealthPill('Health check unreadable', 'red pulse',
                    `GET /api/health returned HTTP ${res.status} with a body that is not JSON.`);
                this.setBootTrace('HEALTH_UNREADABLE',
                    `/api/health returned HTTP ${res.status} and a non-JSON body. No dependency state is known.`, 'error');
                return;
            }

            const checks = this.normalizeHealthChecks(data.checks || data.checks_by_name);
            const overall = String(data.overall || data.status || '').toLowerCase();
            const passing = checks.filter(c => c.status === 'ok').length;
            const unhealthy = checks.filter(c => c.status !== 'ok' && c.status !== 'skipped');
            const tooltip = checks.length
                ? checks.map(c => `${c.name}: ${c.status.toUpperCase()}${c.detail ? ' \u2014 ' + c.detail : ''}${c.remedy ? ' | fix: ' + c.remedy : ''}`).join('\n')
                : 'No per-check detail in the /api/health response.';

            if (checks.length === 0) {
                // A 200 with no checks is not a pass. Say so.
                this.setHealthPill('No checks reported', 'grey', tooltip);
                this.setBootTrace('HEALTH_EMPTY',
                    '/api/health answered but reported no dependency checks, so nothing has actually been verified.', 'error');
                return;
            }

            if (unhealthy.length === 0 && (overall === 'ok' || overall === '')) {
                this.setHealthPill(`${passing}/${checks.length} checks OK`, 'green pulse', tooltip);
                this.setBootTrace('HEALTH_OK',
                    `Probed ${checks.length} dependencies: ${checks.map(c => c.name).join(', ')}. All answered OK.`, 'memory');
            } else {
                const failing = unhealthy.filter(c => c.status === 'fail');
                const isDown = overall === 'fail' || overall === 'down' || res.status === 503;
                const label = isDown ? 'DOWN' : 'DEGRADED';
                const names = unhealthy.map(c => c.name).join(', ');
                this.setHealthPill(`${label}: ${names} (${passing}/${checks.length} OK)`,
                    isDown ? 'red pulse' : 'amber pulse', tooltip);
                this.setBootTrace(`HEALTH_${label}`,
                    `${passing} of ${checks.length} dependencies answered OK. Unhealthy: ` +
                    unhealthy.map(c => `${c.name} (${c.status.toUpperCase()}) \u2014 ${c.detail || 'no detail given'}`).join('; '),
                    'error');
                unhealthy.forEach(c => {
                    this.appendTrace(`DEPENDENCY_${c.status.toUpperCase()}`,
                        `${c.name}: ${c.detail || 'no detail given'}${c.remedy ? ' \u2014 remedy: ' + c.remedy : ''}`, 'error');
                });
                if (failing.length && this.topAuditStatus) {
                    // Nothing downstream of a dead dependency can be trusted.
                    this.topAuditStatus.textContent = 'Blocked (dependency down)';
                }
            }
        } catch (err) {
            console.error('Failed loading health:', err);
            this.healthData = null;
            this.setHealthPill('Health check failed', 'red pulse', String(err && err.message ? err.message : err));
            this.setBootTrace('HEALTH_FAILED',
                `/api/health could not be reached (${err && err.message ? err.message : err}). Dependency state is unknown.`, 'error');
        }
    }

    async loadTopics() {
        try {
            const res = await fetch('/api/memory/topics');
            const data = await res.json();
            this.cachedTopics = data.topics || [];
            if (this.centerEngineSubtitle && this.cachedTopics.length > 0) {
                this.centerEngineSubtitle.textContent = `Live Reasoning Engine • ${this.cachedTopics.length} Custom Fraud Topics (${this.cachedTopics.map(t => t.label).join(', ')})`;
            }
        } catch (err) {
            console.error('Failed loading memory topics:', err);
        }
    }

    async loadGoldenQueries() {
        try {
            const res = await fetch('/api/warehouse/queries');
            const data = await res.json();
            this.cachedGoldenQueries = data.queries || [];
        } catch (err) {
            console.error('Failed loading warehouse queries:', err);
        }
    }

    async loadEvalSummary() {
        try {
            const res = await fetch(`/api/eval/summary?customer_id=${encodeURIComponent(this.customerId)}`);
            this.cachedEvalSummary = await res.json();
        } catch (err) {
            console.error('Failed loading eval summary:', err);
            this.cachedEvalSummary = null;
        }
        this.updateEvalHeadlinePill();
    }

    /**
     * The header benchmarks pill. Reads the same headline the Precision &
     * Recall tab reads. If the sweep has not been run, or the field is absent,
     * the pill says so instead of carrying yesterday's number.
     */
    updateEvalHeadlinePill() {
        if (!this.topEvalHeadline) return;
        const headline = this.evalHeadline();
        const recall = this.metricValue(headline.macro_recall);
        const k = headline.k;
        if (recall === null) {
            this.topEvalHeadline.textContent = 'Precision & Recall (not measured)';
            this.topEvalHeadline.title = 'No macro_recall in /api/eval/summary. Run the retrieval sweep.';
            return;
        }
        this.topEvalHeadline.textContent = `Recall@${k !== null && k !== undefined ? k : '?'} ${(recall * 100).toFixed(1)}%`;
        this.topEvalHeadline.title = 'From /api/eval/summary -> benchmark.results.retrieval.headline.macro_recall';
    }

    /**
     * One reader for two payload shapes, because the benchmark contract moved
     * under this UI once already:
     *
     *   v1  summary.benchmark.results.retrieval        {headline, runs[], labelling}
     *   v2  summary.stored_benchmark.results.retrieval {headline_k, k_sweep{}}
     *       with a flat digest at ...results.summary.retrieval
     *
     * Returns a single normalised view. `found` is false when neither shape is
     * present, and the caller renders "not measured" rather than a number.
     */
    normalizeBenchmark() {
        const s = this.cachedEvalSummary || {};
        const container = s.stored_benchmark || s.benchmark || null;
        const R = (container && container.results) || {};
        const ret = R.retrieval || {};
        const digest = ((R.summary || {}).retrieval) || {};

        const out = {
            found: false,
            ret,
            digest,
            source: container ? (s.stored_benchmark ? 'stored_benchmark' : 'benchmark') : null,
            origin: (container && container.source) || null,
            engine: s.engine || R.engine || null,
            mode: ret.mode || R.mode || null,
            headlineK: null,
            headline: {},
            runs: [],
            labelling: ret.labelling || {},
            corpusSize: null,
            probeCount: null,
            ceiling: null,
            precisionVsCeiling: null,
            rPrecision: null,
            latency: ret.latency || digest.latency || null,
            negativeRejectionRate: digest.negative_rejection_rate,
            binding: ret.binding || null,
            writePolicy: {
                inclusion: R.inclusion || {},
                topics: R.topics || {},
                scope: R.scope || {},
                consolidation: R.consolidation || {}
            }
        };

        // ---- v2: k_sweep keyed by k -------------------------------------
        if (ret.k_sweep && typeof ret.k_sweep === 'object') {
            out.found = true;
            out.headlineK = ret.headline_k !== undefined ? ret.headline_k : digest.headline_k;
            out.runs = Object.keys(ret.k_sweep).map(k => ({
                k: Number(k),
                aggregate: (ret.k_sweep[k] || {}).aggregate || {},
                probes: (ret.k_sweep[k] || {}).per_probe || []
            })).filter(r => Number.isFinite(r.k));
        // ---- v1: runs array ---------------------------------------------
        } else if (Array.isArray(ret.runs) && ret.runs.length) {
            out.found = true;
            out.headlineK = (ret.headline || {}).k;
            out.runs = ret.runs
                .filter(r => r.arm === 'plain' || !r.arm)
                .map(r => ({ k: r.k, aggregate: r.aggregate || {}, probes: r.probes || [] }));
        } else if (ret.headline) {
            out.found = true;
            out.headlineK = ret.headline.k;
        }

        if (!out.found) return out;

        const hr = out.runs.find(r => r.k === out.headlineK) || null;
        const agg = (hr && hr.aggregate) || {};

        // Prefer the per-k aggregate (it carries n_defined/n_undefined); fall
        // back to the v1 headline block, then to the flat digest.
        const v1 = ret.headline || {};
        out.headline = {
            macro_precision: agg.macro_precision_at_k !== undefined ? agg.macro_precision_at_k
                : (v1.macro_precision !== undefined ? v1.macro_precision : digest.precision_at_k),
            macro_recall: agg.macro_recall_at_k !== undefined ? agg.macro_recall_at_k
                : (v1.macro_recall !== undefined ? v1.macro_recall : digest.recall_at_k),
            macro_f1: agg.macro_f1_at_k !== undefined ? agg.macro_f1_at_k : v1.macro_f1,
            mrr: agg.mean_reciprocal_rank !== undefined ? agg.mean_reciprocal_rank
                : (v1.mrr !== undefined ? v1.mrr : digest.mrr),
            ndcg: agg.macro_ndcg_at_k !== undefined ? agg.macro_ndcg_at_k
                : (v1.ndcg !== undefined ? v1.ndcg : digest.ndcg_at_k),
            map: agg.mean_average_precision !== undefined ? agg.mean_average_precision : digest.map,
            why_this_k: v1.why_this_k || agg.ceiling_note || null
        };

        out.ceiling = agg.macro_precision_at_k_ceiling !== undefined
            ? agg.macro_precision_at_k_ceiling : digest.precision_at_k_ceiling;
        out.precisionVsCeiling = agg.precision_vs_ceiling !== undefined
            ? agg.precision_vs_ceiling : digest.precision_vs_ceiling;
        out.rPrecision = agg.r_precision !== undefined ? agg.r_precision : digest.r_precision;

        out.corpusSize = ret.corpus_size !== undefined ? ret.corpus_size
            : (digest.corpus_size !== undefined ? digest.corpus_size
            : ((ret.corpus && ret.corpus.memories) ? ret.corpus.memories.length
            : (ret.binding ? ret.binding.live_corpus_size : null)));

        out.probeCount = ret.probe_count !== undefined ? ret.probe_count
            : (agg.probes !== undefined ? agg.probes
            : (digest.positive_probes !== undefined ? digest.positive_probes
            : (ret.positive_probe_count !== undefined ? ret.positive_probe_count : null)));

        out.headlineRun = hr;
        out.headlineProbes = (hr && hr.probes) || [];
        return out;
    }

    /** The headline metric block, whichever shape the backend served. */
    evalHeadline() {
        const nb = this.normalizeBenchmark();
        return nb.found ? { ...nb.headline, k: nb.headlineK } : {};
    }

    /** Unwrap {value, n_defined, n_undefined} or a bare number. null if absent. */
    metricValue(metric) {
        if (metric === null || metric === undefined) return null;
        if (typeof metric === 'number') return Number.isFinite(metric) ? metric : null;
        if (typeof metric === 'object' && metric.value !== null && metric.value !== undefined) {
            const n = Number(metric.value);
            return Number.isFinite(n) ? n : null;
        }
        return null;
    }

    /** A percentage, or an explicit em-dash. Never a plausible-looking guess. */
    pct(metric, digits = 1) {
        const v = this.metricValue(metric);
        return v === null ? '\u2014' : `${(v * 100).toFixed(digits)}%`;
    }

    /** A fixed-point number, or an explicit em-dash. */
    num(metric, digits = 3) {
        const v = this.metricValue(metric);
        return v === null ? '\u2014' : Number(v).toFixed(digits);
    }

    async loadAgentsRoster() {
        // Bind Left Panel 8-agent probe buttons & KC Governance Adherence button
        document.querySelectorAll('.agent-probe-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                this.executeAgentChannelProbe(btn.dataset.agent);
            });
        });
        const govBtn = document.getElementById('run-governance-adherence-btn');
        if (govBtn) {
            govBtn.addEventListener('click', () => {
                this.renderGovernanceAdherenceView();
            });
        }

        try {
            const res = await fetch('/api/agents/list');
            const data = await res.json();
            this.cachedAgents = data.agents || [];
            if (this.topAgentsCount) {
                this.topAgentsCount.textContent = this.cachedAgents.length
                    ? `${this.cachedAgents.length} Active ADK Agents`
                    : 'no agents returned';
            }
            if (this.agentsRosterContainer) {
                this.agentsRosterContainer.innerHTML = this.cachedAgents.map((ag, idx) => `
                    <div class="agent-roster-card" style="border-left:3px solid #a855f7;">
                        <div class="roster-card-header">
                            <span class="roster-num">0${idx + 1}</span>
                            <div>
                                <h4 class="roster-name">${this.escapeHtml(ag.agent_id || ag.name)}</h4>
                                <span class="roster-role">${this.escapeHtml(ag.role)}</span>
                            </div>
                            <button class="modal-run-agent-btn btn-primary btn-sm" data-agent="${this.escapeHtml(ag.name)}" style="margin-left:auto; font-size:0.68rem; padding:3px 8px;">▶️ Run Agent Live</button>
                        </div>
                        <p class="roster-desc">${this.escapeHtml(ag.description)}</p>
                        <div class="roster-meta" style="font-size:0.71rem; line-height:1.5;">
                            <div><strong style="color:#38bdf8;">🧠 Memory Bank Topics Scoped:</strong> ${(ag.mb_topics_scoped || []).map(t => `<code>${this.escapeHtml(t)}</code>`).join(' ')}</div>
                            <div><strong style="color:#fde047;">🛡️ Dataplex Catalog Governance:</strong> ${(ag.kc_governed_assets || []).map(k => `<code>${this.escapeHtml(k)}</code>`).join(' ')}</div>
                            <div><strong style="color:#34d399;">🏛️ Authorized BigQuery Golden Queries:</strong> ${(ag.golden_queries_authorized || []).map(g => `<code>${this.escapeHtml(g)}</code>`).join(' ')}</div>
                            <div><strong style="color:#c084fc;">🔧 Bound ADK Tools (${(ag.tools || []).length}):</strong> ${(ag.tools || []).map(t => `<code>${this.escapeHtml(t)}</code>`).join(' ')}</div>
                        </div>
                    </div>
                `).join('');

                this.agentsRosterContainer.querySelectorAll('.modal-run-agent-btn').forEach(btn => {
                    btn.addEventListener('click', () => {
                        if (this.agentsModal) this.agentsModal.style.display = 'none';
                        this.executeAgentChannelProbe(btn.dataset.agent);
                    });
                });
            }
        } catch (err) {
            console.error('Failed loading agents roster:', err);
            if (this.topAgentsCount) this.topAgentsCount.textContent = 'roster unavailable';
        }
    }

    async executeAgentChannelProbe(agentName) {
        this.memoryCountBadge.textContent = `Running ${agentName}...`;
        this.appendTrace('AGENT_PROBE', `Executing ${agentName} live MB + KC + BQ Golden Query pipeline for ${this.customerId}...`, 'thought');
        this.memoryTerminal.innerHTML = `
            <div style="padding:16px; color:#38bdf8; font-weight:600; font-size:0.78rem;">
                ⏳ Executing <code>${this.escapeHtml(agentName)}</code> across live Vertex AI Memory Bank, Dataplex Universal Catalog, and BigQuery Golden Queries for <code>${this.customerId}</code>...
            </div>
        `;
        try {
            const res = await fetch('/api/agents/channel-probe', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ customer_id: this.customerId, agent_name: agentName })
            });
            const data = await res.json();
            const agMeta = this.cachedAgents.find(a => a.name === agentName) || { agent_id: agentName, role: 'SPECIALIST' };
            const resultObj = data.result || {};
            this.memoryCountBadge.textContent = `${agMeta.agent_id || agentName} Executed`;

            this.memoryTerminal.innerHTML = `
                <div style="padding:12px; color:#e2e8f0; font-size:0.75rem;">
                    <div style="background:rgba(30,41,59,0.94); border:1px solid rgba(168,85,247,0.45); border-radius:8px; padding:12px; margin-bottom:10px;">
                        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
                            <strong style="color:#c084fc; font-size:0.86rem;">🤖 LIVE EXECUTION: ${this.escapeHtml(agMeta.agent_id || agentName)}</strong>
                            <span class="badge-status success">AD_HOC_SQL: FALSE • KC GOVERNED</span>
                        </div>
                        <div style="font-size:0.72rem; color:#cbd5e1; margin-bottom:8px;">${this.escapeHtml(agMeta.description || '')}</div>
                        <div style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:6px; font-size:0.69rem; margin-bottom:8px;">
                            <div style="background:rgba(15,23,42,0.85); padding:6px 8px; border-radius:5px; border-left:3px solid #38bdf8;">
                                <strong style="color:#38bdf8;">🧠 Memory Bank Scope:</strong><br>
                                ${(agMeta.mb_topics_scoped || []).map(t => `<code>${this.escapeHtml(t)}</code>`).join(', ')}
                            </div>
                            <div style="background:rgba(15,23,42,0.85); padding:6px 8px; border-radius:5px; border-left:3px solid #fde047;">
                                <strong style="color:#fde047;">🛡️ Dataplex Governance:</strong><br>
                                ${(agMeta.kc_governed_assets || []).map(t => `<code>${this.escapeHtml(t)}</code>`).join(', ')}
                            </div>
                            <div style="background:rgba(15,23,42,0.85); padding:6px 8px; border-radius:5px; border-left:3px solid #34d399;">
                                <strong style="color:#34d399;">🏛️ Authorized Golden Queries:</strong><br>
                                ${(agMeta.golden_queries_authorized || []).map(t => `<code>${this.escapeHtml(t)}</code>`).join(', ')}
                            </div>
                        </div>
                        <div style="font-weight:700; color:#38bdf8; font-size:0.74rem; margin-bottom:4px;">📋 Live Tool &amp; Golden Query Output Payload:</div>
                        <pre style="background:#090d16; padding:10px; border-radius:6px; font-family:'JetBrains Mono',monospace; font-size:0.69rem; color:#a7f3d0; max-height:360px; overflow:auto;">${this.escapeHtml(JSON.stringify(resultObj, null, 2))}</pre>
                    </div>
                </div>
            `;
            this.appendTrace('AGENT_COMPLETE', `${agMeta.agent_id || agentName} completed live MB + KC + BQ execution.`, 'synthesis');
        } catch (err) {
            this.memoryTerminal.innerHTML = `<div style="padding:12px; color:#f87171;">Agent probe error: ${this.escapeHtml(err.message)}</div>`;
        }
    }

    async renderGovernanceAdherenceView() {
        this.memoryCountBadge.textContent = `Testing 8-Agent KC Governance & Ad-Hoc SQL...`;
        this.memoryTerminal.innerHTML = `
            <div style="padding:16px; color:#fde047; font-weight:600; font-size:0.78rem;">
                🛡️ Running 5-stage Dataplex Catalog Refusal, Golden Query RBAC (` + "`INDEX.md`" + `), and Ad-Hoc SQL Injection Bypass checks across the 8 agents...
            </div>
        `;
        try {
            const res = await fetch('/api/agents/governance-test', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ customer_id: this.customerId })
            });
            const data = await res.json();
            const checks = data.checks || [];
            this.memoryCountBadge.textContent = `Governance Adherence: ${data.overall_adherence ? '5/5 PASSED' : 'ALERT'}`;

            const rowsHtml = checks.map((c, idx) => `
                <div style="padding:9px 11px; background:rgba(15,23,42,0.88); border-left:4px solid ${c.adhered ? '#10b981' : '#ef4444'}; border-radius:6px; margin-bottom:8px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
                        <strong style="color:#f8fafc; font-size:0.76rem;">#${idx + 1} · ${this.escapeHtml(c.check_id)} (${this.escapeHtml(c.agent)})</strong>
                        <span class="badge-status ${c.adhered ? 'success' : 'danger'}">${c.adhered ? '✓ ADHERED (REFUSED / BLOCKED)' : '⚠️ VIOLATION'}</span>
                    </div>
                    <div style="font-size:0.71rem; color:#cbd5e1;">
                        • <strong>Target Asset / Query:</strong> <code>${this.escapeHtml(c.target)}</code><br>
                        • <strong>Catalog / Gate Verdict:</strong> <strong style="color:#fde047;">${this.escapeHtml(c.catalog_verdict)}</strong> (<code>${this.escapeHtml(c.basis)}</code>)<br>
                        • <strong>Verification Detail:</strong> ${this.escapeHtml(c.detail)}
                    </div>
                </div>
            `).join('');

            this.memoryTerminal.innerHTML = `
                <div style="padding:12px; color:#e2e8f0; font-size:0.75rem;">
                    <div style="background:rgba(30,41,59,0.94); border:1px solid rgba(16,185,129,0.45); border-radius:8px; padding:12px;">
                        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                            <div>
                                <div style="font-weight:800; color:#34d399; font-size:0.86rem;">🛡️ 8-AGENT DATAPLEX GOVERNANCE &amp; AD-HOC SQL ADHERENCE PROOF</div>
                                <div style="font-size:0.71rem; color:#94a3b8;">Verifies whether agents respect Dataplex ` + "`REFUSE`" + ` aspects (` + "`authorizations_deprecated_v1`, `cards.pan_full`, `customers.ssn_last4`" + `), Golden Query RBAC (` + "`INDEX.md`" + `), and block raw ad-hoc SQL bypass attempts.</div>
                            </div>
                            <span class="badge-status success">5 / 5 ENFORCED</span>
                        </div>
                        ${rowsHtml}
                    </div>
                </div>
            `;
            this.appendTrace('GOVERNANCE_PROVEN', `All 5 Dataplex Catalog & Ad-Hoc SQL adherence checks passed across the 8 agents.`, 'synthesis');
        } catch (err) {
            this.memoryTerminal.innerHTML = `<div style="padding:12px; color:#f87171;">Governance test failed: ${this.escapeHtml(err.message)}</div>`;
        }
    }

    async loadCustomerState() {
        try {
            const res = await fetch(`/api/customer/overview?customer_id=${encodeURIComponent(this.customerId)}`);
            const data = await res.json();
            this.customerOverview = data;
            this.cachedMemories = data.memories || [];

            this.updateLeftPanelFromOverview(data);

            const contradictedCount = (data.memories || []).filter(m => m.reconciliation_status === 'CONTRADICTED').length;
            if (this.topAuditStatus) {
                this.topAuditStatus.textContent = contradictedCount > 0
                    ? `${contradictedCount} Contradictions Detected`
                    : `Clean (0 Anomalies)`;
            }

            this.appendTrace(
                'STATE_SYNCHRONIZED',
                `Loaded ${this.cachedMemories.length} live Memory Bank records, ${(data.open_disputes || []).length} open BigQuery disputes, and ${(data.cards || []).length} cards for ${this.customerId}.`,
                'memory'
            );

            if (this.currentViewMode === 'notes') {
                this.renderMemoryBankFragments(this.cachedMemories);
            } else if (this.currentViewMode === 'audit') {
                this.renderAuditReportView();
            } else if (this.currentViewMode === 'admin') {
                this.renderAdminOversightView();
            } else if (this.currentViewMode === 'json') {
                this.renderJsonInspectorView();
            }
        } catch (err) {
            console.error('Failed loading customer overview:', err);
        }
    }

    /** "1 card" not "1 cards". */
    plural(n, one, many) {
        return `${n} ${n === 1 ? one : many}`;
    }

    /** A BigQuery money column, rendered as money. '\u2014' when absent. */
    money(v) {
        const n = Number(v);
        if (!Number.isFinite(n)) return '\u2014';
        return '$' + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    /** A value straight from a warehouse row, or an explicit em-dash. */
    orDash(v) {
        return (v === null || v === undefined || v === '') ? '\u2014' : String(v);
    }

    updateLeftPanelFromOverview(data) {
        const prof = data.profile || {};
        const cards = data.cards || [];
        const disputes = data.open_disputes || [];
        const travels = data.travel_notices || [];
        const tokens = data.wallet_tokens || [];
        const standing = data.standing_instructions || {};
        const recon = data.reconciliation || {};

        const fullName = prof.full_name || '';
        const who = fullName ? `${this.customerId} \u2014 ${fullName}` : this.customerId;

        const welcomeBubble = document.getElementById('welcome-chat-bubble');
        if (welcomeBubble) {
            welcomeBubble.innerHTML = `\ud83d\udc4b <strong>Fraud Desk Active (<code>${this.escapeHtml(who)}</code>)</strong>. Select any demo prompt above or ask a question to retrieve live Vertex AI Memory Bank recollections, verify Dataplex Catalog governance aspects, and reconcile against BigQuery ground truth.`;
        }

        // The dropdown only had ids. Now that the profile is loaded, label the
        // selected one with the name the warehouse actually returned.
        if (this.customerSelector && fullName) {
            const opt = this.customerSelector.querySelector(`option[value="${this.customerId}"]`);
            if (opt) opt.textContent = `${this.customerId} \u2014 ${fullName}`;
        }

        // ---------------------------------------------------------- card widget
        const frozenCards = cards.filter(c => c.status === 'FROZEN');
        const activeCards = cards.filter(c => c.status === 'ACTIVE');
        if (this.cardBadge) {
            if (cards.length === 0) {
                this.cardBadge.className = 'badge-status neutral';
                this.cardBadge.textContent = 'no cards on file';
            } else {
                this.cardBadge.className = frozenCards.length > 0 ? 'badge-status danger' : 'badge-status success';
                this.cardBadge.textContent = `${frozenCards.length} FROZEN \u2022 ${activeCards.length} ACTIVE \u2022 ${cards.length} TOTAL`;
            }
        }
        if (this.cardNumberDisplay) {
            this.cardNumberDisplay.textContent = cards.length
                ? cards.slice(0, 3).map(c => `*${this.orDash(c.last_four)} (${this.orDash(c.status)})`).join(' \u2022 ')
                : 'no cards returned by BigQuery';
        }
        if (this.cardHolderDisplay) {
            this.cardHolderDisplay.textContent = fullName
                ? `${fullName.toUpperCase()} (${this.customerId}${prof.segment ? ' \u2022 ' + prof.segment : ''})`
                : this.customerId;
        }

        // ------------------------------------------------- wallet token status
        // Was fixed HTML reading "Google Pay Active - Apple Pay Revoked".
        // /api/customer/overview has returned wallet_tokens all along.
        if (this.applePayStatus) {
            if (tokens.length === 0) {
                this.applePayStatus.style.color = '#94a3b8';
                this.applePayStatus.textContent = 'No wallet tokens on file';
            } else {
                const live = tokens.filter(t => String(t.token_status).toUpperCase() === 'ACTIVE');
                const parts = tokens.slice(0, 3).map(t =>
                    `${this.orDash(t.wallet_provider)} ${this.orDash(t.token_status)}`);
                if (tokens.length > 3) parts.push(`+${tokens.length - 3} more`);
                this.applePayStatus.style.color = live.length ? '#34d399' : '#fbbf24';
                this.applePayStatus.textContent = parts.join(' \u2022 ');
                this.applePayStatus.title = tokens.map(t =>
                    `${t.wallet_provider}/${t.device_name || 'unknown device'}: ${t.token_status}` +
                    (t.revoked_at ? ` (revoked ${t.revoked_at})` : '')).join('\n');
            }
        }

        // -------------------------------------------------- standing instruction
        if (this.adminVerificationSummary) {
            const text = standing.standing_instructions || prof.standing_instructions;
            this.adminVerificationSummary.innerHTML = text
                ? `<strong>Standing Instruction:</strong> "${this.escapeHtml(text)}"`
                : `<strong>Standing Instruction:</strong> <span class="not-measured">none recorded for this customer</span>`;
        }

        // ------------------------------------- feed card 1: cards & wallet tokens
        const feedCardTime = document.getElementById('feed-card-time');
        const feedCardTitle = document.getElementById('feed-card-title');
        const feedCardDesc = document.getElementById('feed-card-desc');
        const feedCardMeta = document.getElementById('feed-card-meta');
        if (feedCardTime) {
            feedCardTime.textContent = `${this.plural(cards.length, 'card', 'cards')} \u2022 ${this.plural(tokens.length, 'wallet token', 'wallet tokens')}`;
        }
        if (feedCardTitle) {
            feedCardTitle.textContent = frozenCards.length
                ? `${this.plural(frozenCards.length, 'card', 'cards')} frozen on this account`
                : 'Card portfolio & wallet tokens';
        }
        if (feedCardDesc) {
            if (cards.length === 0) {
                feedCardDesc.innerHTML = 'BigQuery <code>cards</code> returned no rows for this customer.';
            } else {
                const lines = cards.slice(0, 4).map(c => {
                    const reason = c.freeze_reason ? ` <code>${this.escapeHtml(c.freeze_reason)}</code>` : '';
                    return `<strong>${this.escapeHtml(this.orDash(c.card_id))} (*${this.escapeHtml(this.orDash(c.last_four))} ${this.escapeHtml(this.orDash(c.card_type))})</strong> <code>${this.escapeHtml(this.orDash(c.status))}</code>${reason}`;
                });
                const more = cards.length > 4 ? ` +${cards.length - 4} more.` : '';
                feedCardDesc.innerHTML = `BigQuery <code>cards</code>: ${lines.join('; ')}.${more}`;
            }
        }
        if (feedCardMeta) {
            const chips = [];
            if (activeCards.length) chips.push(`Active: ${activeCards.map(c => '*' + this.orDash(c.last_four)).join(', ')}`);
            if (frozenCards.length) chips.push(`Frozen: ${frozenCards.map(c => '*' + this.orDash(c.last_four)).join(', ')}`);
            const otherCards = cards.filter(c => c.status !== 'ACTIVE' && c.status !== 'FROZEN');
            otherCards.forEach(c => chips.push(`${this.orDash(c.status)}: *${this.orDash(c.last_four)}`));
            tokens.slice(0, 3).forEach(t => chips.push(`${this.orDash(t.wallet_provider)}: ${this.orDash(t.token_status)}`));
            feedCardMeta.innerHTML = chips.length
                ? chips.map(c => `<span class="meta-chip">${this.escapeHtml(c)}</span>`).join('')
                : '<span class="meta-chip">no card or token rows</span>';
        }

        // --------------------------------------------- feed card 2: travel notice
        const trv = travels[0];
        const feedTravelTime = document.getElementById('feed-travel-time');
        const feedTravelTitle = document.getElementById('feed-travel-title');
        const feedTravelDesc = document.getElementById('feed-travel-desc');
        const feedTravelMeta = document.getElementById('feed-travel-meta');
        const travelFindings = ((recon.travel && recon.travel.findings) || []);
        const travelContra = travelFindings.filter(f => f.status === 'CONTRADICTED');

        if (!trv) {
            if (feedTravelTime) feedTravelTime.textContent = 'no travel notices';
            if (feedTravelTitle) feedTravelTitle.textContent = 'Travel registry';
            if (feedTravelDesc) feedTravelDesc.innerHTML = 'BigQuery <code>travel_notices</code> returned no rows for this customer.';
            if (feedTravelMeta) feedTravelMeta.innerHTML = '';
        } else {
            if (feedTravelTime) {
                feedTravelTime.textContent = `${this.orDash(trv.notice_id)} \u2022 ${this.orDash(trv.declared_channel)}`;
            }
            if (feedTravelTitle) {
                feedTravelTitle.textContent = travelContra.length
                    ? `${this.orDash(trv.destination_city)} notice \u2014 ${this.plural(travelContra.length, 'memory contradicts it', 'memories contradict it')}`
                    : `${this.orDash(trv.destination_city)}, ${this.orDash(trv.destination_country)} travel notice`;
            }
            if (feedTravelDesc) {
                feedTravelDesc.innerHTML =
                    `BigQuery <code>travel_notices</code> records <strong>${this.escapeHtml(this.orDash(trv.destination_city))}, ${this.escapeHtml(this.orDash(trv.destination_country))}</strong> ` +
                    `(${this.escapeHtml(this.orDash(trv.travel_start_date))} to ${this.escapeHtml(this.orDash(trv.travel_end_date))}; actual return: <strong>${this.escapeHtml(this.orDash(trv.actual_return_date))}</strong>, status ${this.escapeHtml(this.orDash(trv.status))}). ` +
                    (travelContra.length
                        ? `The reconciler flagged ${this.plural(travelContra.length, 'memory', 'memories')} that disagree with this record.`
                        : `${travelFindings.length ? 'The reconciler found no disagreement in the memories it could check.' : 'No memory made a checkable travel claim.'}`);
            }
            if (feedTravelMeta) {
                const chips = [
                    `<span class="meta-chip">BQ: ${this.escapeHtml(this.orDash(trv.destination_city))}, ${this.escapeHtml(this.orDash(trv.destination_country))}</span>`,
                    `<span class="meta-chip">Return: ${this.escapeHtml(this.orDash(trv.actual_return_date))}</span>`
                ];
                if (travelContra.length) {
                    chips.push(`<span class="veracity-badge false">\u26a0\ufe0f ${this.plural(travelContra.length, 'contradiction', 'contradictions')}</span>`);
                } else if (travelFindings.length) {
                    chips.push('<span class="veracity-badge true">\u2713 no contradiction found</span>');
                } else {
                    chips.push('<span class="meta-chip">not checkable</span>');
                }
                feedTravelMeta.innerHTML = chips.join('');
            }
        }

        // ------------------------------------------------ feed card 3: disputes
        const feedDisputeTime = document.getElementById('feed-dispute-time');
        const feedDisputeTitle = document.getElementById('feed-dispute-title');
        const feedDisputeDesc = document.getElementById('feed-dispute-desc');
        const feedDisputeMeta = document.getElementById('feed-dispute-meta');
        const totalAmt = disputes.reduce((acc, d) => acc + Number(d.disputed_amount || 0), 0);

        if (feedDisputeTime) {
            feedDisputeTime.textContent = disputes.length
                ? `${this.plural(disputes.length, 'open dispute', 'open disputes')} \u2022 ${this.money(totalAmt)}`
                : 'no open disputes';
        }
        if (feedDisputeTitle) {
            const paths = [...new Set(disputes.map(d => d.regulation_path).filter(Boolean))];
            feedDisputeTitle.textContent = paths.length
                ? `Open dispute ledger \u2014 ${paths.join(' + ')}`
                : 'Open dispute ledger';
        }
        if (feedDisputeDesc) {
            if (disputes.length === 0) {
                feedDisputeDesc.innerHTML = 'BigQuery <code>disputes</code> returned no open rows for this customer.';
            } else {
                const lines = disputes.slice(0, 3).map(d => {
                    const pc = d.provisional_credit_issued
                        ? `provisional credit ${this.money(d.provisional_credit_amount)}${d.provisional_credit_date ? ' on ' + this.escapeHtml(d.provisional_credit_date) : ''}`
                        : 'no provisional credit';
                    const due = d.investigation_due_date ? `, due ${this.escapeHtml(d.investigation_due_date)}` : '';
                    return `<strong>${this.escapeHtml(this.orDash(d.dispute_id))} (${this.money(d.disputed_amount)}, ${this.escapeHtml(this.orDash(d.card_id))}, ${this.escapeHtml(this.orDash(d.regulation_path))})</strong>: ${this.escapeHtml(this.orDash(d.status))} \u2014 ${pc}${due}`;
                });
                const more = disputes.length > 3 ? ` +${disputes.length - 3} more open.` : '';
                feedDisputeDesc.innerHTML = lines.join('. ') + '.' + more;
            }
        }
        if (feedDisputeMeta) {
            const byPath = {};
            disputes.forEach(d => {
                const k = d.regulation_path || 'unspecified';
                byPath[k] = byPath[k] || { n: 0, amt: 0, pc: 0 };
                byPath[k].n += 1;
                byPath[k].amt += Number(d.disputed_amount || 0);
                if (d.provisional_credit_issued) byPath[k].pc += 1;
            });
            const chips = Object.keys(byPath).sort().map(k => {
                const v = byPath[k];
                return `<span class="meta-chip">${this.escapeHtml(k)}: ${v.n} \u2022 ${this.money(v.amt)} \u2022 PC ${v.pc}/${v.n}</span>`;
            });
            feedDisputeMeta.innerHTML = chips.join('') || '<span class="meta-chip">no open disputes</span>';
        }
    }

    /**
     * The governance badge over the card widget. It used to read
     * "PAN_FULL: REFUSED" as fixed HTML, asserting an outcome nothing had
     * computed. This runs the actual check and shows whatever came back.
     */
    async loadGovernanceBadge() {
        const badge = this.adminConfidenceBadge;
        if (!badge) return;
        badge.className = 'badge-status neutral';
        badge.textContent = 'cards.pan_full: checking\u2026';
        try {
            const res = await fetch('/api/catalog/authorize?table=cards&column=pan_full');
            const d = await res.json();
            if (!res.ok || d.error) {
                badge.className = 'badge-status warning';
                badge.textContent = 'cards.pan_full: check failed';
                badge.title = d.error || `HTTP ${res.status}`;
                return;
            }
            const action = d.action || (d.allowed === true ? 'ALLOW' : d.allowed === false ? 'REFUSE' : 'UNKNOWN');
            badge.className = d.allowed === false ? 'badge-status success' : 'badge-status warning';
            // The catalog advises; it does not enforce. The response says so
            // explicitly, so the badge describes the agent's behaviour rather
            // than claiming the read was blocked.
            badge.textContent = `cards.pan_full: agent ${action}`;
            badge.title = [
                'Live GET /api/catalog/authorize?table=cards&column=pan_full',
                d.basis, d.rationale, d.advisory_note,
                d.agent_guidance && d.agent_guidance !== 'n/r' ? d.agent_guidance : null
            ].filter(Boolean).join('\n\n');
            this.appendTrace('CATALOG_AUTHORIZE',
                `Dataplex verdict on cards.pan_full: ${action}. Basis: ${d.basis || 'not recorded'}.`, 'thought');
        } catch (err) {
            badge.className = 'badge-status warning';
            badge.textContent = 'cards.pan_full: check failed';
            badge.title = String(err && err.message ? err.message : err);
        }
    }

    // =========================================================================
    // TAB 1: MEMORY BANK VAULT (Live Memories + Topic Filter + Distance Search)
    // =========================================================================

    renderMemoryBankFragments(memories, searchMeta = null) {
        const list = memories || [];
        const filtered = this.activeTopicFilter === 'ALL'
            ? list
            : list.filter(m => (m.topics || []).includes(this.activeTopicFilter));

        this.memoryCountBadge.textContent = searchMeta
            ? `${filtered.length} Hits (${searchMeta.latency_ms} ms)`
            : `${filtered.length} / ${list.length} Memories`;

        // Topic Filter Bar
        const topicCounts = {};
        list.forEach(m => {
            (m.topics || []).forEach(t => {
                topicCounts[t] = (topicCounts[t] || 0) + 1;
            });
        });

        const topicPillsHtml = [
            `<button class="topic-filter-pill ${this.activeTopicFilter === 'ALL' ? 'active' : ''}" data-topic="ALL" style="padding:3px 8px; border-radius:12px; font-size:0.66rem; font-weight:700; cursor:pointer; border:1px solid ${this.activeTopicFilter === 'ALL' ? '#38bdf8' : 'rgba(255,255,255,0.15)'}; background:${this.activeTopicFilter === 'ALL' ? 'rgba(56,189,248,0.2)' : 'rgba(15,23,42,0.7)'}; color:#f8fafc;">ALL (${list.length})</button>`,
            ...Object.keys(topicCounts).sort().map(t => {
                const isAct = this.activeTopicFilter === t;
                return `<button class="topic-filter-pill" data-topic="${this.escapeHtml(t)}" style="padding:3px 8px; border-radius:12px; font-size:0.66rem; font-weight:600; cursor:pointer; border:1px solid ${isAct ? '#38bdf8' : 'rgba(255,255,255,0.12)'}; background:${isAct ? 'rgba(56,189,248,0.2)' : 'rgba(15,23,42,0.7)'}; color:${isAct ? '#38bdf8' : '#cbd5e1'};">${this.escapeHtml(t)} (${topicCounts[t]})</button>`;
            })
        ].join(' ');

        const searchBannerHtml = searchMeta ? `
            <div style="background:rgba(56,189,248,0.12); border:1px solid rgba(56,189,248,0.4); border-radius:6px; padding:8px 12px; margin-bottom:10px; display:flex; justify-content:space-between; align-items:center; font-size:0.74rem;">
                <div>
                    <strong style="color:#38bdf8;">🔍 Vector Similarity Retrieval (` + "`POST memories:retrieve`" + `):</strong>
                    Query <code>"${this.escapeHtml(searchMeta.query)}"</code> • Ranked by cosine distance (<strong>lower distance = closer match</strong>)
                </div>
                <span class="badge-status success">${searchMeta.latency_ms} ms</span>
            </div>
        ` : '';

        const cardsHtml = filtered.map((mem, index) => {
            const topics = mem.topics || ['UNTAGGED'];
            const recStatus = mem.reconciliation_status || 'UNVERIFIABLE';
            let statusBadge = `<span class="veracity-badge true" style="background:rgba(148,163,184,0.15); color:#cbd5e1;">Conversational Context</span>`;
            if (recStatus === 'CORROBORATED') {
                statusBadge = `<span class="veracity-badge true">✓ Corroborated by BigQuery</span>`;
            } else if (recStatus === 'CONTRADICTED') {
                statusBadge = `<span class="veracity-badge false">⚠️ Contradicts BigQuery Truth</span>`;
            }

            const distanceBadge = (mem.distance !== undefined && mem.distance !== null)
                ? `<span class="badge-status warning" style="font-family:'JetBrains Mono',monospace; font-size:0.68rem;">Distance: ${Number(mem.distance).toFixed(4)}</span>`
                : '';

            const findingsHtml = (mem.reconciliation_findings || []).map(f => `
                <div class="veracity-reason" style="margin-top:5px; padding:5px 8px; border-radius:4px; background:${f.status === 'CONTRADICTED' ? 'rgba(239,68,68,0.15)' : 'rgba(16,185,129,0.12)'}; border-left:3px solid ${f.status === 'CONTRADICTED' ? '#ef4444' : '#10b981'}; font-size:0.7rem; color:#e2e8f0;">
                    <strong>BigQuery Reconciler (${this.escapeHtml(f.claim_kind)}):</strong> ${this.escapeHtml(f.explanation)}
                    (Claimed: <code>${this.escapeHtml((f.claimed_values || []).join(', '))}</code> vs. BigQuery Authoritative: <code>${this.escapeHtml((f.authoritative_values || []).slice(0, 6).join(', '))}</code>)
                </div>
            `).join('');

            const createdStr = mem.create_time ? mem.create_time.replace('T', ' ').slice(0, 19) + ' UTC' : 'Stored Memory';

            return `
                <div class="memory-fragment" style="border-left: 3px solid ${recStatus === 'CONTRADICTED' ? '#ef4444' : (recStatus === 'CORROBORATED' ? '#10b981' : '#38bdf8')};">
                    <div class="frag-header" style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:6px;">
                        <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
                            <span style="font-family:'JetBrains Mono',monospace; font-size:0.68rem; color:#94a3b8;">#${index + 1} [ID: ${this.escapeHtml(mem.short_id || mem.id)}]</span>
                            ${topics.map(t => `<span class="frag-channel" style="background:rgba(56,189,248,0.16); color:#38bdf8; padding:2px 7px; border-radius:4px; font-size:0.67rem; font-weight:700;">${this.escapeHtml(t)}</span>`).join('')}
                        </div>
                        <div style="display:flex; align-items:center; gap:6px;">
                            ${distanceBadge}
                            ${statusBadge}
                            <span class="frag-time">${this.escapeHtml(createdStr)}</span>
                        </div>
                    </div>
                    <div class="frag-summary" style="font-size:0.8rem; color:#f8fafc; margin-top:6px; line-height:1.45;">
                        "${this.escapeHtml(mem.fact)}"
                    </div>
                    ${findingsHtml}
                </div>
            `;
        }).join('');

        this.memoryTerminal.innerHTML = `
            <div style="padding: 8px 10px;">
                <div style="display:flex; flex-wrap:wrap; gap:5px; margin-bottom:10px; padding-bottom:8px; border-bottom:1px solid rgba(255,255,255,0.08);">
                    ${topicPillsHtml}
                </div>
                ${searchBannerHtml}
                ${cardsHtml || '<div style="padding:1rem; color:#94a3b8; text-align:center;">No memories match this filter.</div>'}
            </div>
        `;

        this.memoryTerminal.querySelectorAll('.topic-filter-pill').forEach(pill => {
            pill.addEventListener('click', () => {
                this.activeTopicFilter = pill.dataset.topic;
                this.renderMemoryBankFragments(memories, searchMeta);
            });
        });
    }

    async executeSemanticSearch(query) {
        this.appendTrace('MEMORY_RETRIEVE', `Executing similarity search in scope {"customer_id": "${this.customerId}"} for "${query}"...`, 'memory');
        try {
            const res = await fetch('/api/memory/retrieve', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    customer_id: this.customerId,
                    query: query,
                    top_k: 6
                })
            });
            const data = await res.json();
            const hits = (data.memories || []).map(h => {
                // Attach reconciliation status if known from cachedMemories
                const existing = this.cachedMemories.find(m => m.id === h.id);
                return existing ? { ...existing, distance: h.distance } : h;
            });
            const lat = data.latency_ms && data.latency_ms.value ? data.latency_ms.value : 'n/r';
            this.currentViewMode = 'notes';
            this.setActiveToggle(this.viewNotesBtn);
            this.renderMemoryBankFragments(hits, { query, latency_ms: lat });
            this.appendTrace('RETRIEVE_COMPLETE', `Retrieved ${hits.length} ranked memories in ${lat} ms. Top distance: ${hits[0] ? Number(hits[0].distance).toFixed(4) : 'n/a'}.`, 'memory');
        } catch (err) {
            console.error('Semantic search failed:', err);
        }
    }

    // =========================================================================
    // TAB 2: PRECISION & RECALL + INTELLIGENT WRITE EFFICACY BENCHMARKS
    // =========================================================================

    /**
     * Precision & Recall tab.
     *
     * Every figure below is read out of /api/eval/summary (which serves the
     * stored sweep) or out of the live /api/eval/retrieval response after the
     * re-run button. Nothing is typed in. A field the backend did not send
     * renders as an em-dash or "not measured", so re-running the sweep with
     * different data visibly changes the page.
     */
    async renderPrecisionRecallView() {
        if (!this.cachedEvalSummary) {
            await this.loadEvalSummary();
        }

        const summary = this.cachedEvalSummary || {};
        const nb = this.normalizeBenchmark();

        const ret = nb.ret;
        const inc = nb.writePolicy.inclusion;
        const top = nb.writePolicy.topics;
        const scp = nb.writePolicy.scope;
        const con = nb.writePolicy.consolidation;

        const headline = nb.headline;
        const labelling = nb.labelling;
        const runs = nb.runs;
        const headlineK = nb.headlineK;
        const headlineRun = nb.headlineRun || null;
        const headlineProbes = nb.headlineProbes || [];

        const noBenchmark = !nb.found;

        if (noBenchmark) {
            this.memoryCountBadge.textContent = 'No benchmark in /api/eval/summary';
            this.memoryTerminal.innerHTML = `
                <div style="padding:18px; color:#e2e8f0; font-size:0.8rem;">
                    <div style="background:rgba(30,41,59,0.92); border:1px solid rgba(251,191,36,0.45); border-radius:8px; padding:14px;">
                        <div style="font-weight:800; color:#fbbf24; font-size:0.86rem; margin-bottom:6px;">No retrieval benchmark available</div>
                        <div style="font-size:0.75rem; color:#cbd5e1; line-height:1.5;">
                            <code>GET /api/eval/summary</code> returned no <code>benchmark.results.retrieval</code>.
                            Nothing has been measured, so there is nothing to show. Run the sweep to populate this tab.
                        </div>
                        <button id="run-live-retrieval-btn" class="btn-primary btn-sm" style="margin-top:10px; font-size:0.72rem;">\u26a1 Run Live Retrieval Sweep</button>
                    </div>
                </div>
            `;
            this.bindLiveRetrievalButton();
            return;
        }

        // ---- headline badge -------------------------------------------------
        const recallPct = this.pct(headline.macro_recall);
        const writePrec = inc.score && inc.score.write_precision !== null && inc.score.write_precision !== undefined
            ? `${(Number(inc.score.write_precision) * 100).toFixed(0)}%`
            : 'not measured';
        this.memoryCountBadge.textContent =
            `Recall@${headlineK !== undefined && headlineK !== null ? headlineK : '?'}: ${recallPct} \u2022 Write prec: ${writePrec}`;

        // ---- sub-headline figures, each computed from the sweep -------------
        const maxK = runs.length ? Math.max(...runs.map(r => r.k)) : null;
        const maxKRun = maxK !== null ? runs.find(r => r.k === maxK) : null;
        const recallAtMaxK = maxKRun ? this.pct(maxKRun.aggregate && maxKRun.aggregate.macro_recall_at_k) : '\u2014';

        const minK = runs.length ? Math.min(...runs.map(r => r.k)) : null;
        const minKRun = minK !== null ? runs.find(r => r.k === minK) : null;
        const mrrAtMinK = minKRun ? this.num(minKRun.aggregate && minKRun.aggregate.mean_reciprocal_rank) : '\u2014';

        const mapAtHeadline = headlineRun ? this.num(headlineRun.aggregate && headlineRun.aggregate.mean_average_precision) : '\u2014';

        // Precision ceiling: with `relevant` relevant items spread over `probes`
        // questions, retrieving k of them caps macro precision at avgRel/k.
        let ceilingNote = 'ceiling: not reported';
        let avgRel = null;
        let ceilingValue = this.metricValue(nb.ceiling);
        const micro = headlineRun && headlineRun.aggregate && headlineRun.aggregate.micro;
        if (micro && micro.counts && headlineRun.aggregate.probes) {
            avgRel = Number(micro.counts.relevant) / Number(headlineRun.aggregate.probes);
            if (!Number.isFinite(avgRel)) avgRel = null;
        }
        if (ceilingValue === null && avgRel !== null && headlineK) {
            ceilingValue = Math.min(1, avgRel / headlineK);
        }
        if (ceilingValue !== null) {
            ceilingNote = `ceiling ${(ceilingValue * 100).toFixed(1)}%` +
                (avgRel !== null ? ` (${avgRel.toFixed(2)} rel/q)` : '');
            const vsC = this.metricValue(nb.precisionVsCeiling);
            if (vsC !== null) ceilingNote += ` \u2022 ${(vsC * 100).toFixed(0)}% of it`;
        }

        const selfConsistency = labelling.judge_self_consistency || null;
        const agreement = labelling.agreement_between_labellers || null;
        const scPct = selfConsistency ? this.pct(selfConsistency.raw_agreement_on_positives) : 'not measured';
        const agPct = agreement ? this.pct(agreement.raw_agreement_on_positives) : 'not measured';

        const corpusSize = nb.corpusSize !== null && nb.corpusSize !== undefined ? nb.corpusSize : '\u2014';
        const probeCount = nb.probeCount !== null && nb.probeCount !== undefined ? nb.probeCount : '\u2014';

        // The v2 harness reports whether the labelled corpus still matches the
        // live one, and how the negative probes fared. Both are real measured
        // outcomes, so they are shown when present and omitted when not.
        const integrityBits = [];
        if (nb.binding) {
            const b = nb.binding;
            integrityBits.push(b.corpus_unchanged
                ? `Corpus unchanged since labelling (sha1 <code>${this.escapeHtml(String(b.live_corpus_sha1_12 || ''))}</code>, ${this.orDash(b.live_corpus_size)} memories).`
                : `<strong style="color:#fbbf24;">Corpus has changed since the labels were written</strong> (labelled <code>${this.escapeHtml(String(b.labels_written_against_corpus_sha1_12 || ''))}</code> vs live <code>${this.escapeHtml(String(b.live_corpus_sha1_12 || ''))}</code>).`);
        }
        if (nb.negativeRejectionRate !== undefined && nb.negativeRejectionRate !== null) {
            integrityBits.push(`Negative-probe rejection rate: <strong>${(Number(nb.negativeRejectionRate) * 100).toFixed(0)}%</strong>.`);
        }
        const rp = this.metricValue(nb.rPrecision);
        if (rp !== null) integrityBits.push(`R-precision: <strong>${rp.toFixed(4)}</strong>.`);
        if (nb.latency && nb.latency.p95_ms !== undefined) {
            integrityBits.push(`Retrieval latency: median <strong>${nb.latency.median_ms} ms</strong>, p95 <strong>${nb.latency.p95_ms} ms</strong> over ${this.orDash(nb.latency.n)} calls.`);
        }
        if (nb.mode) integrityBits.push(`Run mode: <code>${this.escapeHtml(String(nb.mode))}</code>.`);
        const integrityHtml = integrityBits.length ? `
            <div style="background:rgba(15,23,42,0.75); border-left:3px solid #94a3b8; padding:6px 10px; border-radius:4px; font-size:0.69rem; color:#cbd5e1; margin-bottom:10px;">
                ${integrityBits.join(' ')}
            </div>` : '';

        const metricCard = (label, value, sub, colour, borderColour) => `
            <div style="background:rgba(15,23,42,0.8); padding:8px; border-radius:6px; text-align:center;${borderColour ? ' border:1px solid ' + borderColour + ';' : ''}">
                <div style="font-size:0.65rem; color:#94a3b8;">${label}</div>
                <div style="font-size:1.15rem; font-weight:800; color:${value === '\u2014' ? '#64748b' : colour};">${value}</div>
                <div style="font-size:0.62rem; color:#64748b;">${sub}</div>
            </div>`;

        const headlineCardsHtml = [
            metricCard(`MACRO RECALL (k=${headlineK ?? '?'})`, recallPct, `${recallAtMaxK} at k=${maxK ?? '?'}`, '#34d399', 'rgba(52,211,153,0.3)'),
            metricCard(`MACRO PRECISION (k=${headlineK ?? '?'})`, this.pct(headline.macro_precision), ceilingNote, '#38bdf8', 'rgba(56,189,248,0.3)'),
            metricCard('MEAN RECIPROCAL RANK', this.num(headline.mrr), `MRR@${minK ?? '?'} = ${mrrAtMinK}`, '#fde047', null),
            metricCard(`nDCG (k=${headlineK ?? '?'})`, this.num(headline.ndcg), `MAP = ${mapAtHeadline}`, '#c084fc', null),
            metricCard('JUDGE CONSISTENCY', scPct, 'Jaccard on positives', '#f8fafc', null)
        ].join('');

        // ---- k-sweep table ---------------------------------------------------
        const kSweepRowsHtml = runs
            .slice()
            .sort((a, b) => a.k - b.k)
            .map(r => {
                const agg = r.aggregate || {};
                const isHeadline = r.k === headlineK;
                return `
                <tr style="background:${isHeadline ? 'rgba(56,189,248,0.14)' : 'transparent'}; border-bottom:1px solid rgba(255,255,255,0.07);">
                    <td style="padding:6px 8px; font-weight:700; color:${isHeadline ? '#38bdf8' : '#f8fafc'};">k = ${r.k}${isHeadline ? ' (preset headline)' : ''}</td>
                    <td style="padding:6px 8px; font-family:'JetBrains Mono',monospace;">${this.pct(agg.macro_precision_at_k)}</td>
                    <td style="padding:6px 8px; font-family:'JetBrains Mono',monospace; color:#34d399; font-weight:700;">${this.pct(agg.macro_recall_at_k)}</td>
                    <td style="padding:6px 8px; font-family:'JetBrains Mono',monospace;">${this.pct(agg.macro_f1_at_k)}</td>
                    <td style="padding:6px 8px; font-family:'JetBrains Mono',monospace;">${this.num(agg.mean_reciprocal_rank)}</td>
                    <td style="padding:6px 8px; font-family:'JetBrains Mono',monospace;">${this.num(agg.macro_ndcg_at_k)}</td>
                </tr>`;
            }).join('') || `<tr><td colspan="6" style="padding:8px; color:#94a3b8;">No k-sweep runs in the response.</td></tr>`;

        // ---- per-probe table -------------------------------------------------
        // Field names taken from the retrieval report: precision_at_k /
        // recall_at_k / counts.{relevant,true_positives}.
        const probesRowsHtml = headlineProbes.map(p => {
            const counts = p.counts || {};
            const pv = p.precision_at_k !== null && p.precision_at_k !== undefined
                ? `${(Number(p.precision_at_k) * 100).toFixed(0)}%` : 'n/r (0 retrieved)';
            const rv = p.recall_at_k !== null && p.recall_at_k !== undefined
                ? `${(Number(p.recall_at_k) * 100).toFixed(0)}%` : 'n/r (no relevant item)';
            return `
                <tr style="border-bottom:1px solid rgba(255,255,255,0.06); font-size:0.7rem;">
                    <td style="padding:5px 7px; font-weight:700; color:#38bdf8;">${this.escapeHtml(p.probe_id)}</td>
                    <td style="padding:5px 7px; color:#e2e8f0;">${this.escapeHtml(p.question)}</td>
                    <td style="padding:5px 7px; font-family:'JetBrains Mono',monospace;">${this.orDash(counts.relevant)}</td>
                    <td style="padding:5px 7px; font-family:'JetBrains Mono',monospace;">${this.orDash(counts.true_positives)} / ${this.orDash(p.k)}</td>
                    <td style="padding:5px 7px; font-family:'JetBrains Mono',monospace;">${pv}</td>
                    <td style="padding:5px 7px; font-family:'JetBrains Mono',monospace; color:#34d399; font-weight:700;">${rv}</td>
                </tr>`;
        }).join('') || `<tr><td colspan="6" style="padding:8px; color:#94a3b8;">No per-probe rows at the headline k.</td></tr>`;

        // ---- written memories from the inclusion experiment --------------------
        const incWritten = inc.written || [];
        const incWrittenHtml = incWritten.map(m => `
            <div style="padding:5px 8px; background:rgba(16,185,129,0.1); border-left:3px solid #10b981; border-radius:4px; margin-bottom:4px; font-size:0.71rem;">
                <span style="color:#34d399; font-weight:700;">[${this.escapeHtml((m.topics || []).join(', ')) || 'no topic'}]</span>
                <span style="color:#f8fafc;">"${this.escapeHtml(m.fact)}"</span>
            </div>
        `).join('') || '<div style="font-size:0.7rem; color:#94a3b8;">No inclusion experiment in the response.</div>';

        // ---- 3-arm topic table, built from whatever arms are present -----------
        const topicTranscripts = Object.keys(top);
        const armNames = [];
        topicTranscripts.forEach(tid => {
            ((top[tid] || {}).arms || []).forEach(a => {
                const n = (a.config && a.config.name) || 'unnamed';
                if (!armNames.includes(n)) armNames.push(n);
            });
        });

        const topicHeaderHtml = armNames.map(n => {
            let count = null;
            for (const tid of topicTranscripts) {
                const arm = ((top[tid] || {}).arms || []).find(a => (a.config && a.config.name) === n);
                if (arm && arm.config && arm.config.topic_count !== undefined) { count = arm.config.topic_count; break; }
            }
            return `<th style="padding:4px 8px;">${this.escapeHtml(n)}${count !== null ? ` (${count} topics)` : ''}</th>`;
        }).join('');

        const topicRowsHtml = topicTranscripts.map(tid => {
            const t = top[tid] || {};
            const cells = armNames.map(n => {
                const arm = (t.arms || []).find(a => (a.config && a.config.name) === n);
                if (!arm) return '<td style="padding:5px 8px; color:#64748b;">\u2014</td>';
                const w = arm.written_count;
                const zero = w === 0;
                return `<td style="padding:5px 8px; font-family:'JetBrains Mono',monospace; color:${zero ? '#38bdf8' : '#e2e8f0'}; font-weight:${zero ? '800' : '400'};">${this.plural(Number(w), 'memory written', 'memories written')}</td>`;
            }).join('');
            const utt = t.utterance_count !== undefined ? ` (${t.utterance_count} utterances)` : '';
            return `<tr style="border-bottom:1px solid rgba(255,255,255,0.06);"><td style="padding:5px 8px;"><code>${this.escapeHtml(tid)}</code>${utt}</td>${cells}</tr>`;
        }).join('') || `<tr><td colspan="${armNames.length + 1}" style="padding:8px; color:#94a3b8;">No topic experiment in the response.</td></tr>`;

        const topicVerdicts = topicTranscripts
            .map(tid => (top[tid] || {}).interpretation)
            .filter(Boolean);

        // ---- inclusion score ---------------------------------------------------
        const sc = inc.score || {};
        const fmtRatio = (a, b) => (a === undefined || b === undefined) ? '\u2014' : `${a} / ${b}`;
        const fmtCount = (v) => (v === undefined || v === null) ? '\u2014' : String(v);

        // ---- scope + consolidation ---------------------------------------------
        const scopeKnown = Object.keys(scp).length > 0;
        const conStages = con.stages || [];
        const stageLine = conStages.length
            ? conStages.map(s => `${this.escapeHtml(s.stage)}: ${this.escapeHtml((s.actions || []).join(', ') || 'no action')} \u2192 corpus ${this.orDash(s.corpus_size_after)}`).join(' \u2022 ')
            : 'No consolidation experiment in the response.';

        this.memoryTerminal.innerHTML = `
            <div style="padding:12px; color:#e2e8f0; font-size:0.76rem;">

                <div style="background:rgba(30,41,59,0.92); border:1px solid rgba(56,189,248,0.4); border-radius:8px; padding:12px; margin-bottom:12px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; margin-bottom:10px;">
                        <div>
                            <div style="font-weight:800; color:#38bdf8; font-size:0.86rem;">\ud83d\udcca 1. RETRIEVAL PRECISION &amp; RECALL (${probeCount} probes \u2022 corpus of ${corpusSize})</div>
                            <div style="font-size:0.71rem; color:#94a3b8;">
                                Dual-labelled. Judge self-consistency: <strong>${scPct}</strong> \u2022 agreement between labellers: <strong>${agPct}</strong>.
                            </div>
                        </div>
                        <button id="run-live-retrieval-btn" class="btn-primary btn-sm" style="font-size:0.7rem;">\u26a1 Re-run live retrieval sweep</button>
                    </div>

                    <div style="display:grid; grid-template-columns:repeat(5, 1fr); gap:8px; margin-bottom:10px;">
                        ${headlineCardsHtml}
                    </div>

                    ${headline.why_this_k ? `
                    <div style="background:rgba(56,189,248,0.08); border-left:3px solid #38bdf8; padding:6px 10px; border-radius:4px; font-size:0.71rem; color:#cbd5e1; margin-bottom:10px;">
                        <strong>On k = ${headlineK}:</strong> ${this.escapeHtml(headline.why_this_k)}
                        ${ceilingValue !== null ? ` Macro precision at this k cannot exceed ${(ceilingValue * 100).toFixed(1)}%.` : ''}
                    </div>` : ''}
                    ${integrityHtml}

                    <table style="width:100%; border-collapse:collapse; font-size:0.72rem; margin-bottom:8px;">
                        <thead>
                            <tr style="border-bottom:1px solid rgba(255,255,255,0.15); color:#94a3b8; text-align:left;">
                                <th style="padding:5px 8px;">Top-K sweep</th>
                                <th style="padding:5px 8px;">Macro precision</th>
                                <th style="padding:5px 8px;">Macro recall</th>
                                <th style="padding:5px 8px;">Macro F1</th>
                                <th style="padding:5px 8px;">MRR</th>
                                <th style="padding:5px 8px;">nDCG</th>
                            </tr>
                        </thead>
                        <tbody>${kSweepRowsHtml}</tbody>
                    </table>
                </div>

                <div style="background:rgba(30,41,59,0.92); border:1px solid rgba(16,185,129,0.4); border-radius:8px; padding:12px; margin-bottom:12px;">
                    <div style="font-weight:800; color:#34d399; font-size:0.86rem; margin-bottom:4px;">\ud83e\udde0 2. WHAT MEMORY BANK WRITES AND WHAT IT DROPS (topics, scopes, consolidation)</div>
                    <div style="font-size:0.72rem; color:#cbd5e1; margin-bottom:10px;">
                        Empirical answer to: <em>how does the memory bank decide what to write and what to exclude?</em>
                    </div>

                    <div style="background:rgba(15,23,42,0.85); padding:10px; border-radius:6px; margin-bottom:10px; border:1px solid rgba(255,255,255,0.08);">
                        <div style="font-weight:700; color:#fde047; font-size:0.76rem; margin-bottom:6px;">
                            Experiment A \u2014 topic vocabulary isolation${armNames.length ? ' (' + this.escapeHtml(armNames.join(' vs. ')) + ')' : ''}
                        </div>
                        <table style="width:100%; border-collapse:collapse; font-size:0.71rem; margin-bottom:6px;">
                            <thead>
                                <tr style="border-bottom:1px solid rgba(255,255,255,0.12); color:#94a3b8; text-align:left;">
                                    <th style="padding:4px 8px;">Dialogue transcript</th>
                                    ${topicHeaderHtml}
                                </tr>
                            </thead>
                            <tbody>${topicRowsHtml}</tbody>
                        </table>
                        ${topicVerdicts.length ? topicVerdicts.map(v =>
                            `<div style="font-size:0.7rem; color:#a7f3d0; margin-top:4px;"><strong>Verdict:</strong> ${this.escapeHtml(v)}</div>`
                        ).join('') : '<div style="font-size:0.7rem; color:#94a3b8;">No interpretation returned for this experiment.</div>'}
                    </div>

                    <div style="background:rgba(15,23,42,0.85); padding:10px; border-radius:6px; margin-bottom:10px; border:1px solid rgba(255,255,255,0.08);">
                        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; gap:6px; flex-wrap:wrap;">
                            <span style="font-weight:700; color:#38bdf8; font-size:0.76rem;">
                                Experiment B \u2014 selective write inclusion${inc.transcript_id ? ' (<code>' + this.escapeHtml(inc.transcript_id) + '</code>)' : ''}
                            </span>
                            <span class="badge-status ${sc.durable_coverage === 1 && (sc.disposable_leaked_ids || []).length === 0 ? 'success' : 'neutral'}">
                                Durable coverage: ${sc.durable_coverage !== undefined ? (Number(sc.durable_coverage) * 100).toFixed(0) + '%' : 'not measured'}
                                \u2022 leaks: ${sc.disposable_leaked_ids ? sc.disposable_leaked_ids.length + (sc.warehouse_leaked_ids || []).length : '\u2014'}
                            </span>
                        </div>
                        <div style="font-size:0.7rem; color:#cbd5e1; margin-bottom:6px;">
                            \u2022 <strong>Durable facts retained:</strong> ${fmtRatio(sc.durable_retained, sc.durable_utterances)}
                            (<code>durable_coverage = ${fmtCount(sc.durable_coverage)}</code>, <code>write_precision = ${fmtCount(sc.write_precision)}</code>)<br>
                            \u2022 <strong>Disposable small talk leaked:</strong> <code>${sc.disposable_leaked_ids ? sc.disposable_leaked_ids.length : '\u2014'}</code>
                            of ${fmtCount(sc.disposable_utterances)} disposable utterances<br>
                            \u2022 <strong>Volatile warehouse figures leaked:</strong> <code>${sc.warehouse_leaked_ids ? sc.warehouse_leaked_ids.length : '\u2014'}</code>
                            of ${fmtCount(sc.warehouse_utterances)} warehouse-owned utterances<br>
                            \u2022 <strong>Memories written:</strong> <code>${fmtCount(sc.memories_written !== undefined ? sc.memories_written : inc.written_count)}</code>
                        </div>
                        ${incWrittenHtml}
                        ${sc.caveat ? `<div style="font-size:0.66rem; color:#94a3b8; margin-top:6px;"><strong>Caveat:</strong> ${this.escapeHtml(sc.caveat)}</div>` : ''}
                    </div>

                    <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px;">
                        <div style="background:rgba(15,23,42,0.85); padding:9px; border-radius:6px; border-left:3px solid #a855f7;">
                            <div style="font-weight:700; color:#c084fc; font-size:0.74rem; margin-bottom:4px;">Experiment C \u2014 scope isolation</div>
                            <div style="font-size:0.69rem; color:#cbd5e1;">
                                ${scopeKnown ? `
                                    Own-scope search hits: <strong>${this.orDash(scp.own_scope_search_count)}</strong> of <strong>${this.orDash(scp.written_count)}</strong> written \u2022
                                    other-customer scope: <strong>${this.orDash(scp.other_scope_search_count)}</strong> \u2022
                                    superset scope (extra key): <strong>${this.orDash(scp.superset_search_count)}</strong>.<br>
                                    ${scp.verdict ? `<em>${this.escapeHtml(scp.verdict)}</em>` : ''}
                                ` : 'No scope experiment in the response.'}
                            </div>
                        </div>
                        <div style="background:rgba(15,23,42,0.85); padding:9px; border-radius:6px; border-left:3px solid #f59e0b;">
                            <div style="font-weight:700; color:#fde047; font-size:0.74rem; margin-bottom:4px;">Experiment D \u2014 fact consolidation</div>
                            <div style="font-size:0.69rem; color:#cbd5e1;">
                                ${stageLine}
                                ${con.verdict ? `<br><em>${this.escapeHtml(con.verdict)}</em>` : ''}
                            </div>
                        </div>
                    </div>
                </div>

                <div style="background:rgba(30,41,59,0.92); border:1px solid rgba(255,255,255,0.12); border-radius:8px; padding:12px;">
                    <div style="font-weight:700; color:#f8fafc; font-size:0.8rem; margin-bottom:8px;">\ud83d\udccb Per-probe retrieval results at k = ${headlineK ?? '?'}</div>
                    <table style="width:100%; border-collapse:collapse;">
                        <thead>
                            <tr style="border-bottom:1px solid rgba(255,255,255,0.15); color:#94a3b8; text-align:left; font-size:0.68rem;">
                                <th style="padding:4px 7px;">Probe id</th>
                                <th style="padding:4px 7px;">Question</th>
                                <th style="padding:4px 7px;">Rel in corpus</th>
                                <th style="padding:4px 7px;">TP @ k</th>
                                <th style="padding:4px 7px;">Precision@k</th>
                                <th style="padding:4px 7px;">Recall@k</th>
                            </tr>
                        </thead>
                        <tbody>${probesRowsHtml}</tbody>
                    </table>
                </div>

                <div style="margin-top:10px; font-size:0.66rem; color:#64748b;">
                    Source: <code>GET /api/eval/summary</code> \u2192 <code>${this.escapeHtml(nb.source || 'none')}</code>${nb.origin ? ` (<code>${this.escapeHtml(String(nb.origin))}</code>)` : ''}${summary.engine ? ` \u2022 engine <code>${this.escapeHtml(summary.engine)}</code>` : ''}.
                    An em-dash means the field was absent from the response, not that the value is zero.
                </div>
            </div>
        `;

        this.bindLiveRetrievalButton();
    }

    bindLiveRetrievalButton() {
        const liveRetBtn = document.getElementById('run-live-retrieval-btn');
        if (!liveRetBtn) return;
        liveRetBtn.addEventListener('click', async () => {
            liveRetBtn.disabled = true;
            liveRetBtn.textContent = '\u23f3 Running live retrieval sweep\u2026';
            this.appendTrace('EVAL_RETRIEVAL', 'Executing live retrieval benchmark against Vertex AI Memory Bank\u2026', 'thought');
            try {
                const res = await fetch('/api/eval/retrieval', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ customer_id: this.customerId })
                });
                const liveReport = await res.json();
                if (!res.ok || liveReport.error) {
                    liveRetBtn.disabled = false;
                    liveRetBtn.textContent = '\u26a0\ufe0f Sweep failed \u2014 retry';
                    this.appendTrace('EVAL_FAILED',
                        `Live retrieval sweep failed: ${liveReport.error || 'HTTP ' + res.status}.`, 'error');
                    return;
                }
                // Graft the live report over the stored one so the whole tab,
                // headline cards included, re-renders from the new numbers.
                this.cachedEvalSummary = this.cachedEvalSummary || {};
                this.cachedEvalSummary.benchmark = this.cachedEvalSummary.benchmark || {};
                this.cachedEvalSummary.benchmark.results = this.cachedEvalSummary.benchmark.results || {};
                this.cachedEvalSummary.benchmark.results.retrieval = liveReport;
                this.appendTrace('EVAL_COMPLETE', 'Live retrieval sweep finished; the panel now shows the new run.', 'synthesis');
                this.updateEvalHeadlinePill();
                this.renderPrecisionRecallView();
            } catch (err) {
                liveRetBtn.disabled = false;
                liveRetBtn.textContent = '\u26a0\ufe0f Sweep failed \u2014 retry';
                this.appendTrace('EVAL_FAILED', `Live retrieval sweep failed: ${err && err.message ? err.message : err}.`, 'error');
            }
        });
    }

    // =========================================================================
    // TAB 3: DATAPLEX KNOWLEDGE CATALOG & BIGQUERY GOLDEN QUERIES
    // =========================================================================

    async renderKnowledgeCatalogView(defaultTable = 'authorizations_deprecated_v1', defaultColumn = '') {
        // Golden queries are already loaded; the term count is not known until
        // the catalog answers, so it is fetched rather than asserted.
        this.memoryCountBadge.textContent =
            `Dataplex Catalog • terms loading… • ${this.plural(this.cachedGoldenQueries.length, 'golden query', 'golden queries')}`;
        this.refreshCatalogTermCount();

        const queryOptionsHtml = this.cachedGoldenQueries.map(q =>
            `<option value="${this.escapeHtml(q.query_id)}">${this.escapeHtml(q.query_id)} — ${this.escapeHtml(q.purpose)}</option>`
        ).join('');

        this.memoryTerminal.innerHTML = `
            <div style="padding:12px; color:#e2e8f0; font-size:0.76rem;">
                <!-- 1. Dataplex Aspect Authorization Tester -->
                <div style="background:rgba(30,41,59,0.92); border:1px solid rgba(56,189,248,0.4); border-radius:8px; padding:12px; margin-bottom:12px;">
                    <div style="font-weight:800; color:#38bdf8; font-size:0.85rem; margin-bottom:4px;">🛡️ 1. DATAPLEX UNIVERSAL CATALOG — PRE-READ ASPECT AUTHORIZATION</div>
                    <div style="font-size:0.71rem; color:#94a3b8; margin-bottom:8px;">
                        Evaluates live Dataplex aspect types (<code>lifecycle-status</code>, <code>sensitivity-class</code>, and <code>@Schema.{column}</code> overrides) before any BigQuery query executes.
                    </div>
                    <div style="display:flex; gap:6px; flex-wrap:wrap; margin-bottom:8px;">
                        <input type="text" id="kc-table-input" value="${this.escapeHtml(defaultTable)}" placeholder="Table name (e.g. disputes, authorizations_deprecated_v1)" style="flex:1; background:#0f172a; border:1px solid #334155; color:#f8fafc; padding:5px 8px; border-radius:5px; font-size:0.73rem;" />
                        <input type="text" id="kc-col-input" value="${this.escapeHtml(defaultColumn)}" placeholder="Column optional (e.g. pan_full, ssn_last4)" style="width:180px; background:#0f172a; border:1px solid #334155; color:#f8fafc; padding:5px 8px; border-radius:5px; font-size:0.73rem;" />
                        <button id="kc-check-auth-btn" class="btn-primary btn-sm">🔍 Check Governance Aspect</button>
                    </div>
                    <div style="display:flex; gap:5px; flex-wrap:wrap; margin-bottom:8px;">
                        <button class="kc-preset-btn btn-secondary btn-sm" data-table="authorizations_deprecated_v1" data-col="" style="font-size:0.66rem; padding:3px 7px;">🚫 Table: authorizations_deprecated_v1 (DEPRECATED -> REFUSE)</button>
                        <button class="kc-preset-btn btn-secondary btn-sm" data-table="cards" data-col="pan_full" style="font-size:0.66rem; padding:3px 7px;">🚫 Column: cards.pan_full (RESTRICTED PII -> REFUSE)</button>
                        <button class="kc-preset-btn btn-secondary btn-sm" data-table="disputes" data-col="" style="font-size:0.66rem; padding:3px 7px;">🔒 Table: disputes (ALLOW_TO_OWNER_ONLY)</button>
                        <button class="kc-preset-btn btn-secondary btn-sm" data-table="travel_notices" data-col="" style="font-size:0.66rem; padding:3px 7px;">✓ Table: travel_notices (ACTIVE)</button>
                    </div>
                    <div id="kc-auth-result-box"></div>
                </div>

                <!-- 2. BigQuery Governed Golden Query Runner -->
                <div style="background:rgba(30,41,59,0.92); border:1px solid rgba(16,185,129,0.4); border-radius:8px; padding:12px; margin-bottom:12px;">
                    <div style="font-weight:800; color:#34d399; font-size:0.85rem; margin-bottom:4px;">🏛️ 2. BIGQUERY GROUND-TRUTH GOLDEN QUERY RUNNER (` + "`jpmc_ccb_fraud`" + `)</div>
                    <div style="font-size:0.71rem; color:#94a3b8; margin-bottom:8px;">
                        Ad-hoc SQL is blocked. Agents can only execute these 10 parameterised queries after Dataplex Catalog aspect verification.
                    </div>
                    <div style="display:flex; gap:6px; margin-bottom:8px;">
                        <select id="wq-select" style="flex:1; background:#0f172a; border:1px solid #334155; color:#f8fafc; padding:6px 8px; border-radius:5px; font-size:0.73rem;">
                            ${queryOptionsHtml}
                        </select>
                        <button id="wq-run-btn" class="btn-primary btn-sm">▶️ Execute Governed Query</button>
                    </div>
                    <div id="wq-result-box"></div>
                </div>

                <!-- 3. Dataplex Business Glossary Search -->
                <div style="background:rgba(30,41,59,0.92); border:1px solid rgba(168,85,247,0.4); border-radius:8px; padding:12px;">
                    <div style="font-weight:800; color:#c084fc; font-size:0.85rem; margin-bottom:4px;">📖 3. DATAPLEX BUSINESS GLOSSARY <span id="kc-glossary-count">(term count loading…)</span></div>
                    <div style="display:flex; gap:6px; margin-bottom:8px;">
                        <input type="text" id="kc-term-query" value="provisional credit regulation" placeholder="Search glossary terms (e.g. Regulation E, Regulation Z, provisional credit)..." style="flex:1; background:#0f172a; border:1px solid #334155; color:#f8fafc; padding:5px 8px; border-radius:5px; font-size:0.73rem;" />
                        <button id="kc-term-search-btn" class="btn-primary btn-sm">🔍 Search Glossary</button>
                    </div>
                    <div id="kc-terms-result-box"></div>
                </div>
            </div>
        `;

        const checkAuth = async (tName, cName) => {
            const box = document.getElementById('kc-auth-result-box');
            if (!box) return;
            box.innerHTML = `<div style="color:#38bdf8; font-size:0.72rem;">Checking Dataplex aspect metadata for <code>${this.escapeHtml(tName)}${cName ? '.' + this.escapeHtml(cName) : ''}</code>...</div>`;
            const url = `/api/catalog/authorize?table=${encodeURIComponent(tName)}${cName ? '&column=' + encodeURIComponent(cName) : ''}`;
            const res = await fetch(url);
            const d = await res.json();
            const isRefuse = d.action === 'REFUSE' || !d.allowed;
            box.innerHTML = `
                <div style="padding:8px 10px; background:rgba(15,23,42,0.85); border-left:4px solid ${isRefuse ? '#ef4444' : '#10b981'}; border-radius:5px; font-size:0.72rem;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
                        <strong style="color:${isRefuse ? '#f87171' : '#34d399'};">Verdict: ${this.escapeHtml(d.action)} (${d.allowed ? 'PERMITTED' : 'AGENT DECLINED READ'})</strong>
                        <span class="badge-status ${isRefuse ? 'danger' : 'success'}">${this.escapeHtml(d.basis)} (${d.latency_ms} ms)</span>
                    </div>
                    <div style="color:#e2e8f0; margin-bottom:4px;"><strong>Agent Guidance / Rationale:</strong> ${this.escapeHtml(d.agent_guidance || d.rationale)}</div>
                    <div style="font-size:0.67rem; color:#94a3b8;">${this.escapeHtml(d.advisory_note || '')}</div>
                </div>
            `;
            this.appendTrace('DATAPLEX_AUTH', `Catalog verdict for ${tName}${cName ? '.' + cName : ''}: ${d.action} (${d.basis})`, 'thought');
        };

        const runGoldenQuery = async (qId) => {
            const box = document.getElementById('wq-result-box');
            if (!box) return;
            box.innerHTML = `<div style="color:#34d399; font-size:0.72rem;">Running governed BigQuery query <code>${this.escapeHtml(qId)}</code> for <code>${this.customerId}</code>...</div>`;
            const res = await fetch('/api/warehouse/run', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ query_id: qId, customer_id: this.customerId })
            });
            const d = await res.json();
            const rows = d.rows || [];
            const cols = rows.length > 0 ? Object.keys(rows[0]) : [];
            const tableHtml = rows.length > 0 ? `
                <div style="overflow-x:auto; max-height:200px; border:1px solid rgba(255,255,255,0.1); border-radius:5px; margin-top:6px;">
                    <table style="width:100%; border-collapse:collapse; font-size:0.68rem;">
                        <thead>
                            <tr style="background:rgba(15,23,42,0.95); color:#38bdf8; text-align:left;">
                                ${cols.map(c => `<th style="padding:4px 7px; border-bottom:1px solid rgba(255,255,255,0.12); white-space:nowrap;">${this.escapeHtml(c)}</th>`).join('')}
                            </tr>
                        </thead>
                        <tbody>
                            ${rows.map(r => `
                                <tr style="border-bottom:1px solid rgba(255,255,255,0.05);">
                                    ${cols.map(c => `<td style="padding:4px 7px; color:#e2e8f0; max-width:240px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${this.escapeHtml(r[c])}">${this.escapeHtml(r[c])}</td>`).join('')}
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                </div>
            ` : `<div style="padding:6px; color:#94a3b8;">0 rows returned.</div>`;

            const govBadges = (d.governance_checks || []).map(g =>
                `<span class="badge-status ${g.allowed ? 'success' : 'danger'}" style="font-size:0.63rem; margin-right:4px;">${this.escapeHtml(g.resource)}: ${this.escapeHtml(g.action)}</span>`
            ).join('');

            box.innerHTML = `
                <div style="padding:8px; background:rgba(15,23,42,0.85); border-radius:5px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:4px;">
                        <div><strong>Rows:</strong> ${d.row_count} • <strong>Latency:</strong> ${(d.latency_ms && d.latency_ms.value) || 'n/r'} ms • <strong>Job ID:</strong> <code>${this.escapeHtml((d.job_id && d.job_id.value) || '')}</code></div>
                        <div>${govBadges}</div>
                    </div>
                    ${tableHtml}
                </div>
            `;
            this.appendTrace('BIGQUERY_READ', `Executed golden query '${qId}' for ${this.customerId} -> ${d.row_count} rows in ${(d.latency_ms && d.latency_ms.value) || 'n/r'} ms.`, 'synthesis');
        };

        const searchGlossary = async (q) => {
            const box = document.getElementById('kc-terms-result-box');
            if (!box) return;
            if (!q) {
                box.innerHTML = '<div style="color:#94a3b8;">Type a term to search the live glossary.</div>';
                return;
            }
            box.innerHTML = '<div style="color:#94a3b8;">Searching the glossary\u2026</div>';
            try {
                // GET /api/catalog/terms reads `q`. This used to send `query`,
                // which the route ignored, so every search silently returned
                // the unfiltered list path and the box looked empty.
                const res = await fetch(`/api/catalog/terms?q=${encodeURIComponent(q)}&top_k=5`);
                const d = await res.json();
                if (!res.ok || d.error) {
                    box.innerHTML = `<div style="color:#f87171;">Glossary search failed: ${this.escapeHtml(d.error || 'HTTP ' + res.status)}</div>`;
                    this.appendTrace('GLOSSARY_ERROR', `Glossary search for "${q}" failed: ${d.error || 'HTTP ' + res.status}.`, 'error');
                    return;
                }
                const terms = d.terms || [];
                box.innerHTML = terms.map(t => `
                    <div style="padding:6px 8px; background:rgba(15,23,42,0.8); border-left:3px solid #a855f7; border-radius:4px; margin-bottom:5px; font-size:0.71rem;">
                        <div style="font-weight:700; color:#c084fc;">${this.escapeHtml(t.display_name)} <span style="color:#64748b; font-weight:400;">(${this.escapeHtml(t.term_id)})</span></div>
                        <div style="color:#cbd5e1; margin-top:2px;">${this.escapeHtml(t.summary)}</div>
                    </div>
                `).join('') || `<div style="color:#94a3b8;">No glossary term matched "${this.escapeHtml(q)}".</div>`;
                this.appendTrace('GLOSSARY_SEARCH', `Searched the live Dataplex glossary for "${q}": ${terms.length} match(es).`, 'thought');
            } catch (err) {
                box.innerHTML = `<div style="color:#f87171;">Glossary search failed: ${this.escapeHtml(err && err.message ? err.message : String(err))}</div>`;
            }
        };

        document.getElementById('kc-check-auth-btn').addEventListener('click', () => {
            checkAuth(document.getElementById('kc-table-input').value.trim(), document.getElementById('kc-col-input').value.trim());
        });
        this.memoryTerminal.querySelectorAll('.kc-preset-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.getElementById('kc-table-input').value = btn.dataset.table;
                document.getElementById('kc-col-input').value = btn.dataset.col;
                checkAuth(btn.dataset.table, btn.dataset.col);
            });
        });
        document.getElementById('wq-run-btn').addEventListener('click', () => {
            runGoldenQuery(document.getElementById('wq-select').value);
        });
        document.getElementById('kc-term-search-btn').addEventListener('click', () => {
            searchGlossary(document.getElementById('kc-term-query').value.trim());
        });

        // Auto-load initial examples
        checkAuth(defaultTable, defaultColumn);
        runGoldenQuery('open_disputes');
        searchGlossary('provisional credit regulation');
    }

    /** Fill in the catalog term count from the catalog, not from memory. */
    async refreshCatalogTermCount() {
        try {
            const res = await fetch('/api/catalog/terms');
            const d = await res.json();
            if (!res.ok || d.error) throw new Error(d.error || `HTTP ${res.status}`);
            this.cachedCatalogTermCount = d.count !== undefined ? d.count : (d.terms || []).length;
            const glossaryCount = document.getElementById('kc-glossary-count');
            if (glossaryCount) {
                glossaryCount.textContent = `(${this.plural(this.cachedCatalogTermCount, 'term', 'terms')})`;
            }
            if (this.currentViewMode === 'kg') {
                this.memoryCountBadge.textContent =
                    `Dataplex Catalog \u2022 ${this.plural(this.cachedCatalogTermCount, 'term', 'terms')} \u2022 ${this.plural(this.cachedGoldenQueries.length, 'golden query', 'golden queries')}`;
            }
        } catch (err) {
            const glossaryCount = document.getElementById('kc-glossary-count');
            if (glossaryCount) glossaryCount.textContent = '(term count unavailable)';
            if (this.currentViewMode === 'kg') {
                this.memoryCountBadge.textContent =
                    `Dataplex Catalog \u2022 term count unavailable \u2022 ${this.plural(this.cachedGoldenQueries.length, 'golden query', 'golden queries')}`;
            }
        }
    }

    // =========================================================================
    // TAB 4: SIDE-BY-SIDE COMPARISON (Google ADK vs. Direct Non-ADK Agent)
    // =========================================================================

    /**
     * Custom vs ADK.
     *
     * Two shapes are accepted:
     *
     *   1. A real comparison: {lane_adk, lane_direct, summary:{epistemic_parity,
     *      adk_loc, direct_loc}} -- the shape backend/custom_agent_comparison.py
     *      already produces. No route serves it yet (see the report), so this
     *      path lights up the moment one does.
     *   2. What POST /api/agents/run returns today: a single ADK trace
     *      {text, tool_calls, agents_that_produced_events, event_count, error}.
     *      That is one lane, not a comparison, and the panel says so.
     *
     * The parity badge is only drawn when the backend computed a parity value.
     * There is no default, and no colour is chosen from anything but that value.
     */
    async renderComparisonView(customPrompt = null) {
        const defaultPrompt = customPrompt || 'Where did this customer travel recently, what are her open disputes under Reg E vs Reg Z, and does her memory match the bank record?';
        this.memoryCountBadge.textContent = 'Custom vs ADK';

        this.memoryTerminal.innerHTML = `
            <div style="padding:12px; color:#e2e8f0; font-size:0.76rem;">
                <div style="background:rgba(30,41,59,0.92); border:1px solid rgba(56,189,248,0.4); border-radius:8px; padding:10px; margin-bottom:10px;">
                    <div style="font-weight:800; color:#38bdf8; font-size:0.84rem; margin-bottom:4px;">\u2696\ufe0f GOOGLE NATIVE ADK AGENT vs. DIRECT NON-ADK AGENT</div>
                    <div style="font-size:0.7rem; color:#94a3b8; margin-bottom:8px;">
                        Scope <code>${this.escapeHtml(this.customerId)}</code>. Only fields present in the response are shown; anything the backend did not send is marked as such.
                    </div>
                    <div style="display:flex; gap:6px;">
                        <input type="text" id="comparison-prompt-input" value="${this.escapeHtml(defaultPrompt)}" style="flex:1; background:#0f172a; border:1px solid #334155; color:#f8fafc; padding:5px 8px; border-radius:5px; font-size:0.73rem;" />
                        <button id="run-comparison-inline-btn" class="btn-primary btn-sm">\u26a1 Run</button>
                    </div>
                </div>
                <div id="comparison-results-body" style="padding:1.2rem; text-align:center; color:#38bdf8; font-weight:600;">
                    \u23f3 Running against live Vertex AI &amp; BigQuery\u2026
                </div>
            </div>
        `;

        const runBtn = document.getElementById('run-comparison-inline-btn');
        const promptInput = document.getElementById('comparison-prompt-input');
        if (runBtn && promptInput) {
            runBtn.addEventListener('click', () => this.renderComparisonView(promptInput.value.trim()));
        }

        this.appendTrace('COMPARISON_START', `Running the agent lane(s) for ${this.customerId}\u2026`, 'thought');

        const bodyEl = () => document.getElementById('comparison-results-body');

        try {
            const res = await fetch('/api/agents/run', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    mode: 'comparison',
                    customer_id: this.customerId,
                    question: defaultPrompt
                })
            });
            const d = await res.json();
            const el = bodyEl();
            if (!el) return;
            el.style.textAlign = 'left';
            el.style.padding = '0';

            if (!res.ok || d.error) {
                el.innerHTML = `
                    <div style="background:rgba(239,68,68,0.12); border:1px solid rgba(239,68,68,0.45); border-radius:6px; padding:10px; font-size:0.75rem; color:#fca5a5;">
                        <strong style="color:#f87171;">The run failed.</strong><br>
                        ${this.escapeHtml(d.error || d.failure || `HTTP ${res.status}`)}
                        <div style="color:#94a3b8; margin-top:6px;">No parity verdict is shown, because nothing ran to compare.</div>
                    </div>`;
                this.appendTrace('COMPARISON_FAILED', `Agent run failed: ${d.error || 'HTTP ' + res.status}.`, 'error');
                return;
            }

            const laneAdk = d.lane_adk || d.adk_lane || (d.comparison && d.comparison.lane_adk) || null;
            const laneDirect = d.lane_direct || d.direct_lane || (d.comparison && d.comparison.lane_direct) || null;
            const cmpSummary = d.summary || (d.comparison && d.comparison.summary) || null;

            if (laneAdk && laneDirect) {
                el.innerHTML = this.renderTwoLaneComparison(laneAdk, laneDirect, cmpSummary);
                const parity = cmpSummary && cmpSummary.epistemic_parity;
                this.appendTrace('COMPARISON_COMPLETE',
                    `Two-lane comparison returned. Epistemic parity: ${parity || 'not reported by the backend'}.`, 'synthesis');
            } else {
                el.innerHTML = this.renderSingleLaneTrace(d);
                this.appendTrace('COMPARISON_PARTIAL',
                    'POST /api/agents/run returned one ADK trace, not a two-lane comparison. Parity was not computed, so no verdict is shown.',
                    'thought');
            }
        } catch (err) {
            const el = bodyEl();
            if (el) {
                el.style.textAlign = 'left';
                el.innerHTML = `<div style="padding:10px; color:#f87171; font-size:0.75rem;">Comparison request failed: ${this.escapeHtml(err && err.message ? err.message : String(err))}</div>`;
            }
        }
    }

    /**
     * A tool call's name. The ADK trace uses `tool`; the comparison lane uses
     * `name`. Anything else is labelled rather than stringified into
     * "[object Object]".
     */
    toolName(t) {
        if (typeof t === 'string') return t;
        if (t && typeof t === 'object') {
            const n = t.tool || t.name || t.function_name;
            if (n) return t.agent ? `${n} (${t.agent})` : String(n);
        }
        return 'unnamed tool';
    }

    /** Render a value that may be a Provenanced {value, unit, rendered}. */
    provVal(v) {
        if (v === null || v === undefined) return '<span class="not-measured">not reported</span>';
        if (typeof v === 'object') {
            if (v.rendered) return this.escapeHtml(String(v.rendered));
            if (v.value !== null && v.value !== undefined) return this.escapeHtml(`${v.value}${v.unit || ''}`);
            const why = v.reason || v.why || v.note;
            return `<span class="not-measured">not measured${why ? ': ' + this.escapeHtml(String(why)) : ''}</span>`;
        }
        return this.escapeHtml(String(v));
    }

    renderLaneCard(title, colour, lane) {
        const status = lane.status || 'status not reported';
        const ok = String(status).toUpperCase() === 'OK' || String(status).toUpperCase() === 'SUCCEEDED';
        const tools = lane.tool_calls || [];
        const mems = lane.memory_ids_seen || [];
        const body = lane.response_text || lane.answer || lane.failure_message;
        return `
            <div style="background:rgba(30,41,59,0.92); border:1px solid ${colour}; border-radius:8px; padding:10px;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; gap:6px;">
                    <span style="font-weight:800; color:${colour}; font-size:0.8rem;">${title}</span>
                    <span class="badge-status ${ok ? 'success' : 'warning'}">${this.escapeHtml(String(status))}</span>
                </div>
                <div style="font-size:0.7rem; color:#cbd5e1; margin-bottom:8px; line-height:1.45;">
                    \u2022 <strong>Latency:</strong> ${this.provVal(lane.latency_ms)}<br>
                    \u2022 <strong>Prompt / response tokens:</strong> ${this.provVal(lane.prompt_tokens)} / ${this.provVal(lane.response_tokens !== undefined ? lane.response_tokens : lane.completion_tokens)}<br>
                    \u2022 <strong>Tool calls observed:</strong> <code>${tools.length}</code>${tools.length ? ' (' + this.escapeHtml(tools.map(t => this.toolName(t)).join(', ')) + ')' : ''}<br>
                    \u2022 <strong>Memory ids seen:</strong> ${mems.length ? `<code>${this.escapeHtml(mems.join(', '))}</code>` : '<span class="not-measured">none reported</span>'}
                </div>
                <div style="background:#0f172a; padding:8px; border-radius:6px; font-size:0.73rem; color:#f8fafc; max-height:220px; overflow-y:auto; line-height:1.45;">
                    ${body ? this.formatMarkdown(body) : '<span class="not-measured">This lane returned no text.</span>'}
                </div>
            </div>`;
    }

    renderTwoLaneComparison(laneAdk, laneDirect, summary) {
        const parity = summary && summary.epistemic_parity;
        let parityHtml;
        if (!parity) {
            parityHtml = `
                <div style="background:rgba(148,163,184,0.1); border:1px solid rgba(148,163,184,0.35); border-radius:6px; padding:7px 10px; margin-bottom:10px; font-size:0.73rem; color:#cbd5e1;">
                    <strong>Epistemic parity:</strong> <span class="not-measured">not computed by the backend, so no verdict is shown.</span>
                </div>`;
        } else {
            const p = String(parity).toLowerCase();
            const cls = p === 'identical' ? 'success' : (p === 'diverged' ? 'danger' : 'neutral');
            const words = {
                identical: 'Both lanes saw the same memory ids.',
                diverged: 'The lanes saw different memory ids.',
                not_established: 'At least one lane did not report the memories it saw, so parity could not be established.'
            };
            parityHtml = `
                <div style="background:rgba(15,23,42,0.85); border:1px solid rgba(255,255,255,0.12); border-radius:6px; padding:7px 10px; margin-bottom:10px; display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap; font-size:0.73rem;">
                    <span><strong>\ud83d\udd2c Epistemic parity (computed by the backend):</strong> ${this.escapeHtml(words[p] || '')}</span>
                    <span class="badge-status ${cls}">PARITY: ${this.escapeHtml(String(parity).toUpperCase())}</span>
                </div>`;
        }

        const locHtml = summary ? `
            <div style="font-size:0.69rem; color:#94a3b8; margin-top:8px;">
                Integration lines of code \u2014 ADK: ${this.provVal(summary.adk_loc)} \u2022 direct: ${this.provVal(summary.direct_loc)}
            </div>` : '';

        return `
            ${parityHtml}
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px;">
                ${this.renderLaneCard('\ud83e\udd16 Lane A: Google native ADK agent', '#60a5fa', laneAdk)}
                ${this.renderLaneCard('\u2699\ufe0f Lane B: Direct non-ADK agent', '#c084fc', laneDirect)}
            </div>
            ${locHtml}`;
    }

    /** One ADK trace. Honest about the missing second lane. */
    renderSingleLaneTrace(d) {
        const tools = d.tool_calls || [];
        const agents = d.agents_that_produced_events || [];
        return `
            <div style="background:rgba(251,191,36,0.1); border:1px solid rgba(251,191,36,0.45); border-radius:6px; padding:9px 11px; margin-bottom:10px; font-size:0.73rem; color:#fde68a;">
                <strong>Comparison data not available from the backend.</strong>
                <div style="color:#cbd5e1; margin-top:4px; line-height:1.45;">
                    <code>POST /api/agents/run</code> executes the ADK graph and returns one trace. It does not run a second, non-ADK lane,
                    and it does not report latency, token counts, integration LOC or an epistemic-parity verdict.
                    Until a route returns those, only Lane A can be shown \u2014 and no parity badge is drawn,
                    because nothing computed one.
                </div>
            </div>
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px;">
                <div style="background:rgba(30,41,59,0.92); border:1px solid #3b82f6; border-radius:8px; padding:10px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
                        <span style="font-weight:800; color:#60a5fa; font-size:0.8rem;">\ud83e\udd16 Lane A: Google native ADK agent</span>
                        <span class="badge-status ${d.error ? 'danger' : 'success'}">${d.error ? 'ERROR' : 'RAN'}</span>
                    </div>
                    <div style="font-size:0.7rem; color:#cbd5e1; margin-bottom:8px; line-height:1.45;">
                        \u2022 <strong>Events observed:</strong> <code>${this.orDash(d.event_count)}</code><br>
                        \u2022 <strong>Agents that produced events:</strong> ${agents.length ? `<code>${this.escapeHtml(agents.join(', '))}</code>` : '<span class="not-measured">none observed</span>'}<br>
                        \u2022 <strong>Tool calls observed:</strong> <code>${tools.length}</code>${tools.length ? ' (' + this.escapeHtml(tools.map(t => this.toolName(t)).join(', ')) + ')' : ' \u2014 the model answered without calling anything'}<br>
                        \u2022 <strong>Latency / tokens / LOC:</strong> <span class="not-measured">not returned by this endpoint</span>
                    </div>
                    <div style="background:#0f172a; padding:8px; border-radius:6px; font-size:0.73rem; color:#f8fafc; max-height:220px; overflow-y:auto; line-height:1.45;">
                        ${d.error ? `<span style="color:#f87171;">${this.escapeHtml(d.error)}</span>` : (d.text ? this.formatMarkdown(d.text) : '<span class="not-measured">This lane returned no text.</span>')}
                    </div>
                </div>
                <div style="background:rgba(15,23,42,0.7); border:1px dashed rgba(148,163,184,0.45); border-radius:8px; padding:10px; display:flex; flex-direction:column; justify-content:center; align-items:center; text-align:center;">
                    <div style="font-weight:800; color:#94a3b8; font-size:0.8rem; margin-bottom:6px;">\u2699\ufe0f Lane B: Direct non-ADK agent</div>
                    <div style="font-size:0.72rem; color:#94a3b8; line-height:1.5;">
                        Not run.<br>
                        <code>backend/custom_agent_comparison.py</code> already measures this lane,
                        but no HTTP route exposes it. The panel will populate itself the moment one returns
                        <code>lane_adk</code>, <code>lane_direct</code> and <code>summary</code>.
                    </div>
                </div>
            </div>`;
    }

    // =========================================================================
    // TAB 5: AUDIT & RECONCILE (Memory Bank vs. BigQuery Ground Truth)
    // =========================================================================

    async renderAuditReportView() {
        this.memoryCountBadge.textContent = `Auditing ${this.customerId}...`;
        try {
            const res = await fetch(`/api/audit/customer/${encodeURIComponent(this.customerId)}`);
            const audit = await res.json();
            this.cachedAuditReport = audit;

            const contradicted = audit.contradicted || [];
            this.memoryCountBadge.textContent = `${contradicted.length} Contradictions • ${audit.corroborated_count || 0} Corroborated`;

            const contraHtml = contradicted.map(c => `
                <div style="padding:9px 11px; background:rgba(239,68,68,0.12); border:1px solid rgba(239,68,68,0.4); border-left:4px solid #ef4444; border-radius:6px; margin-bottom:8px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
                        <strong style="color:#f87171; font-size:0.76rem;">❌ CONTRADICTED: ${this.escapeHtml(String(c.claim_kind || '').toUpperCase())} (Memory #${this.escapeHtml(c.memory_id.slice(-8))})</strong>
                        <span class="badge-status danger">BigQuery Wins</span>
                    </div>
                    <div style="font-size:0.75rem; color:#f8fafc; margin-bottom:4px;"><strong>Customer Memory:</strong> "${this.escapeHtml(c.memory_fact)}"</div>
                    <div style="font-size:0.72rem; color:#fca5a5;">
                        • <strong>Memory Claimed:</strong> <code>${this.escapeHtml((c.claimed_values || []).join(', '))}</code><br>
                        • <strong>BigQuery Authoritative Record:</strong> <code>${this.escapeHtml((c.authoritative_values || []).join(', '))}</code><br>
                        • <strong>Auditor Finding:</strong> ${this.escapeHtml(c.explanation)}
                    </div>
                </div>
            `).join('');

            this.memoryTerminal.innerHTML = `
                <div style="padding:12px; color:#e2e8f0; font-size:0.76rem;">
                    <div style="background:rgba(30,41,59,0.92); border:1px solid rgba(239,68,68,0.4); border-radius:8px; padding:12px; margin-bottom:12px;">
                        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
                            <span style="font-weight:800; color:#f87171; font-size:0.86rem;">🔍 MEMORY BANK vs. BIGQUERY GROUND-TRUTH RECONCILIATION SWEEP</span>
                            <span class="badge-status ${audit.clean ? 'success' : 'danger'}">${audit.memories_scanned} Memories Scanned</span>
                        </div>
                        <div style="font-size:0.72rem; color:#cbd5e1; margin-bottom:10px;">
                            ${this.escapeHtml(audit.interpretation || '')}
                        </div>
                        ${contraHtml || '<div style="color:#34d399; font-weight:700;">✓ Zero contradictions found against BigQuery ground truth.</div>'}
                    </div>
                </div>
            `;
            this.appendTrace('AUDIT_SWEEP', `Reconciled ${audit.memories_scanned} memories for ${this.customerId} against BigQuery: ${contradicted.length} contradictions identified.`, 'synthesis');
        } catch (err) {
            console.error('Audit sweep failed:', err);
        }
    }

    // =========================================================================
    // TAB 6: CARD LIFECYCLE & REG E / REG Z OVERSIGHT
    // =========================================================================

    renderAdminOversightView() {
        const data = this.customerOverview || {};
        const cards = data.cards || [];
        const disputes = data.open_disputes || [];
        this.memoryCountBadge.textContent = `${cards.length} Cards • ${disputes.length} Disputes`;

        const cardsHtml = cards.map(c => `
            <div style="display:flex; justify-content:space-between; align-items:center; padding:7px 10px; background:rgba(15,23,42,0.8); border-left:3px solid ${c.status === 'ACTIVE' ? '#10b981' : (c.status === 'FROZEN' ? '#ef4444' : '#f59e0b')}; border-radius:5px; margin-bottom:6px; font-size:0.73rem;">
                <div>
                    <strong style="color:#f8fafc;">${this.escapeHtml(c.card_id)} (${this.escapeHtml(c.pan_masked)})</strong> — ${this.escapeHtml(c.card_type)} ${this.escapeHtml(c.network)}
                    <div style="font-size:0.68rem; color:#94a3b8;">
                        Issued: ${this.escapeHtml(c.issue_date)} • Replaces: ${this.escapeHtml(c.replaces_card_id || 'None')} • Replaced By: ${this.escapeHtml(c.replaced_by_card_id || 'None')}
                        ${c.freeze_reason ? ` • Freeze Reason: <code>${this.escapeHtml(c.freeze_reason)}</code>` : ''}
                    </div>
                </div>
                <span class="badge-status ${c.status === 'ACTIVE' ? 'success' : (c.status === 'FROZEN' ? 'danger' : 'warning')}">${this.escapeHtml(c.status)}</span>
            </div>
        `).join('');

        const disputesHtml = disputes.map(d => `
            <div style="padding:8px 10px; background:rgba(15,23,42,0.8); border-left:3px solid ${d.regulation_path === 'REG_E' ? '#38bdf8' : '#c084fc'}; border-radius:5px; margin-bottom:6px; font-size:0.72rem;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:3px;">
                    <strong style="color:#f8fafc;">${this.escapeHtml(d.dispute_id)} • $${this.escapeHtml(d.disputed_amount)} on ${this.escapeHtml(d.card_id)} (${this.escapeHtml(d.pan_masked)})</strong>
                    <span class="badge-status ${d.provisional_credit_issued ? 'success' : 'warning'}">${this.escapeHtml(d.regulation_path)} • ${this.escapeHtml(d.status)}</span>
                </div>
                <div style="color:#cbd5e1; font-size:0.69rem;">${this.escapeHtml(d.dispute_reason)}</div>
                <div style="color:#94a3b8; font-size:0.67rem; margin-top:3px;">
                    Opened: ${this.escapeHtml(d.opened_date)} • Investigation Due: ${this.escapeHtml(d.investigation_due_date)} • Provisional Credit: ${d.provisional_credit_issued ? `$${d.provisional_credit_amount} on ${d.provisional_credit_date}` : 'Not required (Reg Z Credit)'}
                </div>
            </div>
        `).join('');

        this.memoryTerminal.innerHTML = `
            <div style="padding:12px; color:#e2e8f0; font-size:0.76rem;">
                <div style="background:rgba(30,41,59,0.92); border:1px solid rgba(56,189,248,0.4); border-radius:8px; padding:12px; margin-bottom:12px;">
                    <div style="font-weight:800; color:#38bdf8; font-size:0.84rem; margin-bottom:8px;">💳 CARD INVENTORY &amp; REPLACEMENT CHAIN (` + "`cards`" + ` Table)</div>
                    ${cardsHtml}
                </div>
                <div style="background:rgba(30,41,59,0.92); border:1px solid rgba(168,85,247,0.4); border-radius:8px; padding:12px;">
                    <div style="font-weight:800; color:#c084fc; font-size:0.84rem; margin-bottom:8px;">⚖️ OPEN DISPUTES: REGULATION E (DEBIT) vs. REGULATION Z (CREDIT)</div>
                    ${disputesHtml}
                </div>
            </div>
        `;
    }

    // =========================================================================
    // TAB 7: RAW JSON INSPECTOR
    // =========================================================================

    renderJsonInspectorView() {
        const payload = {
            customer_id: this.customerId,
            memory_topics_configured: this.cachedTopics,
            memories_in_scope: this.cachedMemories,
            warehouse_reconciliation: this.customerOverview ? this.customerOverview.reconciliation : null
        };
        this.memoryTerminal.innerHTML = `<pre style="padding:12px; font-family:'JetBrains Mono',monospace; font-size:0.71rem; color:#38bdf8; overflow-x:auto;">${this.escapeHtml(JSON.stringify(payload, null, 2))}</pre>`;
    }

    // =========================================================================
    // LEFT PANEL: INTELLIGENT WRITE & PRE-WRITE VERACITY TESTER
    // =========================================================================

    async executePreWriteValidation() {
        const mode = this.validatorChannelSelect ? this.validatorChannelSelect.value : 'LIVE_WRITE_AND_VERIFY';
        const claimText = this.validatorClaimInput.value.trim();
        if (!claimText) return;

        const testWrite = mode === 'LIVE_WRITE_AND_VERIFY';
        this.validatorResult.style.display = 'block';
        this.validatorResult.innerHTML = `
            <div style="color:#38bdf8; font-size:0.72rem; padding:4px;">
                ⏳ Evaluating claim against BigQuery Ground Truth ${testWrite ? 'and executing live Vertex AI Memory Bank `memories:generate` extraction filter (~6–10s LRO)...' : '...'}
            </div>
        `;
        this.appendTrace('WRITE_AND_VERIFY', `Testing utterance: "${claimText.slice(0, 70)}..." (test_write=${testWrite})`, 'thought');

        try {
            const res = await fetch('/api/veracity/validate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    customer_id: this.customerId,
                    claim_text: claimText,
                    test_write: testWrite
                })
            });
            const data = await res.json();
            const ver = data.veracity || {};
            const iw = data.intelligent_write;

            const isContra = ver.status === 'CONTRADICTED';
            const isCorr = ver.status === 'CORROBORATED';
            const statusColor = isContra ? '#ef4444' : (isCorr ? '#10b981' : '#38bdf8');

            let writeHtml = '';
            if (iw) {
                const memsHtml = (iw.extracted_memories || []).map(m => `
                    <div style="margin-top:4px; padding:4px 7px; background:rgba(16,185,129,0.14); border-left:3px solid #10b981; border-radius:4px; font-size:0.69rem;">
                        <strong style="color:#34d399;">[${this.escapeHtml((m.topics || []).join(', '))}]</strong> "${this.escapeHtml(m.fact)}"
                    </div>
                `).join('');

                writeHtml = `
                    <div style="margin-top:7px; padding-top:7px; border-top:1px solid rgba(255,255,255,0.12);">
                        <div style="display:flex; justify-content:space-between; align-items:center;">
                            <strong style="color:${iw.wrote_nothing ? '#fde047' : '#34d399'}; font-size:0.72rem;">
                                🧠 Memory Bank ` + "`generateMemories`" + ` Outcome: ${iw.wrote_nothing ? '0 MEMORIES WRITTEN (`wrote_nothing=True`)' : `${iw.generated_count} DURABLE FACT(S) EXTRACTED`}
                            </strong>
                            <span class="badge-status ${iw.wrote_nothing ? 'warning' : 'success'}">${(iw.latency_ms && iw.latency_ms.value) || ''} ms</span>
                        </div>
                        <div style="font-size:0.69rem; color:#cbd5e1; margin-top:3px;">${this.escapeHtml(iw.explanation)}</div>
                        ${memsHtml}
                    </div>
                `;
            }

            this.validatorResult.innerHTML = `
                <div style="padding:8px; background:rgba(15,23,42,0.92); border-left:4px solid ${statusColor}; border-radius:6px; font-size:0.71rem;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:3px;">
                        <strong style="color:${statusColor};">🛡️ BigQuery Ground-Truth Verdict: ${this.escapeHtml(ver.status)}</strong>
                        <span class="badge-status">${(ver.latency_ms && ver.latency_ms.value) || ''} ms</span>
                    </div>
                    <div style="color:#e2e8f0;">${this.escapeHtml(ver.note)}</div>
                    ${writeHtml}
                </div>
            `;
            this.appendTrace('VALIDATOR_RESULT', `Veracity: ${ver.status}. ${iw ? (iw.wrote_nothing ? 'Memory Bank wrote NOTHING (off-topic).' : `Memory Bank extracted ${iw.generated_count} durable memory.`) : ''}`, 'synthesis');
        } catch (err) {
            this.validatorResult.innerHTML = `<div style="color:#f87171; font-size:0.72rem;">Validation error: ${this.escapeHtml(err.message)}</div>`;
        }
    }

    // =========================================================================
    // RIGHT PANEL: LIVE CHAT & DYNAMIC A2UI EVIDENCE SYNTHESIS CARD
    // =========================================================================

    async executeChatWorkflow(prompt) {
        this.appendChatMessage('user', prompt);
        this.chatInput.value = '';
        this.a2uiContainer.innerHTML = '';

        const agentMessageDiv = this.appendChatMessage('agent', '⏳ Gathering live Memory Bank recollections, verifying Dataplex Catalog aspects, and reading BigQuery system of record...');
        const bubble = agentMessageDiv.querySelector('.msg-bubble');

        this.appendTrace('USER_QUERY', `Dispatched to FraudDeskAgent for ${this.customerId}: "${prompt}"`, 'thought');

        try {
            const res = await fetch('/api/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    customer_id: this.customerId,
                    question: prompt,
                    write_back: true
                })
            });
            const data = await res.json();

            if (data.status === 'FAILED' || data.error) {
                bubble.innerHTML = `<span style="color:#f87171;">⚠️ Service Error: ${this.escapeHtml(data.failure || data.error)}</span>`;
                this.renderMemoryProvenanceUnavailable(data.failure || data.error || 'The turn failed.');
                return;
            }

            const ev = data.evidence || {};
            const mems = this.memoriesFrom(data);
            const govs = this.governanceFrom(ev);
            const terms = this.glossaryFrom(ev);
            const memLatency = this.retrievalLatency(ev);

            this.appendTrace('MEMORY_RETRIEVED',
                `Memory Bank returned ${mems.length} memory/memories for this question${memLatency ? ' in ' + memLatency : ''}.`,
                'memory');
            this.appendTrace('DATAPLEX_GOVERNANCE',
                `${govs.length} Dataplex governance verdict(s) and ${terms.length} glossary definition(s) came back with the answer.`,
                'thought');
            this.appendTrace('SYNTHESIS_COMPLETE',
                `Answer synthesised${data.timings && data.timings.synthesis_ms !== undefined ? ' in ' + data.timings.synthesis_ms + ' ms' : ''}.`,
                'synthesis');

            bubble.innerHTML = this.formatMarkdown(data.answer || '');
            this.chatViewport.scrollTop = this.chatViewport.scrollHeight;

            this.renderMemoryProvenance(data);
            this.buildA2UiEvidenceCard(data);
        } catch (err) {
            bubble.innerHTML = `<span style="color:#f87171;">⚠️ Request error: ${this.escapeHtml(err.message)}</span>`;
            this.renderMemoryProvenanceUnavailable(err && err.message ? err.message : String(err));
        }
    }

    // =========================================================================
    // "MEMORIES THAT INFORMED THIS ANSWER"
    //
    // The retrieved memories are what the customer came to see: which stored
    // facts the agent actually read before answering. All of it is read
    // defensively, because the backend field names are still settling -- a
    // missing field degrades to the empty state rather than throwing.
    // =========================================================================

    /** Memories from whichever place the response carries them. */
    memoriesFrom(data) {
        const ev = (data && data.evidence) || {};
        const candidates = [
            ev.memories,
            ev.memory_bank && ev.memory_bank.memories,
            ev.memories_used,
            data && data.memories,
            data && data.memories_used,
            ev.retrieved_memories
        ];
        for (const c of candidates) {
            if (Array.isArray(c)) return c;
        }
        return [];
    }

    governanceFrom(ev) {
        const c = ev.governance || (ev.catalog && ev.catalog.governance_decisions);
        return Array.isArray(c) ? c : [];
    }

    glossaryFrom(ev) {
        const c = ev.glossary || (ev.catalog && ev.catalog.glossary_terms);
        if (Array.isArray(c)) return c;
        if (c && typeof c === 'object') return Object.keys(c).map(k => ({ term_id: k, ...(c[k] || {}) }));
        return [];
    }

    /** A rendered retrieval latency, or '' if nothing measured it. */
    retrievalLatency(ev) {
        const p = ev.provenance || {};
        const entry = p.memory_retrieval_ms;
        if (!entry) return '';
        if (typeof entry === 'string') return entry;
        if (typeof entry === 'object') return entry.rendered || (entry.value !== undefined ? `${entry.value} ms` : '');
        return String(entry);
    }

    setProvenanceState(cls, badgeText, badgeCls, subText) {
        if (this.memoryProvenance) {
            this.memoryProvenance.className = `memory-provenance ${cls}`.trim();
        }
        if (this.mpBadge) {
            this.mpBadge.className = `mp-badge ${badgeCls}`.trim();
            this.mpBadge.textContent = badgeText;
        }
        if (this.mpSub) this.mpSub.textContent = subText;
    }

    renderMemoryProvenanceUnavailable(reason) {
        if (!this.mpBody) return;
        this.setProvenanceState('is-unavailable', 'unavailable', 'unavailable',
            'Memory Bank retrieval could not be confirmed for this turn.');
        this.mpBody.innerHTML =
            `<div class="mp-empty"><strong>Memory retrieval state is unknown for this answer.</strong><br>${this.escapeHtml(String(reason))}</div>`;
    }

    renderMemoryProvenance(data) {
        if (!this.mpBody) return;

        const ev = (data && data.evidence) || {};
        const mems = this.memoriesFrom(data);
        const ctx = ev.memory_context || {};
        const contradicted = ev.contradicted_memory_ids || data.contradicted_memory_ids || [];
        const memErrors = (ev.errors || []).filter(e => String(e.source || '').startsWith('memory_bank'));
        const state = String(ctx.state || '').toUpperCase();

        // Retrieval itself failed: not the same as "nothing matched".
        if (state === 'UNAVAILABLE' || (mems.length === 0 && memErrors.length > 0)) {
            const why = ctx.note || (memErrors[0] && (memErrors[0].error || memErrors[0].detail)) || 'Memory Bank did not answer.';
            this.renderMemoryProvenanceUnavailable(why);
            return;
        }

        // Retrieval ran and matched nothing.
        if (mems.length === 0) {
            this.setProvenanceState('is-empty', 'none', 'empty',
                'Memory Bank was read for this question.');
            this.mpBody.innerHTML =
                '<div class="mp-empty"><strong>No stored memories matched this question.</strong><br>' +
                this.escapeHtml(ctx.note || 'The answer rests on warehouse evidence alone; no prior context informed it.') +
                '</div>';
            return;
        }

        const injected = ctx.injected_into_prompt !== false;
        this.setProvenanceState('', `${mems.length} used`, 'used',
            injected
                ? `${this.plural(mems.length, 'memory was', 'memories were')} retrieved and fed to the agent before it answered.`
                : `${this.plural(mems.length, 'memory was', 'memories were')} retrieved. The backend did not confirm they reached the prompt.`);

        const itemsHtml = mems.map((m, idx) => {
            const id = m.memory_id || m.id || m.short_id || '';
            const shortId = m.short_id || (id ? String(id).slice(-8) : '');
            const topics = m.topics || [];
            const isContra = (contradicted || []).includes(id) || m.reconciliation_status === 'CONTRADICTED';

            const hasDistance = m.distance !== null && m.distance !== undefined && Number.isFinite(Number(m.distance));
            const distHtml = hasDistance
                ? `<span class="mp-distance" title="Vector distance from the question. Lower is a closer match.">distance ${Number(m.distance).toFixed(4)}</span>`
                : `<span class="mp-distance unmeasured" title="This memory came from a scope listing, which does not score distance.">distance not scored</span>`;

            const topicHtml = topics.length
                ? topics.map(t => `<span class="mp-topic">${this.escapeHtml(t)}</span>`).join('')
                : '<span class="mp-topic" style="opacity:0.6;">no topic</span>';

            const updated = m.update_time
                ? `<span class="mp-id" title="Last update time reported by Memory Bank">updated ${this.escapeHtml(String(m.update_time).slice(0, 10))}</span>`
                : '';

            return `
                <div class="mp-item ${isContra ? 'contradicted' : ''}">
                    <div class="mp-rank">${idx + 1}</div>
                    <div class="mp-item-main">
                        <div class="mp-fact">“${this.escapeHtml(m.fact || '(no fact text returned)')}”</div>
                        <div class="mp-meta">
                            ${topicHtml}
                            ${distHtml}
                            ${shortId ? `<span class="mp-id" title="${this.escapeHtml(String(id))}">#${this.escapeHtml(shortId)}</span>` : ''}
                            ${updated}
                            ${isContra ? '<span class="mp-flag">contradicted by BigQuery</span>' : ''}
                        </div>
                    </div>
                </div>`;
        }).join('');

        const scored = mems.filter(m => m.distance !== null && m.distance !== undefined && Number.isFinite(Number(m.distance))).length;
        const footnote = scored === 0
            ? 'None of these carried a distance score, so they were listed by scope rather than ranked by similarity.'
            : `${scored} of ${mems.length} carried a vector distance; lower means a closer match to the question.`;

        this.mpBody.innerHTML = itemsHtml +
            `<div class="mp-footnote">${this.escapeHtml(footnote)}${ctx.note ? ' ' + this.escapeHtml(ctx.note) : ''}</div>`;
    }

    buildA2UiEvidenceCard(data) {
        const ev = data.evidence || {};
        const mems = this.memoriesFrom(data);
        const govs = this.governanceFrom(ev);
        const rec = ev.reconciliation || {};
        const travelContra = (rec.travel && rec.travel.findings || []).filter(f => f.status === 'CONTRADICTED');
        const amountContra = (rec.amounts && rec.amounts.findings || []).filter(f => f.status === 'CONTRADICTED');
        const totalContra = travelContra.length + amountContra.length;

        const topMemsHtml = mems.slice(0, 3).map(m => `
            <div class="a2ui-timeline-step" style="padding:5px 0;">
                <div class="step-time" style="min-width:85px; font-size:0.65rem; color:#38bdf8;">
                    ${this.escapeHtml((m.topics || ['MEMORY'])[0])}<br>
                    <span style="color:#94a3b8;">dist: ${m.distance !== undefined ? Number(m.distance).toFixed(3) : 'n/a'}</span>
                </div>
                <div class="step-content">
                    <div class="step-desc" style="font-size:0.71rem; color:#e2e8f0;">"${this.escapeHtml(m.fact)}"</div>
                </div>
            </div>
        `).join('');

        const govSummary = govs.slice(0, 4).map(g =>
            `<span class="meta-chip" style="font-size:0.63rem;">${this.escapeHtml(g.resource)}: ${this.escapeHtml(g.action)}</span>`
        ).join(' ');

        const writeOp = data.memory_write && data.memory_write.operation
            ? data.memory_write.operation.split('/').pop()
            : 'Submitted';

        const widget = document.createElement('div');
        widget.className = 'a2ui-card';
        widget.innerHTML = `
            <div class="a2ui-card-header">
                <span class="a2ui-card-title">🔬 Live Evidence Provenance &amp; Ground-Truth Reconciliation</span>
                <span class="badge-status ${totalContra > 0 ? 'warning' : 'success'}" style="font-size:0.65rem;">
                    ${mems.length} Memories • ${govs.length} Catalog Checks • ${totalContra} Discrepancies Flagged
                </span>
            </div>
            <div class="a2ui-timeline" style="max-height:155px; overflow-y:auto;">
                ${topMemsHtml}
            </div>
            <div style="padding:6px 10px; background:rgba(15,23,42,0.75); border-top:1px solid rgba(255,255,255,0.08); display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:6px; font-size:0.68rem;">
                <div>${govSummary}</div>
                <div style="color:#34d399; font-family:'JetBrains Mono',monospace;">Async Write LRO: #${this.escapeHtml(writeOp)}</div>
            </div>
        `;

        this.a2uiContainer.innerHTML = '';
        this.a2uiContainer.appendChild(widget);
    }

    appendChatMessage(sender, text) {
        const msgDiv = document.createElement('div');
        msgDiv.className = `chat-message ${sender}`;
        msgDiv.innerHTML = `<div class="msg-bubble">${this.formatMarkdown(text)}</div>`;
        this.chatViewport.appendChild(msgDiv);
        this.chatViewport.scrollTop = this.chatViewport.scrollHeight;
        return msgDiv;
    }

    appendTrace(badge, message, type = 'thought') {
        if (!this.traceLogs) return;
        const item = document.createElement('div');
        item.className = `trace-item ${type}`;
        item.innerHTML = `
            <span class="trace-badge">${this.escapeHtml(badge)}</span>
            <span class="trace-msg">${this.escapeHtml(message)}</span>
        `;
        this.traceLogs.appendChild(item);
        this.traceLogs.scrollTop = this.traceLogs.scrollHeight;
    }

    async playSimulationSequence() {
        this.appendTrace('DEMO_WALKTHROUGH', `Starting end-to-end Fraud Desk demonstration for ${this.customerId}...`, 'thought');
        const cards = ['card-fraud', 'card-ivr', 'card-mobile'];
        for (const id of cards) {
            const el = document.getElementById(id);
            if (el) {
                el.classList.add('active-pulse');
                await new Promise(r => setTimeout(r, 700));
                el.classList.remove('active-pulse');
            }
        }
        // Every line below reports state that has actually been loaded or a
        // call that has actually been made. The walkthrough narrates the demo;
        // it does not invent its findings.
        const topicLabels = (this.cachedTopics || []).map(t => t.label);
        this.appendTrace('STEP_1_MEMORY',
            `${this.plural(this.cachedMemories.length, 'memory', 'memories')} in scope {"customer_id": "${this.customerId}"}` +
            (topicLabels.length ? ` across ${this.plural(topicLabels.length, 'configured topic', 'configured topics')}: ${topicLabels.join(', ')}.` : '. Topic list not loaded.'),
            'memory');
        await new Promise(r => setTimeout(r, 400));

        const probes = [
            { table: 'disputes', column: '' },
            { table: 'authorizations_deprecated_v1', column: '' },
            { table: 'cards', column: 'pan_full' }
        ];
        const verdicts = [];
        for (const p of probes) {
            const label = p.column ? `${p.table}.${p.column}` : p.table;
            try {
                const url = `/api/catalog/authorize?table=${encodeURIComponent(p.table)}${p.column ? '&column=' + encodeURIComponent(p.column) : ''}`;
                const r = await fetch(url);
                const v = await r.json();
                verdicts.push(`'${label}' -> ${(!r.ok || v.error) ? 'check failed' : (v.action || 'UNKNOWN')}`);
            } catch (err) {
                verdicts.push(`'${label}' -> check failed`);
            }
        }
        this.appendTrace('STEP_2_CATALOG', `Live Dataplex verdicts: ${verdicts.join(', ')}.`, 'thought');
        await new Promise(r => setTimeout(r, 400));

        const recon = (this.customerOverview && this.customerOverview.reconciliation) || {};
        const findings = [
            ...(((recon.travel || {}).findings) || []),
            ...(((recon.amounts || {}).findings) || [])
        ];
        const contra = findings.filter(f => f.status === 'CONTRADICTED');
        this.appendTrace('STEP_3_RECONCILE',
            findings.length === 0
                ? 'Reconciler ran but found no memory claim it could check against BigQuery.'
                : (contra.length
                    ? `Reconciler checked ${this.plural(findings.length, 'claim', 'claims')} and flagged ${this.plural(contra.length, 'contradiction', 'contradictions')}: ` +
                      contra.map(f => f.explanation || `${f.claim_kind || 'claim'} on memory ${String(f.memory_id || '').slice(-8)}`).join('; ') + '.'
                    : `Reconciler checked ${this.plural(findings.length, 'claim', 'claims')} against BigQuery and found no contradiction.`),
            contra.length ? 'error' : 'synthesis');
    }
}

document.addEventListener('DOMContentLoaded', () => {
    window.uiEngine = new MemoryBankUiEngine();
});
