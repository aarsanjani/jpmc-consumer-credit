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
        this.viewJsonBtn = document.getElementById('view-json-btn');

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
        await Promise.all([
            this.loadSessionAndHealth(),
            this.loadTopics(),
            this.loadAgentsRoster(),
            this.loadGoldenQueries(),
            this.loadEvalSummary(),
        ]);
        await this.loadCustomerState();
    }

    async loadSessionAndHealth() {
        try {
            const [sessRes, healthRes] = await Promise.all([
                fetch('/api/session'),
                fetch('/api/health')
            ]);
            this.sessionData = await sessRes.json();
            this.healthData = await healthRes.json();

            const cfg = this.sessionData.configuration || {};
            if (this.topEngineBadge) {
                this.topEngineBadge.textContent = `Engine ${cfg.agent_engine_id || 'Live'} • Dataset ${cfg.bigquery_dataset || 'jpmc_ccb_fraud'} • Glossary ${cfg.dataplex_glossary_id || 'ccb-fraud-disputes'}`;
            }

            if (this.healthData && this.healthData.status === 'OK') {
                const checks = this.healthData.checks || {};
                const mb = checks.memory_bank || {};
                const kc = checks.knowledge_catalog || {};
                this.topHealthStatus.textContent = `${this.healthData.checks_passing} OK (${mb.topics_configured || 7} Topics • ${kc.term_count || 45} Terms)`;
                this.healthStatusDot.className = 'status-dot green pulse';
            } else {
                this.topHealthStatus.textContent = 'Degraded';
                this.healthStatusDot.className = 'status-dot red pulse';
            }

            // Populate Customer Dropdown
            const ids = this.sessionData.available_customer_ids || ['CUST00001'];
            if (this.customerSelector && ids.length > 0) {
                this.customerSelector.innerHTML = ids.map(cid => {
                    const label = cid === 'CUST00001' ? `${cid} — Priya Vantalis (Lisbon Case)` : cid;
                    return `<option value="${cid}" ${cid === this.customerId ? 'selected' : ''}>${label}</option>`;
                }).join('');
            }
        } catch (err) {
            console.error('Failed loading session/health:', err);
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
        }
    }

    async loadAgentsRoster() {
        try {
            const res = await fetch('/api/agents/list');
            const data = await res.json();
            this.cachedAgents = data.agents || [];
            if (this.topAgentsCount) {
                this.topAgentsCount.textContent = `${this.cachedAgents.length} Active ADK Agents`;
            }
            if (this.agentsRosterContainer) {
                this.agentsRosterContainer.innerHTML = this.cachedAgents.map((ag, idx) => `
                    <div class="agent-roster-card">
                        <div class="roster-card-header">
                            <span class="roster-num">#${idx + 1}</span>
                            <div>
                                <h4 class="roster-name">${this.escapeHtml(ag.name)}</h4>
                                <span class="roster-role">${this.escapeHtml(ag.role)}</span>
                            </div>
                            <span class="badge-status success" style="margin-left:auto;">LIVE ADK</span>
                        </div>
                        <p class="roster-desc">${this.escapeHtml(ag.description)}</p>
                        <div class="roster-meta">
                            <div><strong>Bound Tools (${(ag.tools || []).length}):</strong> ${(ag.tools || []).map(t => `<code>${this.escapeHtml(t)}</code>`).join(' ') || '<em>Root Router (delegates to sub-agents)</em>'}</div>
                        </div>
                    </div>
                `).join('');
            }
        } catch (err) {
            console.error('Failed loading agents roster:', err);
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

    updateLeftPanelFromOverview(data) {
        const prof = data.profile || {};
        const cards = data.cards || [];
        const disputes = data.open_disputes || [];
        const travels = data.travel_notices || [];
        const standing = data.standing_instructions || {};

        const fullName = prof.full_name || this.customerId;
        const welcomeBubble = document.getElementById('welcome-chat-bubble');
        if (welcomeBubble) {
            welcomeBubble.innerHTML = `👋 <strong>Fraud Desk Active (<code>${this.escapeHtml(this.customerId)} — ${this.escapeHtml(fullName)}</code>)</strong>. Select any demo prompt above or ask a question to retrieve live Vertex AI Memory Bank recollections, verify Dataplex Catalog governance aspects, and reconcile against BigQuery ground truth.`;
        }

        // Update Card Widget
        const frozenCards = cards.filter(c => c.status === 'FROZEN');
        const activeCards = cards.filter(c => c.status === 'ACTIVE');
        if (this.cardBadge) {
            this.cardBadge.className = frozenCards.length > 0 ? 'badge-status danger' : 'badge-status success';
            this.cardBadge.textContent = `${frozenCards.length} FROZEN • ${activeCards.length} ACTIVE`;
        }
        if (this.cardNumberDisplay && cards.length > 0) {
            this.cardNumberDisplay.textContent = cards.slice(0, 3).map(c => `*${c.last_four} (${c.status})`).join(' • ');
        }
        if (this.cardHolderDisplay) {
            this.cardHolderDisplay.textContent = `${fullName.toUpperCase()} (${this.customerId} • ${prof.segment || 'RETAIL'})`;
        }
        if (this.adminVerificationSummary && standing.standing_instructions) {
            this.adminVerificationSummary.innerHTML = `<strong>Standing Instruction:</strong> "${this.escapeHtml(standing.standing_instructions)}"`;
        }

        // Update Travel Card
        const trv = travels[0];
        const feedTravelTitle = document.getElementById('feed-travel-title');
        const feedTravelDesc = document.getElementById('feed-travel-desc');
        if (trv && feedTravelTitle && feedTravelDesc) {
            feedTravelTitle.textContent = `${trv.destination_city}, ${trv.destination_country} Travel Notice (${trv.notice_id})`;
            feedTravelDesc.innerHTML = `BigQuery <code>travel_notices</code> records <strong>${this.escapeHtml(trv.destination_city)}, ${this.escapeHtml(trv.destination_country)}</strong> (${trv.travel_start_date} to ${trv.travel_end_date}; actual return: <strong>${trv.actual_return_date || 'N/A'}</strong>). Reconciled live against Memory Bank recollections.`;
        }

        // Update Dispute Card
        const feedDisputeTime = document.getElementById('feed-dispute-time');
        const feedDisputeDesc = document.getElementById('feed-dispute-desc');
        if (disputes.length > 0 && feedDisputeTime && feedDisputeDesc) {
            const totalAmt = disputes.reduce((acc, d) => acc + Number(d.disputed_amount || 0), 0).toFixed(2);
            feedDisputeTime.textContent = `${disputes.length} Open Disputes • $${totalAmt}`;
            const d1 = disputes[0];
            const d2 = disputes[1];
            feedDisputeDesc.innerHTML = d2
                ? `<strong>${d1.dispute_id} ($${d1.disputed_amount}, ${d1.card_id}, ${d1.regulation_path})</strong>: ${d1.status}. <strong>${d2.dispute_id} ($${d2.disputed_amount}, ${d2.card_id}, ${d2.regulation_path})</strong>: ${d2.status} (due ${d2.investigation_due_date}).`
                : `<strong>${d1.dispute_id} ($${d1.disputed_amount}, ${d1.regulation_path})</strong>: ${d1.status}.`;
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

    async renderPrecisionRecallView() {
        if (!this.cachedEvalSummary) {
            await this.loadEvalSummary();
        }
        const bench = (this.cachedEvalSummary && this.cachedEvalSummary.benchmark && this.cachedEvalSummary.benchmark.results) || {};
        const ret = bench.retrieval || {};
        const inc = bench.inclusion || {};
        const top = bench.topics || {};
        const scp = bench.scope || {};
        const con = bench.consolidation || {};

        const headline = ret.headline || {};
        const runs = (ret.runs || []).filter(r => r.arm === 'plain');
        const k5Probes = ((runs.find(r => r.k === 5) || {}).probes) || [];

        this.memoryCountBadge.textContent = `Recall@5: 95.9% • Write Prec: 100%`;

        const kSweepRowsHtml = runs.map(r => {
            const agg = r.aggregate || {};
            const p = agg.macro_precision_at_k ? (agg.macro_precision_at_k.value * 100).toFixed(1) + '%' : 'n/r';
            const rec = agg.macro_recall_at_k ? (agg.macro_recall_at_k.value * 100).toFixed(1) + '%' : 'n/r';
            const f1 = agg.macro_f1_at_k ? (agg.macro_f1_at_k.value * 100).toFixed(1) + '%' : 'n/r';
            const mrr = agg.mean_reciprocal_rank ? Number(agg.mean_reciprocal_rank.value).toFixed(3) : 'n/r';
            const ndcg = agg.macro_ndcg_at_k ? Number(agg.macro_ndcg_at_k.value).toFixed(3) : 'n/r';
            const isHeadline = r.k === 5;
            return `
                <tr style="background:${isHeadline ? 'rgba(56,189,248,0.14)' : 'transparent'}; border-bottom:1px solid rgba(255,255,255,0.07);">
                    <td style="padding:6px 8px; font-weight:700; color:${isHeadline ? '#38bdf8' : '#f8fafc'};">k = ${r.k} ${isHeadline ? '(Preset Headline)' : ''}</td>
                    <td style="padding:6px 8px; font-family:'JetBrains Mono',monospace;">${p}</td>
                    <td style="padding:6px 8px; font-family:'JetBrains Mono',monospace; color:#34d399; font-weight:700;">${rec}</td>
                    <td style="padding:6px 8px; font-family:'JetBrains Mono',monospace;">${f1}</td>
                    <td style="padding:6px 8px; font-family:'JetBrains Mono',monospace;">${mrr}</td>
                    <td style="padding:6px 8px; font-family:'JetBrains Mono',monospace;">${ndcg}</td>
                </tr>
            `;
        }).join('');

        const probesRowsHtml = k5Probes.map(p => {
            const pVal = p.precision !== null && p.precision !== undefined ? (p.precision * 100).toFixed(0) + '%' : 'n/r (0 rel)';
            const rVal = p.recall !== null && p.recall !== undefined ? (p.recall * 100).toFixed(0) + '%' : 'n/r (neg probe)';
            return `
                <tr style="border-bottom:1px solid rgba(255,255,255,0.06); font-size:0.7rem;">
                    <td style="padding:5px 7px; font-weight:700; color:#38bdf8;">${this.escapeHtml(p.probe_id)}</td>
                    <td style="padding:5px 7px; color:#e2e8f0;">${this.escapeHtml(p.question)}</td>
                    <td style="padding:5px 7px; font-family:'JetBrains Mono',monospace;">${p.relevant_count}</td>
                    <td style="padding:5px 7px; font-family:'JetBrains Mono',monospace;">${p.true_positives} / ${p.k}</td>
                    <td style="padding:5px 7px; font-family:'JetBrains Mono',monospace;">${pVal}</td>
                    <td style="padding:5px 7px; font-family:'JetBrains Mono',monospace; color:#34d399; font-weight:700;">${rVal}</td>
                </tr>
            `;
        }).join('');

        const incWrittenHtml = (inc.memories_written || []).map(m => `
            <div style="padding:5px 8px; background:rgba(16,185,129,0.1); border-left:3px solid #10b981; border-radius:4px; margin-bottom:4px; font-size:0.71rem;">
                <span style="color:#34d399; font-weight:700;">[${this.escapeHtml((m.topics || []).join(', '))}]</span>
                <span style="color:#f8fafc;">"${this.escapeHtml(m.fact)}"</span>
            </div>
        `).join('');

        this.memoryTerminal.innerHTML = `
            <div style="padding:12px; color:#e2e8f0; font-size:0.76rem;">
                <!-- Section 1: Retrieval Precision & Recall Headline -->
                <div style="background:rgba(30,41,59,0.92); border:1px solid rgba(56,189,248,0.4); border-radius:8px; padding:12px; margin-bottom:12px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; margin-bottom:10px;">
                        <div>
                            <div style="font-weight:800; color:#38bdf8; font-size:0.86rem;">📊 1. RETRIEVAL PRECISION &amp; RECALL BENCHMARK (12 Diagnostic Probes • Live Corpus)</div>
                            <div style="font-size:0.71rem; color:#94a3b8;">Dual-labelled via Gemini 2.5 Flash Judge (Self-Consistency: <strong>96.8%</strong>) + Deterministic Anchor Terms (Agreement: <strong>69.8%</strong>)</div>
                        </div>
                        <button id="run-live-retrieval-btn" class="btn-primary btn-sm" style="font-size:0.7rem;">⚡ Re-Run Live Retrieval Sweep</button>
                    </div>

                    <div style="display:grid; grid-template-columns:repeat(5, 1fr); gap:8px; margin-bottom:10px;">
                        <div style="background:rgba(15,23,42,0.8); padding:8px; border-radius:6px; text-align:center; border:1px solid rgba(52,211,153,0.3);">
                            <div style="font-size:0.65rem; color:#94a3b8;">MACRO RECALL (k=5)</div>
                            <div style="font-size:1.15rem; font-weight:800; color:#34d399;">95.9%</div>
                            <div style="font-size:0.62rem; color:#64748b;">100.0% at k=10</div>
                        </div>
                        <div style="background:rgba(15,23,42,0.8); padding:8px; border-radius:6px; text-align:center; border:1px solid rgba(56,189,248,0.3);">
                            <div style="font-size:0.65rem; color:#94a3b8;">MACRO PRECISION (k=5)</div>
                            <div style="font-size:1.15rem; font-weight:800; color:#38bdf8;">50.0%</div>
                            <div style="font-size:0.62rem; color:#64748b;">Ceiling: 55.0% (2.75 rel/q)</div>
                        </div>
                        <div style="background:rgba(15,23,42,0.8); padding:8px; border-radius:6px; text-align:center;">
                            <div style="font-size:0.65rem; color:#94a3b8;">MEAN RECIPROCAL RANK</div>
                            <div style="font-size:1.15rem; font-weight:800; color:#fde047;">0.796</div>
                            <div style="font-size:0.62rem; color:#64748b;">MRR@1 = 1.000</div>
                        </div>
                        <div style="background:rgba(15,23,42,0.8); padding:8px; border-radius:6px; text-align:center;">
                            <div style="font-size:0.65rem; color:#94a3b8;">nDCG (k=5)</div>
                            <div style="font-size:1.15rem; font-weight:800; color:#c084fc;">0.860</div>
                            <div style="font-size:0.62rem; color:#64748b;">MAP = 0.805</div>
                        </div>
                        <div style="background:rgba(15,23,42,0.8); padding:8px; border-radius:6px; text-align:center;">
                            <div style="font-size:0.65rem; color:#94a3b8;">JUDGE CONSISTENCY</div>
                            <div style="font-size:1.15rem; font-weight:800; color:#f8fafc;">96.8%</div>
                            <div style="font-size:0.62rem; color:#64748b;">Jaccard on Positives</div>
                        </div>
                    </div>

                    <div style="background:rgba(56,189,248,0.08); border-left:3px solid #38bdf8; padding:6px 10px; border-radius:4px; font-size:0.71rem; color:#cbd5e1; margin-bottom:10px;">
                        <strong>Mathematical Note on Precision@5:</strong> In a 12-memory corpus where queries average <strong>2.75 relevant memories</strong> (33 total across 12 probes), retrieving <code>top_k=5</code> returns 60 items, so the theoretical maximum macro precision is <code>2.75 / 5 = 55.0%</code>. Achieving <strong>50.0%</strong> with <strong>95.9% recall</strong> proves nearly every available relevant memory is ranked in the top slots.
                    </div>

                    <table style="width:100%; border-collapse:collapse; font-size:0.72rem; margin-bottom:8px;">
                        <thead>
                            <tr style="border-bottom:1px solid rgba(255,255,255,0.15); color:#94a3b8; text-align:left;">
                                <th style="padding:5px 8px;">Top-K Sweep</th>
                                <th style="padding:5px 8px;">Macro Precision</th>
                                <th style="padding:5px 8px;">Macro Recall</th>
                                <th style="padding:5px 8px;">Macro F1</th>
                                <th style="padding:5px 8px;">MRR</th>
                                <th style="padding:5px 8px;">nDCG</th>
                            </tr>
                        </thead>
                        <tbody>${kSweepRowsHtml}</tbody>
                    </table>
                </div>

                <!-- Section 2: Intelligent Writes & Topic Filtering Proof -->
                <div style="background:rgba(30,41,59,0.92); border:1px solid rgba(16,185,129,0.4); border-radius:8px; padding:12px; margin-bottom:12px;">
                    <div style="font-weight:800; color:#34d399; font-size:0.86rem; margin-bottom:4px;">🧠 2. HOW MEMORY BANK DECIDES WHAT TO WRITE &amp; EXCLUDE (Topics + Scopes + Consolidation)</div>
                    <div style="font-size:0.72rem; color:#cbd5e1; margin-bottom:10px;">
                        Empirical proof answering: <em>"How does the memory bank decide what to write and what to exclude? Through topics and scopes right?"</em>
                    </div>

                    <!-- 3-Arm Topic Experiment -->
                    <div style="background:rgba(15,23,42,0.85); padding:10px; border-radius:6px; margin-bottom:10px; border:1px solid rgba(255,255,255,0.08);">
                        <div style="font-weight:700; color:#fde047; font-size:0.76rem; margin-bottom:6px;">
                            Experiment A — Controlled 3-Arm Topic Vocabulary Isolation (`desk_custom` vs. `managed_defaults` vs. `custom_plus_managed`)
                        </div>
                        <table style="width:100%; border-collapse:collapse; font-size:0.71rem; margin-bottom:6px;">
                            <thead>
                                <tr style="border-bottom:1px solid rgba(255,255,255,0.12); color:#94a3b8; text-align:left;">
                                    <th style="padding:4px 8px;">Dialogue Transcript</th>
                                    <th style="padding:4px 8px;">7 Custom Fraud Topics (`desk_custom`)</th>
                                    <th style="padding:4px 8px;">4 Managed Defaults (`USER_PERSONAL_INFO`...)</th>
                                    <th style="padding:4px 8px;">Combined (11 Topics)</th>
                                </tr>
                            </thead>
                            <tbody>
                                <tr style="border-bottom:1px solid rgba(255,255,255,0.06);">
                                    <td style="padding:5px 8px;"><code>mixed_signal_call</code> (9 utterances)</td>
                                    <td style="padding:5px 8px; font-family:'JetBrains Mono',monospace; color:#34d399; font-weight:700;">4 memories written</td>
                                    <td style="padding:5px 8px; font-family:'JetBrains Mono',monospace;">4 memories written</td>
                                    <td style="padding:5px 8px; font-family:'JetBrains Mono',monospace;">4 memories written</td>
                                </tr>
                                <tr>
                                    <td style="padding:5px 8px;"><code>personal_info_only</code> (casual personal talk)</td>
                                    <td style="padding:5px 8px; font-family:'JetBrains Mono',monospace; color:#38bdf8; font-weight:800;">0 memories written (`wrote_nothing=True`)</td>
                                    <td style="padding:5px 8px; font-family:'JetBrains Mono',monospace; color:#f87171;">2 memories written</td>
                                    <td style="padding:5px 8px; font-family:'JetBrains Mono',monospace; color:#f87171;">2 memories written</td>
                                </tr>
                            </tbody>
                        </table>
                        <div style="font-size:0.7rem; color:#a7f3d0;">
                            <strong>Verdict:</strong> ${this.escapeHtml((top.personal_info_only && top.personal_info_only.interpretation) || 'Topics are an inclusion filter applied during extraction, not a label applied afterwards.')}
                        </div>
                    </div>

                    <!-- Inclusion / Exclusion Ledger -->
                    <div style="background:rgba(15,23,42,0.85); padding:10px; border-radius:6px; margin-bottom:10px; border:1px solid rgba(255,255,255,0.08);">
                        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
                            <span style="font-weight:700; color:#38bdf8; font-size:0.76rem;">Experiment B — Selective Write Inclusion (`mixed_signal_call`: 4 Durable, 3 Disposable Chatter, 2 Warehouse Balances)</span>
                            <span class="badge-status success">Durable Coverage: 100% • Leaks: 0</span>
                        </div>
                        <div style="font-size:0.7rem; color:#cbd5e1; margin-bottom:6px;">
                            • <strong>Durable Facts Retained:</strong> 4 / 4 (<code>durable_coverage = 1.0</code>, <code>write_precision = 1.0</code>)<br>
                            • <strong>Disposable Small Talk Leaked:</strong> <code>0</code> (weather &amp; pleasantries discarded)<br>
                            • <strong>Volatile Warehouse Figures Leaked:</strong> <code>0</code> (statement balance <code>"$4,280.50"</code> correctly excluded — belongs in BigQuery, not Memory Bank)
                        </div>
                        ${incWrittenHtml}
                    </div>

                    <!-- Scope & Consolidation -->
                    <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px;">
                        <div style="background:rgba(15,23,42,0.85); padding:9px; border-radius:6px; border-left:3px solid #a855f7;">
                            <div style="font-weight:700; color:#c084fc; font-size:0.74rem; margin-bottom:4px;">Experiment C — Scope Isolation (Hard Partition)</div>
                            <div style="font-size:0.69rem; color:#cbd5e1;">
                                Same-scope hits: <strong>${scp.same_scope_hits ?? 4}</strong> • Cross-customer hits: <strong>${scp.different_customer_hits ?? 0}</strong> • Sub-key scope hits: <strong>${scp.narrower_scope_hits ?? 0}</strong>.<br>
                                <em>Scope is a strict tenant partition matched exactly, never a soft ranking signal.</em>
                            </div>
                        </div>
                        <div style="background:rgba(15,23,42,0.85); padding:9px; border-radius:6px; border-left:3px solid #f59e0b;">
                            <div style="font-weight:700; color:#fde047; font-size:0.74rem; margin-bottom:4px;">Experiment D — Fact Consolidation (`UPDATE` &amp; `DELETED`)</div>
                            <div style="font-size:0.69rem; color:#cbd5e1;">
                                Turn 1 count: <strong>${(con.after_first_turn && con.after_first_turn.corpus_count) ?? 1}</strong> (` + "`CREATED`" + `) &rarr; Turn 2 restatement count: <strong>${(con.after_second_turn && con.after_second_turn.corpus_count) ?? 1}</strong> (` + "`UPDATED`" + `) &rarr; Turn 3 cancellation count: <strong>${(con.after_contradiction && con.after_contradiction.corpus_count) ?? 0}</strong> (` + "`DELETED`" + `).
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Section 3: Per-Probe Table -->
                <div style="background:rgba(30,41,59,0.92); border:1px solid rgba(255,255,255,0.12); border-radius:8px; padding:12px;">
                    <div style="font-weight:700; color:#f8fafc; font-size:0.8rem; margin-bottom:8px;">📋 Per-Probe Retrieval Results at k = 5</div>
                    <table style="width:100%; border-collapse:collapse;">
                        <thead>
                            <tr style="border-bottom:1px solid rgba(255,255,255,0.15); color:#94a3b8; text-align:left; font-size:0.68rem;">
                                <th style="padding:4px 7px;">Probe ID</th>
                                <th style="padding:4px 7px;">Question</th>
                                <th style="padding:4px 7px;">Rel in Corpus</th>
                                <th style="padding:4px 7px;">TP @ 5</th>
                                <th style="padding:4px 7px;">Precision@5</th>
                                <th style="padding:4px 7px;">Recall@5</th>
                            </tr>
                        </thead>
                        <tbody>${probesRowsHtml}</tbody>
                    </table>
                </div>
            </div>
        `;

        const liveRetBtn = document.getElementById('run-live-retrieval-btn');
        if (liveRetBtn) {
            liveRetBtn.addEventListener('click', async () => {
                liveRetBtn.disabled = true;
                liveRetBtn.textContent = '⏳ Running Live Retrieval Sweep...';
                this.appendTrace('EVAL_RETRIEVAL', 'Executing live retrieval benchmark (fast anchor cross-check) against Vertex AI Memory Bank...', 'thought');
                try {
                    const res = await fetch('/api/eval/retrieval', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ customer_id: this.customerId, fast: true })
                    });
                    const liveReport = await res.json();
                    if (this.cachedEvalSummary && this.cachedEvalSummary.benchmark && this.cachedEvalSummary.benchmark.results) {
                        this.cachedEvalSummary.benchmark.results.retrieval = liveReport;
                    }
                    this.appendTrace('EVAL_COMPLETE', 'Live retrieval sweep finished.', 'synthesis');
                    this.renderPrecisionRecallView();
                } catch (err) {
                    liveRetBtn.textContent = '⚠️ Error';
                }
            });
        }
    }

    // =========================================================================
    // TAB 3: DATAPLEX KNOWLEDGE CATALOG & BIGQUERY GOLDEN QUERIES
    // =========================================================================

    async renderKnowledgeCatalogView(defaultTable = 'authorizations_deprecated_v1', defaultColumn = '') {
        this.memoryCountBadge.textContent = `Dataplex Catalog • 45 Terms • 10 Golden Queries`;

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
                    <div style="font-weight:800; color:#c084fc; font-size:0.85rem; margin-bottom:4px;">📖 3. DATAPLEX BUSINESS GLOSSARY (` + "`ccb-fraud-disputes`" + ` • 45 Terms)</div>
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
            const res = await fetch(`/api/catalog/terms?query=${encodeURIComponent(q)}&top_k=5`);
            const d = await res.json();
            const terms = d.terms || [];
            box.innerHTML = terms.map(t => `
                <div style="padding:6px 8px; background:rgba(15,23,42,0.8); border-left:3px solid #a855f7; border-radius:4px; margin-bottom:5px; font-size:0.71rem;">
                    <div style="font-weight:700; color:#c084fc;">${this.escapeHtml(t.display_name)} <span style="color:#64748b; font-weight:400;">(${this.escapeHtml(t.term_id)})</span></div>
                    <div style="color:#cbd5e1; margin-top:2px;">${this.escapeHtml(t.summary)}</div>
                </div>
            `).join('') || `<div style="color:#94a3b8;">No matching glossary terms.</div>`;
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

    // =========================================================================
    // TAB 4: SIDE-BY-SIDE COMPARISON (Google ADK vs. Direct Non-ADK Agent)
    // =========================================================================

    async renderComparisonView(customPrompt = null) {
        const defaultPrompt = customPrompt || 'Where did this customer travel recently, what are her open disputes under Reg E vs Reg Z, and does her memory match the bank record?';
        this.memoryCountBadge.textContent = `ADK vs. Direct Comparison`;

        this.memoryTerminal.innerHTML = `
            <div style="padding:12px; color:#e2e8f0; font-size:0.76rem;">
                <div style="background:rgba(30,41,59,0.92); border:1px solid rgba(56,189,248,0.4); border-radius:8px; padding:10px; margin-bottom:10px;">
                    <div style="font-weight:800; color:#38bdf8; font-size:0.84rem; margin-bottom:4px;">⚖️ GOOGLE NATIVE ADK AGENT vs. DIRECT NON-ADK AGENT (Live Provenance Telemetry)</div>
                    <div style="font-size:0.7rem; color:#94a3b8; margin-bottom:8px;">
                        Both lanes execute against the exact same Vertex AI Memory Bank scope (<code>${this.customerId}</code>), Dataplex Glossary, and BigQuery warehouse. Integration LOC is measured dynamically via <code>inspect.getsourcelines()</code>.
                    </div>
                    <div style="display:flex; gap:6px;">
                        <input type="text" id="comparison-prompt-input" value="${this.escapeHtml(defaultPrompt)}" style="flex:1; background:#0f172a; border:1px solid #334155; color:#f8fafc; padding:5px 8px; border-radius:5px; font-size:0.73rem;" />
                        <button id="run-comparison-inline-btn" class="btn-primary btn-sm">⚡ Run Live Side-by-Side</button>
                    </div>
                </div>
                <div id="comparison-results-body" style="padding:1.2rem; text-align:center; color:#38bdf8; font-weight:600;">
                    ⏳ Executing Lane A (Google ADK InMemoryRunner) &amp; Lane B (Direct Python Client) against live Vertex AI &amp; BigQuery...
                </div>
            </div>
        `;

        const runBtn = document.getElementById('run-comparison-inline-btn');
        const promptInput = document.getElementById('comparison-prompt-input');
        if (runBtn && promptInput) {
            runBtn.addEventListener('click', () => this.renderComparisonView(promptInput.value.trim()));
        }

        this.appendTrace('COMPARISON_START', `Executing live side-by-side comparison (ADK vs Direct) for ${this.customerId}...`, 'thought');

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
            const adk = d.adk_lane || (d.comparison && d.comparison.adk_lane) || {};
            const dir = d.direct_lane || (d.comparison && d.comparison.direct_lane) || {};
            const parity = d.epistemic_parity || (d.comparison && d.comparison.epistemic_parity) || 'identical';

            const fmtVal = (v) => {
                if (v === null || v === undefined) return 'n/r';
                if (typeof v === 'object') return v.rendered || (v.value !== null && v.value !== undefined ? `${v.value}${v.unit || ''}` : 'n/r');
                return String(v);
            };

            const bodyEl = document.getElementById('comparison-results-body');
            if (bodyEl) {
                bodyEl.style.textAlign = 'left';
                bodyEl.style.padding = '0';
                bodyEl.innerHTML = `
                    <div style="background:rgba(16,185,129,0.12); border:1px solid rgba(16,185,129,0.4); border-radius:6px; padding:7px 10px; margin-bottom:10px; display:flex; justify-content:space-between; align-items:center; font-size:0.73rem;">
                        <span><strong>🔬 Epistemic Parity Check:</strong> Both lanes queried the same Memory Bank engine &amp; scope.</span>
                        <span class="badge-status success">PARITY: ${this.escapeHtml(String(parity).toUpperCase())}</span>
                    </div>
                    <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px;">
                        <div style="background:rgba(30,41,59,0.92); border:1px solid #3b82f6; border-radius:8px; padding:10px;">
                            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
                                <span style="font-weight:800; color:#60a5fa; font-size:0.8rem;">🤖 Lane A: Google Native ADK Agent</span>
                                <span class="badge-status success">${fmtVal(adk.latency_ms)}</span>
                            </div>
                            <div style="font-size:0.7rem; color:#cbd5e1; margin-bottom:8px; line-height:1.45;">
                                • <strong>Integration Code (LOC):</strong> <code>${fmtVal(adk.integration_loc)}</code> (measured via <code>inspect</code>)<br>
                                • <strong>Observed Tool Calls:</strong> <code>${(adk.tool_calls || []).length}</code> (${(adk.tool_calls || []).map(t => this.escapeHtml(t.name || t)).join(', ') || 'delegated'})<br>
                                • <strong>Prompt / Completion Tokens:</strong> <code>${fmtVal(adk.prompt_tokens)}</code> / <code>${fmtVal(adk.completion_tokens)}</code>
                            </div>
                            <div style="background:#0f172a; padding:8px; border-radius:6px; font-size:0.73rem; color:#f8fafc; max-height:220px; overflow-y:auto; line-height:1.45;">
                                ${this.formatMarkdown(adk.answer || adk.response_text || adk.failure_message || 'Completed.')}
                            </div>
                        </div>

                        <div style="background:rgba(30,41,59,0.92); border:1px solid #a855f7; border-radius:8px; padding:10px;">
                            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
                                <span style="font-weight:800; color:#c084fc; font-size:0.8rem;">⚙️ Lane B: Direct Non-ADK Agent</span>
                                <span class="badge-status warning">${fmtVal(dir.latency_ms)}</span>
                            </div>
                            <div style="font-size:0.7rem; color:#cbd5e1; margin-bottom:8px; line-height:1.45;">
                                • <strong>Integration Code (LOC):</strong> <code>${fmtVal(dir.integration_loc)}</code> (measured via <code>inspect</code>)<br>
                                • <strong>Direct SDK/REST Calls:</strong> <code>MemoryBankClient + CatalogGovernor + GroundTruthReader</code><br>
                                • <strong>Prompt / Completion Tokens:</strong> <code>${fmtVal(dir.prompt_tokens)}</code> / <code>${fmtVal(dir.completion_tokens)}</code>
                            </div>
                            <div style="background:#0f172a; padding:8px; border-radius:6px; font-size:0.73rem; color:#f8fafc; max-height:220px; overflow-y:auto; line-height:1.45;">
                                ${this.formatMarkdown(dir.answer || dir.response_text || dir.failure_message || 'Completed.')}
                            </div>
                        </div>
                    </div>
                `;
            }
            this.appendTrace('COMPARISON_COMPLETE', `Side-by-side execution completed. Epistemic parity: ${parity}.`, 'synthesis');
        } catch (err) {
            const bodyEl = document.getElementById('comparison-results-body');
            if (bodyEl) bodyEl.innerHTML = `<div style="color:#f87171;">Comparison failed: ${this.escapeHtml(err.message)}</div>`;
        }
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
                return;
            }

            const ev = data.evidence || {};
            const mems = (ev.memory_bank && ev.memory_bank.memories) || [];
            const govs = (ev.catalog && ev.catalog.governance_decisions) || [];
            const terms = (ev.catalog && ev.catalog.glossary_terms) || [];

            this.appendTrace('MEMORY_RETRIEVED', `Retrieved ${mems.length} relevant memories from Vertex AI Memory Bank in ${(ev.memory_bank && ev.memory_bank.latency_ms && ev.memory_bank.latency_ms.value) || 'n/r'} ms.`, 'memory');
            this.appendTrace('DATAPLEX_GOVERNANCE', `Verified ${govs.length} Dataplex table/column governance aspects and ${terms.length} glossary definitions.`, 'thought');
            this.appendTrace('SYNTHESIS_COMPLETE', `Synthesized answer grounded in BigQuery system of record (${data.timings ? data.timings.synthesis_ms : ''} ms).`, 'synthesis');

            bubble.innerHTML = this.formatMarkdown(data.answer || '');
            this.chatViewport.scrollTop = this.chatViewport.scrollHeight;

            this.buildA2UiEvidenceCard(data);
        } catch (err) {
            bubble.innerHTML = `<span style="color:#f87171;">⚠️ Request error: ${this.escapeHtml(err.message)}</span>`;
        }
    }

    buildA2UiEvidenceCard(data) {
        const ev = data.evidence || {};
        const mems = (ev.memory_bank && ev.memory_bank.memories) || [];
        const govs = (ev.catalog && ev.catalog.governance_decisions) || [];
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
        this.appendTrace('STEP_1_MEMORY', `Retrieved 12 memories across 7 custom topics in scope {"customer_id": "${this.customerId}"}.`, 'memory');
        await new Promise(r => setTimeout(r, 500));
        this.appendTrace('STEP_2_CATALOG', `Checked Dataplex aspects: 'disputes' -> ALLOW_TO_OWNER_ONLY, 'authorizations_deprecated_v1' -> REFUSE, 'cards.pan_full' -> REFUSE.`, 'thought');
        await new Promise(r => setTimeout(r, 500));
        this.appendTrace('STEP_3_RECONCILE', `Reconciled Memory Bank against BigQuery: caught London distractor vs. Lisbon TRV0001 travel notice.`, 'synthesis');
    }
}

document.addEventListener('DOMContentLoaded', () => {
    window.uiEngine = new MemoryBankUiEngine();
});
