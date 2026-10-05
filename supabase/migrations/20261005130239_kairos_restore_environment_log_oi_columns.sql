-- Restore the persisted score payload contract used by SupabaseDB.
alter table public.environment_log
    add column if not exists ce_oi_change bigint not null default 0,
    add column if not exists pe_oi_change bigint not null default 0;
