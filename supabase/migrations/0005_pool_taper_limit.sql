-- 0005: tapered share limits.
--
-- `cswap pool share N --hard-limit 50 --taper` keeps the owner's reserve
-- (100 - limit) only for the share of the 7-day window that is still left:
-- the cap is 100 - (100 - limit) x share of the week left. A 50% limit is
-- 50% right after a reset, 80% with 40% of the week left, and 100% at the
-- reset, so quota the owner was keeping becomes borrowable just as it would
-- otherwise expire unused. Owner-only like the rest of the sharing columns.
--
-- Rollout: no schema_version bump. A taper only ever loosens a limit, so a
-- cswap that does not know the column enforces the plain limit (the safe
-- side) and keeps syncing. cswap looks for the `hard_limit_taper` flag in
-- pool_meta before sending the column, and refuses a taper share by name
-- until this has run.

alter table public.pool_accounts
  add column share_hard_limit_taper boolean not null default false;

-- Same guard as 0004, with the new column in the owner-only list.
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
       or new.share_hard_limit_pace <> old.share_hard_limit_pace
       or new.share_hard_limit_taper <> old.share_hard_limit_taper
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
    new.updated_by_user_id := auth.uid();
    if old.status = 'needs_relogin' then
      new.status := 'ok';
      new.needs_relogin_since := null;
      new.needs_relogin_reported_by := null;
    end if;
  end if;

  if new.status = 'needs_relogin' and old.status <> 'needs_relogin' then
    new.needs_relogin_since := coalesce(new.needs_relogin_since, now());
  end if;
  new.updated_at := now();
  return new;
end;
$$;

-- 0002 revoked direct execution of the guard; keep it that way after the replace.
revoke execute on function public.pool_accounts_guard() from public, anon, authenticated;

insert into public.pool_meta (key, value) values ('hard_limit_taper', '1')
  on conflict (key) do update set value = excluded.value;
