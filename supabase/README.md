# Team pool: Supabase setup (admin)

One Supabase project per pool. The repo's `.mcp.json` is scoped to the
"AI Agent Swapping" project.

1. Apply `migrations/0001_pool.sql` (SQL editor, `supabase db push`, or the
   MCP `apply_migration` tool).
2. Add members: Authentication → Users → *Invite user* with their email. They
   receive an email and set a password.
3. For each invited user, insert their member row (SQL editor):
   `insert into public.pool_members (user_id, display_name, role)
    values ('<auth user id>', 'Harsha', 'member');`
   Make yourself `admin`.
4. Give members the project URL and the anon key (Project Settings → API).
   Each of them runs `cswap pool login`.

Rules enforced by the database, not by cswap:
- a push with a lower `credential_version` than the row is rejected;
- a push with a higher version clears `needs_relogin`;
- only the owner (or an admin) changes `shared`, the limits, or withdraws;
- any member may flag a shared row `needs_relogin`;
- a member only inserts usage events and machines as themselves.
