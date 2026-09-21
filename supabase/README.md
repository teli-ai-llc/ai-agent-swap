# Team pool: Supabase setup (admin)

One Supabase project per pool. The repo's `.mcp.json` is scoped to the
"AI Agent Swapping" project.

## One-time setup

1. Apply the migrations in order: `migrations/0001_pool.sql`,
   `0002_pool_hardening.sql`, `0003_pool_signup.sql` (SQL editor,
   `supabase db push`, or the MCP `apply_migration` tool).
2. Let teammates sign themselves up by work email (SQL editor):
   ```sql
   insert into public.pool_meta (key, value) values ('signup_domains', 'teli.ai')
     on conflict (key) do update set value = excluded.value;
   ```
   Comma-separate several domains. While this is set, a first login from one
   of them creates the auth user and its `pool_members` row (role `member`);
   any other domain is refused, dashboard "Add user" included.
3. Dashboard → Authentication → Sign In / Providers → Email: leave
   **Allow new users to sign up** on, and keep **Confirm email** on.
4. Dashboard → Authentication → Email Templates: put the code in the
   **Magic Link** template and in **Confirm signup** (a first-time address
   gets the latter), e.g.
   ```html
   <h2>Your cswap pool sign-in code</h2>
   <p>Enter this code: <b>{{ .Token }}</b></p>
   <p>It expires in a few minutes. If you did not ask for it, ignore this email.</p>
   ```
   Do not use `{{ .ConfirmationURL }}`: the CLI has nowhere to receive a
   link, and the project's Site URL is not a web app.
5. Make yourself admin after your own first login:
   `update public.pool_members set role = 'admin' where user_id = '<your auth user id>';`
6. Post the project URL and the anon key (Project Settings → API) in the
   team channel. Each teammate runs `cswap pool login`, types their work
   email, and enters the code from their inbox.

Supabase's built-in mailer allows only a few emails per hour; the code is
needed once per machine, so that is fine for a small team. If logins start
failing with "rate limited", configure custom SMTP (Project Settings →
Authentication → SMTP).

## Without self-service (signup_domains unset)

Authentication → Users → *Add user* with the teammate's email, then insert
their member row:
`insert into public.pool_members (user_id, display_name, role)
 values ('<auth user id>', 'Harsha', 'member');`
They still log in with an emailed code.

## Rules enforced by the database, not by cswap

- a push with a lower `credential_version` than the row is rejected;
- a push with a higher version clears `needs_relogin`, whoever pushes it;
- only the owner (or an admin) changes `shared`, the limits, or withdraws;
- any member may flag a shared row `needs_relogin`;
- a member only inserts usage events and machines as themselves;
- new auth users outside `signup_domains` are refused; inside them they get
  a member row automatically.

## Remote Control on pooled machines

`cswap pool login` writes `disableRemoteControl: true` and
`remoteControlAtStartup: false` into the machine's `~/.claude/settings.json`.
A Claude Code Remote Control session started while a borrowed login is
active would otherwise appear in, and be drivable from, the owner's
claude.ai. `cswap pool status` and `cswap sync` warn if the keys are removed.
