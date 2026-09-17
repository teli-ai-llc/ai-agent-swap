-- Team pool schema v1 hardening (Supabase security advisor findings).
-- The cost view must apply the querying user's RLS, not the creator's.
alter view public.pool_usage_cost set (security_invoker = on);

-- Trigger functions are never meant to be called through PostgREST RPC.
revoke execute on function public.pool_accounts_guard() from public, anon, authenticated;
revoke execute on function public.pool_accounts_insert_guard() from public, anon, authenticated;

-- pool_is_admin() is evaluated inside RLS policies as the querying role, so
-- authenticated keeps EXECUTE; only anonymous callers lose it.
revoke execute on function public.pool_is_admin() from public, anon;
