# Kairos Environment Monitor: Discord Orchestrator Context & Integration Guide

**Target Audience:** Discord Orchestrator / Gemini LLM
**Purpose:** Provide the exact technical context, architecture rules, and database schemas required for the Discord Orchestrator to operate seamlessly alongside the Python head-less scoring engine.

---

## 1. Core Architecture & Orchestrator's Role

The system is a 60-second interval condition scoring engine designed to detect favorable environments for Intraday NIFTY Options Buying.
It is **not** a signal generator. It is a live weather report pushing **🟢 GO / 🟡 CAUTION / 🔴 AVOID** alerts.

### The Boundary (Crucial Concept)
The Discord Orchestrator and the Python Engine **never communicate directly** — there are no HTTP requests or sockets between the two.
They communicate entirely via a asynchronous Shared State Bridge using **Supabase**.

```text
Discord Orchestrator   <⇄>   Supabase (4 Tables)   <⇄>   Python Engine (VPS)
```

**Orchestrator's Responsibilities:**
1. Host Discord Slash Commands (`/start-monitor`, `/stop-monitor`).
2. Read available expiry dates from the database to present to the user.
3. Write the user's selected session configuration back to the database to start the Python scoring loop.
4. Enhance the Python engine's raw summary logs using Gemini to produce human-readable environment descriptions.

---

## 2. Interaction Flow Requirements

### Flow 1: `/start-monitor`
When the user triggers this slash command in Discord:
1. **READ:** The Orchestrator queries the `available_expiries` table in Supabase.
   - The Python engine routinely populates this table with valid `symbol`, `expiry`, and `expiry_type` rows.
2. **UI:** The Orchestrator displays an interactive dropdown in Discord, allowing the user to select the specific symbol (e.g., NIFTY) and expiry date.
3. **WRITE:** Once the user submits their selection, the Orchestrator **MUST** write a new row to the `session_config` table:
   - `symbol` = "NIFTY"
   - `expiry` = (User selected date)
   - `expiry_type` = "WEEKLY" or "MONTHLY"
   - `status` = "ACTIVE"
4. *Effect:* Within 60 seconds, the Python Engine detects `status="ACTIVE"`, initializes its in-memory rolling buffers, fetches previous day levels, and begins scoring.

### Flow 2: `/stop-monitor`
1. **WRITE:** The Orchestrator updates the `session_config` table (or inserts a new row) setting `status = "STOPPED"`.
2. *Effect:* The Python Engine detects this and halts scoring immediately.

### Flow 3: Summary Enhancement (Gemini Polishing)
The Python engine generates a compact, machine-readable string indicating the points scored across the 7 technical conditions (e.g., `iv_trend`, `momentum`, `gamma_theta`). This string is stored in `environment_log.summary_raw`.
1. **LISTEN:** The Orchestrator must monitor the `environment_log` table (e.g., via Supabase Realtime subscriptions).
2. **PROCESS:** When a new state-change row appears, the Orchestrator extracts `summary_raw`, passes it to Gemini to translate into a natural language sentence, and updates the `summary` column of that row.
*Note:* The Python codebase's `notifier.py` currently attempts to send Discord webhooks immediately utilizing `summary` if available, or a fallback. The Orchestrator may need to either update the `summary` fast enough, edit the Discord webhook message via API after the fact, or the Python engine will need to be configured to wait for the Orchestrator's update.

---

## 3. Database Schema Mapping

The Orchestrator must structure its Supabase queries exactly matching these 4 tables.

### A. `session_config` (Written by Orchestrator, Read by Python)
Controls whether the Python engine is actively polling the Dhan APIs.
* **id** [SERIAL PRIMARY KEY]
* **symbol** [TEXT] -> e.g., "NIFTY"
* **expiry** [DATE] -> e.g., "2026-03-26"
* **expiry_type** [TEXT] -> "WEEKLY" or "MONTHLY"
* **status** [TEXT] -> "ACTIVE" or "STOPPED"

### B. `available_expiries` (Written by Python, Read by Orchestrator)
Used to populate the `/start-monitor` dropdown dynamically.
* **id** [SERIAL PRIMARY KEY]
* **symbol** [TEXT]
* **expiry** [DATE]
* **expiry_type** [TEXT]
* **fetched_at** [TIMESTAMPTZ] -> Used by pg_cron for cleanup

### C. `environment_log` (Written by Python, Updated by Orchestrator)
Contains the exact cycle-by-cycle scoring results. Central hub for triggering Orchestrator enhancements.
* **id** [SERIAL PRIMARY KEY]
* **timestamp** [TIMESTAMPTZ]
* **symbol** [TEXT]
* **score** [INTEGER] -> 0 to 8
* **status** [TEXT] -> "GO", "CAUTION", "AVOID"
* **iv_capped** [BOOLEAN] -> True if score was nerfed to CAUTION due to IV contraction
* **ce_oi_change** [BIGINT NOT NULL DEFAULT 0] -> Call-side OI change written by the scoring engine
* **pe_oi_change** [BIGINT NOT NULL DEFAULT 0] -> Put-side OI change written by the scoring engine
* **summary_raw** [TEXT] -> Python-generated machine string of condition hits
* **summary** [TEXT] -> Orchestrator/Gemini writes the polished response here

### D. `previous_day_levels` (Internal to Python)
*The Orchestrator does not need to interact with this table.* Used by Python to store PDH/PDL fetched on startup to survive process restarts without redundant API calls.

---

## 4. Discord Channels & Notifications

While the Orchestrator manages the conversational slash commands, **the Python engine independently utilizes Webhooks** to broadcast alerts directly to the designated Discord channels. 

1. **`#environment`**: (Powered by `discord_webhook_url`)
   - The Python engine posts state changes here (e.g., transitioning from AVOID to GO). 
   - **Noise Reduction**: After warmup, the first result is sent even if it is CAUTION or AVOID. During low-score periods, diagnostic detail changes remain silent; environment or condition color changes still alert. OI Flow alerts only on RED ↔ GREEN flips. Favorable GO results also alert on score changes.
   - These alerts are clean, structured grids showing condition scores mapped via emojis (🟢, 🟡, 🔴).
2. **`#system-check`**: (Powered by `discord_health_webhook_url`)
   - Routine five-minute heartbeats are logged locally and suppressed in Discord under ADR-006.
   - Supabase session-read failures pause monitoring and send one critical warning per outage. Failed warning deliveries retry on subsequent checks; restored access produces a recovery notice, including when no ACTIVE session exists. Failed recovery deliveries also retry on subsequent successful reads. A later outage can send a new warning even if the previous recovery notice was not delivered.
   - It will post critical alerts, startup warm-up timers, and stale-signal warnings if the API endpoints (Dhan) fail to respond. 

The Orchestrator can leverage this logic to ensure its own administrative alerts route to the same locations without muddying the clean `#environment` channel.

## 5. Deployment and Recovery Checks

The headless worker uses a server-side Supabase service-role key. Keep this key out of public clients and Discord messages. The current bridge tables have RLS enabled without client policies; an orchestrator using an anon/authenticated key needs explicit, scoped authorization before it can create a session. Do not resolve this by granting public write access.

`environment_log` must include `ce_oi_change` and `pe_oi_change` as bigint fields with a zero default, matching the worker's score payload. Apply the tracked migrations under `supabase/migrations/` before deploying code that depends on them.

To verify recovery, confirm the intended ACTIVE session is visible, today's market levels/expiries refresh, and score rows advance every minute with OI fields populated. Observe startup/health delivery and the first environment notification after the normal warmup period. An empty session table correctly leaves the worker idle; a failed session read is a system fault and now alerts separately.

A transient session-read outage pauses the cycle while preserving session membership, candle/IV/OI history, and warmup progress. The first scoring cycle that successfully reads the same ACTIVE session resumes normal scoring and alert delivery, subject to the existing market schedule and alert rules. A confirmed inactive session, changed configuration, or market boundary still follows the normal session transition rules.
