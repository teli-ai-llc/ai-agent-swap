# Team pool: Supabase setup (admin)

One Supabase project per pool. The repo's `.mcp.json` is scoped to the
"AI Agent Swapping" project.

Members sign in with their work email and one shared **pool code**. The code
is every member's password in Supabase Auth; a first-time email is signed up
with it on the spot. No emails are ever sent, so the built-in mailer's limits
do not matter.

## One-time setup

1. Apply the migrations in order: `migrations/0001_pool.sql`,
   `0002_pool_hardening.sql`, `0003_pool_signup.sql` (SQL editor,
   `supabase db push`, or the MCP `apply_migration` tool).
2. Allow your email domain (SQL editor):
   ```sql
   insert into public.pool_meta (key, value) values ('signup_domains', 'teli.ai')
     on conflict (key) do update set value = excluded.value;
   ```
   Comma-separate several domains. A first login from one of them creates the
   auth user and its `pool_members` row (role `member`); any other domain is
   refused, dashboard "Add user" included.
3. Dashboard → Authentication → Sign In / Providers → Email: keep **Allow new
   users to sign up** on and turn **Confirm email** OFF. With it on, a first
   login is created but gets no session, and Supabase tries to email a
   confirmation instead.
4. Pick a pool code (a passphrase, at least 6 characters). Members who already
   exist in Authentication → Users get it with (SQL editor, your code in place
   of the placeholder):
   ```sql
   update auth.users
   set encrypted_password = extensions.crypt('<POOL_CODE>', extensions.gen_salt('bf')),
       email_confirmed_at = coalesce(email_confirmed_at, now()),
       updated_at = now()
   where email like '%@teli.ai';
   ```
   New members never need this: their first login signs them up with the code.
5. Make yourself admin after your own first login:
   `update public.pool_members set role = 'admin' where user_id = '<your auth user id>';`
6. Give members the project URL, the anon key (Project Settings → API) and the
   pool code, privately. Each runs `cswap pool login`.

## Changing or resetting the code

Run the statement from step 4 again with the new code. Machines that are
already logged in keep working (they hold a session, not the code); the code
is only asked for at login.

The same statement narrowed to one address resets a single member.

## Rules enforced by the database, not by cswap

- a push with a lower `credential_version` than the row is rejected;
- a push with a higher version clears `needs_relogin`, whoever pushes it;
- only the owner (or an admin) changes `shared`, the limits, or withdraws;
- any member may flag a shared row `needs_relogin`;
- a member only inserts usage events and machines as themselves;
- new auth users outside `signup_domains` are refused; inside them they get
  a member row automatically.

## What the shared code does not give you

Anyone with the code can sign up as any address in the allowed domains
without proving they own the mailbox. Fine for an internal team behind a
private code; not a public sign-up. Rotate the code if it leaks.

## Remote Control on pooled machines

`cswap pool login` writes `disableRemoteControl: true` and
`remoteControlAtStartup: false` into the machine's `~/.claude/settings.json`.
A Claude Code Remote Control session started while a borrowed login is
active would otherwise appear in, and be drivable from, the owner's
claude.ai. `cswap pool status` and `cswap sync` warn if the keys are removed.
