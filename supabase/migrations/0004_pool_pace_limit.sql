-- 0004: pace-bound share limits (schema v2).
--
-- `cswap pool share N --hard-limit` (bare, or `--hard-limit pace`) caps
-- borrowers at the share of the owner's 7-day window that has elapsed —
-- 60% through the week, 60% — so borrowing never pushes a login ahead of
-- pace. The flag rides beside the numeric limit (a borrower's cswap takes
-- the stricter of the two) and is owner-only like the rest of the sharing
-- columns.
--
-- Rollout: cswap speaks schema v1 and v2, so teammates may update before or
-- after this runs. On a v1 pool a pace share is refused by name; once this
-- has run, a cswap that only speaks v1 stops syncing ("upgrade cswap"), so
-- nobody keeps borrowing while blind to a pace share.

alter table public.pool_accounts
  add column share_hard_limit_pace boolean not null default false;

-- Same guard as 0001, with the new column in the owner-only list.
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

update public.pool_meta set value = '2' where key = 'schema_version';
