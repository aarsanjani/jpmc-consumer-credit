# The 8 CCB Fraud & Disputes Desk Agents

This document specifies the 8 agents defined in `backend/adk_agents.py` (`create_consumer_credit_synthesizer_agent` at L1685 and `list_all_agents` at L1885), their assigned roles, their Vertex AI Memory Bank topic scopes, their Dataplex Catalog governance rules, their authorized BigQuery Golden Queries, and their bound ADK tools.

---

## Topology Overview

- **Hub (`08`)**: `consumer_credit_synthesizer_agent` (`LEAD_SYNTHESIZER`) — the single agent that speaks to the customer/operator, synthesizes cross-channel evidence, and stages card unlock actions.
- **5 Channel Producer Spokes (`01`–`05`)**:
  - `01 · fraud_monitoring_agent` (`FRAUD_DETECTION` / `FRAUD_OPS_OUTBOUND`)
  - `02 · telephony_ivr_agent` (`TELEPHONY_IVR`)
  - `03 · mobile_app_agent` (`MOBILE_APP`)
  - `04 · web_portal_agent` (`WEB_PORTAL`)
  - `05 · branch_support_agent` (`BRANCH_SUPPORT`)
- **2 Governance & Audit Spokes (`06`–`07`)**:
  - `06 · claim_veracity_validator_agent` (`PRE_WRITE_VALIDATOR`)
  - `07 · memory_bank_audit_agent` (`CONSOLIDATED_AUDITOR · ASYNC JOB`)

All 8 agents are constructed as `google.adk.agents.LlmAgent` instances and executed on demand via `google.adk.runners.InMemoryRunner` (`run_adk_agent_live()` in `backend/adk_agents.py` and `POST /api/agents/<agent_id>/run` in `backend/app.py`).

---

## 01 · `fraud_monitoring_agent`
**Fraud Velocity & Geo-Anomaly Specialist (`CHANNEL_PRODUCER · FRAUD_OPS_OUTBOUND`)**

Monitors cross-border velocity anomalies, outbound fraud alerts (`FRAUD_OPS_OUTBOUND`), and stolen-card POS attempts using Vertex AI Memory Bank (`travel_context`, `fraud_history`), Dataplex Catalog (`discover_sibling_tables` & `check_table`/`check_column` to refuse `authorizations_deprecated_v1` and `cards.pan_full`), and BigQuery Golden Queries `03` & `04` before staging card blocks or escalating to the synthesizer.

- **🧠 Memory Bank Topics Scoped**: `travel_context` (`TRAVEL_STATUS`) · `fraud_history` (`CARD_LIFECYCLE`) · `spending_directives` (`SPENDING_CONTEXT`)
- **🛡️ Dataplex Catalog Governance**: `authorizations (ACTIVE)` · `agg_merchant_velocity_daily (ACTIVE)` · `authorizations_deprecated_v1 (REFUSE · DEPRECATED)` · `cards.pan_full (REFUSE · RESTRICTED PII)` · `cards.pan_masked (ALLOW_TO_OWNER_ONLY)` · `authorizations.is_fraud (REFUSE · Triage Answer Key)`
- **🏛️ Authorized BigQuery Golden Queries (`6`)**: `02_recent_authorizations_for_card` · `03_suspicious_activity_on_card_window` · `04_geo_impossible_travel` · `05_merchant_velocity_spike` · `06_cnp_burst_detection` · `12_fraud_rate_by_merchant_category`
- **🔧 Bound ADK Tools (`4`)**: `query_fraud_velocity_alerts` · `golden_03_suspicious_activity_on_card_window` · `golden_04_geo_impossible_travel` · `execute_fraud_card_control_or_escalate`

---

## 02 · `telephony_ivr_agent`
**Contact Center IVR & Inbound Call Specialist (`CHANNEL_PRODUCER · TELEPHONY_IVR`)**

Ingests and reads inbound customer call transcripts and DTMF step-up authentication turns from the `TELEPHONY_IVR` channel in Vertex AI Memory Bank, and cross-checks reported declined charges against the BigQuery authorization ledger via Golden Query `02`.

- **🧠 Memory Bank Topics Scoped**: `identity_verification` (`STEP_UP_AUTH`) · `travel_context` (`TRAVEL_STATUS`) · `fraud_history` (`CALL_TRANSCRIPT_FACTS`)
- **🛡️ Dataplex Catalog Governance**: `authorizations (ACTIVE)` · `cards (ACTIVE)` · `merchants (ACTIVE)` · `authorizations_deprecated_v1 (REFUSE · DEPRECATED)` · `cards.pan_full (REFUSE · RESTRICTED PII)` · `cards.pan_masked (ALLOW_TO_OWNER_ONLY)`
- **🏛️ Authorized BigQuery Golden Queries (`1`)**: `02_recent_authorizations_for_card`
- **🔧 Bound ADK Tools (`2`)**: `query_ivr_call_records` · `golden_02_recent_authorizations_for_card`

---

## 03 · `mobile_app_agent`
**Mobile Banking & Digital Wallet Token Specialist (`CHANNEL_PRODUCER · MOBILE_APP`)**

Tracks in-app push approvals, biometric login events, and Apple Pay / Google Pay device token provisioning on the `MOBILE_APP` channel, verifying whether any device wallet token (`token_id`) can still spend on a frozen physical card using Golden Query `10`.

- **🧠 Memory Bank Topics Scoped**: `device_trust` (`DEVICE_WALLET_TOKENS`) · `spending_directives` (`RECURRING_MERCHANT_RULES`) · `travel_context` (`MOBILE_GEO_CHECKIN`)
- **🛡️ Dataplex Catalog Governance**: `wallet_tokens (ACTIVE)` · `devices (ACTIVE)` · `cards (ACTIVE)` · `cards.pan_full (REFUSE · RESTRICTED PII)` · `cards.pan_masked (ALLOW_TO_OWNER_ONLY)`
- **🏛️ Authorized BigQuery Golden Queries (`2`)**: `10_wallet_tokens_active_on_card` · `11_dispute_event_timeline`
- **🔧 Bound ADK Tools (`2`)**: `query_mobile_wallet_events` · `golden_10_wallet_tokens_active_on_card`

---

## 04 · `web_portal_agent`
**Online Banking & Statutory Dispute Policy Specialist (`CHANNEL_PRODUCER · WEB_PORTAL`)**

Reads online dispute filings, replacement delivery instructions, and travel notices from `WEB_PORTAL` sessions in Memory Bank, and reconciles every open dispute against the BigQuery ledger (`07`, `08`, `09`) and Dataplex Business Glossary to enforce the statutory separation between **Regulation E** (debit cards — 10-business-day provisional credit) and **Regulation Z** (credit cards — no provisional credit; payment withheld).

- **🧠 Memory Bank Topics Scoped**: `dispute_context` (`OPEN_DISPUTE_CLAIMS`) · `travel_context` (`HOTEL_COURIER_ADDRESS`) · `spending_directives` (`STANDING_INSTRUCTIONS`)
- **🛡️ Dataplex Catalog Governance**: `disputes (ACTIVE)` · `accounts (ACTIVE)` · `cards (ACTIVE)` · `dataplex.glossary/regulation-e (VERIFIED)` · `dataplex.glossary/regulation-z (VERIFIED)` · `dataplex.glossary/provisional-credit (VERIFIED)` · `cards.pan_full (REFUSE · RESTRICTED PII)`
- **🏛️ Authorized BigQuery Golden Queries (`3`)**: `07_open_disputes_for_customer` · `08_dispute_filing_window_check` · `09_provisional_credit_due`
- **🔧 Bound ADK Tools (`2`)**: `query_web_portal_activity` · `golden_07_open_disputes_for_customer`

---

## 05 · `branch_support_agent`
**In-Branch Banker & Identity Verification Specialist (`CHANNEL_PRODUCER · BRANCH_SUPPORT`)**

Produces in-person teller and branch banker consultation records (such as physical ID verification and notary attestations) from `BRANCH_SUPPORT` sessions in Memory Bank, and verifies core customer profile, account tenure, and 90-day relationship state via Golden Query `01`.

- **🧠 Memory Bank Topics Scoped**: `identity_verification` (`IN_PERSON_KYC`) · `account_profile` (`RELATIONSHIP_TENURE`) · `dispute_context` (`BRANCH_CASE_NOTES`)
- **🛡️ Dataplex Catalog Governance**: `v_customer_360 (ACTIVE)` · `customers (ACTIVE)` · `accounts (ACTIVE)` · `cards (ACTIVE)` · `customers.ssn_last4 (REFUSE · RESTRICTED PII)` · `customers.customer_id (ALLOW)`
- **🏛️ Authorized BigQuery Golden Queries (`2`)**: `01_customer_360_snapshot` · `11_dispute_event_timeline`
- **🔧 Bound ADK Tools (`2`)**: `query_branch_teller_interactions` · `golden_01_customer_360_snapshot`

---

## 06 · `claim_veracity_validator_agent`
**Pre-Write Claim Veracity Gatekeeper (`PRE_WRITE_VALIDATOR`)**

Gates every incoming customer claim before it can be persisted to Vertex AI Memory Bank. Cross-examines the customer's natural-language claim against 7 ground-truth avenues (BigQuery Golden Queries `01`, `02`, `03`, `04`, `07` + scoped Memory Bank recall) and runs Gemini structured evaluation (`ClaimVeracityEvaluation`) to detect travel/location contradictions, amount mismatches, or superseded facts.

- **🧠 Memory Bank Topics Scoped**: `travel_context` · `fraud_history` · `dispute_context` · `identity_verification`
- **🛡️ Dataplex Catalog Governance**: `v_customer_360 (ACTIVE)` · `authorizations (ACTIVE)` · `disputes (ACTIVE)` · `authorizations_deprecated_v1 (REFUSE · DEPRECATED)` · `customers.ssn_last4 (REFUSE · PII)` · `cards.pan_full (REFUSE · PII)`
- **🏛️ Authorized BigQuery Golden Queries (`5`)**: `01_customer_360_snapshot` · `02_recent_authorizations_for_card` · `03_suspicious_activity_on_card_window` · `04_geo_impossible_travel` · `07_open_disputes_for_customer`
- **🔧 Bound ADK Tools (`1`)**: `validate_and_record_customer_claim`

---

## 07 · `memory_bank_audit_agent`
**Enterprise Memory Bank Consolidated Sweep Auditor (`CONSOLIDATED_AUDITOR · ASYNC JOB`)**

Runs as an asynchronous background or operator-triggered sweep (`▶ Run 95s audit sweep`) kept off the synchronous customer chat path. Audits consolidated memories across `CUST00001`..`CUST00010` against BigQuery telemetry to detect stale travel notices, un-superseded contradictions, or cross-channel drift.

- **🧠 Memory Bank Topics Scoped**: `ALL_TOPICS` (`Enterprise Audit Scope: CUST00001 .. CUST00010`)
- **🛡️ Dataplex Catalog Governance**: `v_customer_360 (ACTIVE)` · `authorizations (ACTIVE)` · `disputes (ACTIVE)` · `customers.ssn_last4 (REFUSE · PII)` · `cards.pan_full (REFUSE · PII)`
- **🏛️ Authorized BigQuery Golden Queries (`2`)**: `01_customer_360_snapshot` · `04_geo_impossible_travel`
- **🔧 Bound ADK Tools (`1`)**: `run_consolidated_memory_audit_sweep`

---

## 08 · `consumer_credit_synthesizer_agent`
**Lead Synthesizer & Hub Orchestrator (`LEAD_SYNTHESIZER · CORE_BANKING`)**

The hub of the 8-agent topology and the only agent that communicates with the customer/operator. Collects live channel telemetry from spokes `01`–`05`, enforces the pre-write veracity gate (`06`), queries the Memory Bank topic vocabulary (`list_memory_topics_tool`), reconstructs the chronological cross-channel causal chain (`read_customer_memory_bank`), and executes governed card unlock or restriction actions (`execute_one_click_card_unlock`).

- **🧠 Memory Bank Topics Scoped**: `travel_context` · `fraud_history` · `dispute_context` · `device_trust` · `spending_directives` · `identity_verification`
- **🛡️ Dataplex Catalog Governance**: `v_customer_360 (ACTIVE)` · `disputes (ACTIVE)` · `wallet_tokens (ACTIVE)` · `dispute_events (ACTIVE)` · `dataplex.glossary/regulation-e` · `dataplex.glossary/regulation-z` · `customers.ssn_last4 (REFUSE · PII)` · `cards.pan_full (REFUSE · PII)`
- **🏛️ Authorized BigQuery Golden Queries (`6`)**: `01_customer_360_snapshot` · `07_open_disputes_for_customer` · `08_dispute_filing_window_check` · `09_provisional_credit_due` · `10_wallet_tokens_active_on_card` · `11_dispute_event_timeline`
- **🔧 Bound ADK Tools (`3` direct + `7` sub-agent spokes)**: `list_memory_topics_tool` · `read_customer_memory_bank` · `execute_one_click_card_unlock`
