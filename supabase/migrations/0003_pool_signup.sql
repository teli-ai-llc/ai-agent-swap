-- Team pool: self-service sign-up by email domain.
--
-- cswap members log in with a one-time code emailed by Supabase Auth
-- (POST /auth/v1/otp, then /auth/v1/verify). With sign-ups enabled in the
-- dashboard, a first-time address is created by that first request. These
-- triggers make that safe and complete:
--
--   * pool_meta.signup_domains — a comma-separated list of email domains,
--     e.g. 'teli.ai' or 'teli.ai,example.com'. Set it with
--       insert into public.pool_meta (key, value) values ('signup_domains', 'teli.ai')
--         on conflict (key) do update set value = excluded.value;
--   * while it is set, a new auth user outside those domains is refused
--     (GoTrue reports it as "Database error saving new user"; cswap explains
--     it), and a new user inside them gets a pool_members row on the spot,
--     as 'member', named after the local part of the address;
--   * while it is unset or empty nothing changes: anyone can be created and
--     the admin inserts member rows by hand, as before.
--
-- The guard applies to every auth.users insert, dashboard "Add user"
-- included, so an admin creating an outside address must clear the list
-- first. Schema version stays 1: no client-visible table changed.

create or replace function public.pool_signup_domains() returns text[]
language sql stable security definer set search_path = public as $$
  select coalesce(
    (select array_remove(array_agg(lower(trim(d))), '')
       from unnest(string_to_array(
         (select value from public.pool_meta where key = 'signup_domains'), ',')) as d),
    '{}'::text[]);
$$;

create or replace function public.pool_signup_guard() returns trigger
language plpgsql security definer set search_path = public as $$
declare
  domains text[] := public.pool_signup_domains();
  domain text := lower(split_part(coalesce(new.email, ''), '@', 2));
begin
  if cardinality(domains) > 0 and not (domain = any (domains)) then
    raise exception 'pool: sign-ups from @% are not allowed (pool_meta.signup_domains)', domain
      using errcode = 'check_violation';
  end if;
  return new;
end $$;

create or replace function public.pool_signup_member() returns trigger
language plpgsql security definer set search_path = public as $$
declare
  domains text[] := public.pool_signup_domains();
  domain text := lower(split_part(coalesce(new.email, ''), '@', 2));
begin
  if cardinality(domains) > 0 and domain = any (domains) then
    insert into public.pool_members (user_id, display_name, role)
      values (new.id, split_part(new.email, '@', 1), 'member')
      on conflict (user_id) do nothing;
  end if;
  return new;
end $$;

drop trigger if exists pool_signup_guard on auth.users;
create trigger pool_signup_guard before insert on auth.users
  for each row execute function public.pool_signup_guard();

drop trigger if exists pool_signup_member on auth.users;
create trigger pool_signup_member after insert on auth.users
  for each row execute function public.pool_signup_member();

-- Auth inserts run as supabase_auth_admin; nobody else needs these (same
-- posture as 0002 for the other trigger functions).
revoke execute on function public.pool_signup_domains() from public, anon, authenticated;
revoke execute on function public.pool_signup_guard() from public, anon, authenticated;
revoke execute on function public.pool_signup_member() from public, anon, authenticated;
grant execute on function public.pool_signup_domains() to supabase_auth_admin;
grant execute on function public.pool_signup_guard() to supabase_auth_admin;
grant execute on function public.pool_signup_member() to supabase_auth_admin;
