-- Team pool schema v1. See docs/superpowers/specs/2026-09-17-team-pool-credential-sync-design.md
create extension if not exists pgcrypto;

create table public.pool_meta (
  key text primary key,
  value text not null
);
insert into public.pool_meta (key, value) values ('schema_version', '1');

create table public.pool_members (
  user_id uuid primary key references auth.users (id) on delete cascade,
  display_name text not null,
  role text not null default 'member' check (role in ('admin', 'member')),
  created_at timestamptz not null default now()
);

create table public.pool_accounts (
  id uuid primary key default gen_random_uuid(),
  account_uuid text not null,
  organization_uuid text not null default '',
  email text not null,
  organization_name text not null default '',
  owner_user_id uuid not null references public.pool_members (user_id),
  credential jsonb,
  credential_version bigint not null default 0,
  credential_fingerprint text,
  updated_by_user_id uuid references public.pool_members (user_id),
  updated_by_machine_id uuid,
  updated_at timestamptz not null default now(),
  status text not null default 'ok' check (status in ('ok', 'needs_relogin', 'withdrawn')),
  needs_relogin_since timestamptz,
  needs_relogin_reported_by uuid,
  shared boolean not null default false,
  share_swap_limit numeric check (share_swap_limit is null or (share_swap_limit >= 1 and share_swap_limit <= 100)),
  share_hard_limit numeric check (share_hard_limit is null or (share_hard_limit >= 1 and share_hard_limit <= 100)),
  created_at timestamptz not null default now(),
  unique (account_uuid, organization_uuid)
);
create index pool_accounts_updated_at_idx on public.pool_accounts (updated_at);

create table public.pool_machines (
  machine_id uuid primary key,
  user_id uuid not null references public.pool_members (user_id) on delete cascade,
  hostname text not null default '',
  cswap_version text not null default '',
  last_seen_at timestamptz not null default now()
);

create table public.model_rates (
  model text primary key,
  input_per_m numeric not null,
  output_per_m numeric not null,
  cache_read_per_m numeric not null,
  cache_write_per_m numeric not null
);

create table public.pool_usage_events (
  id bigint generated always as identity primary key,
  account_id uuid not null references public.pool_accounts (id),
  user_id uuid not null references public.pool_members (user_id),
  machine_id uuid not null,
  session_id text not null,
  request_id text not null unique,
  model text not null,
  input_tokens bigint not null default 0,
  output_tokens bigint not null default 0,
  cache_read_tokens bigint not null default 0,
  cache_write_tokens bigint not null default 0,
  occurred_at timestamptz not null
);
create index pool_usage_events_account_time_idx on public.pool_usage_events (account_id, occurred_at);
create index pool_usage_events_user_time_idx on public.pool_usage_events (user_id, occurred_at);

create view public.pool_usage_cost as
select e.*,
       (e.input_tokens * r.input_per_m + e.output_tokens * r.output_per_m
        + e.cache_read_tokens * r.cache_read_per_m + e.cache_write_tokens * r.cache_write_per_m) / 1e6 as cost_usd
from public.pool_usage_events e
left join public.model_rates r on r.model = e.model;

-- Helpers ---------------------------------------------------------------
create or replace function public.pool_is_admin() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from public.pool_members where user_id = auth.uid() and role = 'admin');
$$;

-- A newer credential heals the row; owner-only columns stay owner-only.
-- Ownership checks run against the status the client sent; the version heal is applied afterwards,
-- so a newer credential from any member clears needs_relogin.
create or replace function public.pool_accounts_guard() returns trigger
language plpgsql security definer set search_path = public as $$
declare
  is_owner boolean := (auth.uid() = old.owner_user_id);
  is_admin boolean := public.pool_is_admin();
begin
  if new.credential_version < old.credential_version then
    raise exception 'pool: stale credential version (% < %)', new.credential_version, old.credential_version
      using errcode = 'check_violation';
  end if;

  if not (is_owner or is_admin) then
    if new.owner_user_id <> old.owner_user_id
       or new.shared <> old.shared
       or new.share_swap_limit is distinct from old.share_swap_limit
       or new.share_hard_limit is distinct from old.share_hard_limit
       or new.email <> old.email
       or new.account_uuid <> old.account_uuid
       or new.organization_uuid <> old.organization_uuid then
      raise exception 'pool: only the owner may change sharing or identity' using errcode = 'insufficient_privilege';
    end if;
    -- a non-owner may only report a dead lineage
    if new.status <> old.status and not (new.status = 'needs_relogin' and old.status = 'ok') then
      raise exception 'pool: only the owner may set status %', new.status using errcode = 'insufficient_privilege';
    end if;
  end if;

  if new.credential_version > old.credential_version then
    new.status := 'ok';
    new.needs_relogin_since := null;
    new.needs_relogin_reported_by := null;
    new.updated_by_user_id := auth.uid();
  end if;

  if new.status = 'needs_relogin' and old.status <> 'needs_relogin' then
    new.needs_relogin_since := coalesce(new.needs_relogin_since, now());
  end if;
  new.updated_at := now();
  return new;
end;
$$;

create trigger pool_accounts_guard before update on public.pool_accounts
for each row execute function public.pool_accounts_guard();

create or replace function public.pool_accounts_insert_guard() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if new.owner_user_id <> auth.uid() and not public.pool_is_admin() then
    raise exception 'pool: an account can only be published by its owner' using errcode = 'insufficient_privilege';
  end if;
  new.updated_by_user_id := auth.uid();
  new.updated_at := now();
  return new;
end;
$$;

create trigger pool_accounts_insert_guard before insert on public.pool_accounts
for each row execute function public.pool_accounts_insert_guard();

-- Row-level security ----------------------------------------------------
alter table public.pool_meta enable row level security;
alter table public.pool_members enable row level security;
alter table public.pool_accounts enable row level security;
alter table public.pool_machines enable row level security;
alter table public.model_rates enable row level security;
alter table public.pool_usage_events enable row level security;

create policy members_read_meta on public.pool_meta for select to authenticated using (true);
create policy members_read_members on public.pool_members for select to authenticated using (true);
create policy admins_write_members on public.pool_members for all to authenticated
  using (public.pool_is_admin()) with check (public.pool_is_admin());

create policy members_read_accounts on public.pool_accounts for select to authenticated
  using (shared or owner_user_id = auth.uid() or public.pool_is_admin());
create policy members_insert_own_accounts on public.pool_accounts for insert to authenticated
  with check (owner_user_id = auth.uid() or public.pool_is_admin());
create policy members_update_visible_accounts on public.pool_accounts for update to authenticated
  using (shared or owner_user_id = auth.uid() or public.pool_is_admin());

create policy members_read_machines on public.pool_machines for select to authenticated using (true);
create policy members_write_own_machines on public.pool_machines for all to authenticated
  using (user_id = auth.uid()) with check (user_id = auth.uid());

create policy members_read_rates on public.model_rates for select to authenticated using (true);
create policy admins_write_rates on public.model_rates for all to authenticated
  using (public.pool_is_admin()) with check (public.pool_is_admin());

create policy members_read_usage on public.pool_usage_events for select to authenticated using (true);
create policy members_insert_own_usage on public.pool_usage_events for insert to authenticated
  with check (user_id = auth.uid());
