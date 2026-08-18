-- ─────────────────────────────────────────────────────────────
-- Kairos — Supabase Schema
-- Run this entire file in Supabase SQL Editor once
-- ─────────────────────────────────────────────────────────────


-- ── Table: session_config ────────────────────────────────────
-- Written by Discord Orchestrator after /start-monitor
-- Read by Python every cycle to get active symbol + expiry

create table if not exists session_config (
    id          serial primary key,
    symbol      text not null,                      -- 'NIFTY' or 'SENSEX'
    expiry      date not null,
    expiry_type text not null,                      -- 'WEEKLY' or 'MONTHLY'
    status      text not null default 'STOPPED',    -- 'ACTIVE' or 'STOPPED'
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create index if not exists idx_session_config_status
    on session_config (symbol, status);


-- ── Table: available_expiries ────────────────────────────────
-- Written by Python at /start-monitor trigger
-- Read by Discord Orchestrator to build the expiry dropdown in Discord

create table if not exists available_expiries (
    id          serial primary key,
    symbol      text not null,
    expiry      date not null,
    expiry_type text not null,                      -- 'WEEKLY' or 'MONTHLY'
    fetched_at  timestamptz not null default now()
);


-- ── Table: previous_day_levels ───────────────────────────────
-- Written by Python at session start (once per day)
-- Holds yesterday's high and low for the PDH/PDL breakout condition

create table if not exists previous_day_levels (
    id              serial primary key,
    symbol          text not null,
    trade_date      date not null,                  -- today (these levels apply to today)
    prev_day_high   float not null,
    prev_day_low    float not null,
    fetched_at      timestamptz not null default now(),
    unique (symbol, trade_date)                     -- one row per symbol per day
);


-- ── Table: environment_log ───────────────────────────────────
-- Written by Python every cycle (1 row per minute)
-- Read by Python for state-change detection (last row only)
-- Read by Discord Orchestrator to build Discord embed

create table if not exists environment_log (
    id              serial primary key,
    timestamp       timestamptz not null,
    symbol          text not null,
    expiry          date not null,
    dte             integer not null,               -- days to expiry at time of scoring
    score           integer not null,               -- 0–8
    status          text not null,                  -- 'GO', 'CAUTION', 'AVOID'
    iv_trend        text not null,                  -- 'GREEN', 'YELLOW', 'RED'
    momentum        text not null,
    oi_flow         text not null,
    gamma_theta     text not null,
    pdhl_breakout   text not null,
    move_ratio      text not null,
    vwap_distance   text not null,
    iv_capped       boolean not null default false,
    summary_raw     text,                           -- Python-generated template string
    summary         text                            -- Gemini-polished (filled by Discord Orchestrator)
);

-- Critical index — used every cycle for state-change detection
create index if not exists idx_env_log_latest
    on environment_log (symbol, timestamp desc);


-- ── Table: api_keys ──────────────────────────────────────────
-- Written by centralized token renewal microservice
-- Read by Python to fetch dynamic client_id and access_token

create table if not exists api_keys (
    provider      text primary key,                   -- 'DHAN', etc.
    client_id     text not null,
    access_token  text not null,
    updated_at    timestamptz not null default now()
);


-- ─────────────────────────────────────────────────────────────
-- pg_cron cleanup jobs
-- Run after the tables above — keeps free tier storage clean
-- ─────────────────────────────────────────────────────────────

create extension if not exists pg_cron;

-- Keep 30 days of environment_log (useful for trade review)
-- Runs daily at 18:30 IST (13:00 UTC) — after market close
select cron.schedule(
    'cleanup-environment-log',
    '0 13 * * *',
    $$ delete from environment_log where timestamp < now() - interval '30 days'; $$
);

-- Clean up stale available_expiries (refreshed on each /start-monitor)
select cron.schedule(
    'cleanup-expiries',
    '0 13 * * *',
    $$ delete from available_expiries where fetched_at < now() - interval '7 days'; $$
);
