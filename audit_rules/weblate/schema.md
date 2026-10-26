# Investigation database (read-only SQLite)

- `audit_log(id, ts, action, action_name, user_id, author_id, project_id, component_id, translation_id,
  unit_id, target, details)` Project history exported from Weblate (one row per recorded change in the
  project). `ts` is ISO-8601 UTC. `action` is Weblate's numeric action code and `action_name` its name:
  translation edits are `new` (5, a string translated for the first time) and `change` (2, an existing
  translation edited); other names include `upload`, `approve`, `comment`, `suggestion`, `invite_user`,
  `remove_user`, `add_user`. `user_id` is the account that performed the change: a person, or a
  project token's robot user. `details` is the JSON payload written by the application. Only rows
  inside the coverage window are present.
- `account_log(id, ts, user_id, username, activity, actor_username, team, project, params)` Account
  activity log exported from Weblate (one row per recorded account event). `user_id` / `username`
  identify the account the row is about. `activity` values include `token-created` (a project token
  was created; the account is the token's robot user, `actor_username` is the creator, `project` the
  project name), `team-add` / `team-remove` (the account was added to / removed from the team named in
  `team`; `actor_username` is the administrator who did it), `disabled-expiry` (the account was disabled
  by the expiry cleanup), `login`, `invited`, `accepted`. `params` is the application's JSON parameter
  object; `actor_username`, `team` and `project` are its keys `username`, `team`, `project` as columns.
  Only rows inside the coverage window are present.
- `users(id, username, full_name, is_bot, is_active, is_superuser, date_joined)` current state. Project
  tokens are the rows with `is_bot = 1`. Weblate's internal service accounts are not exported.
- `teams(id, name, project_id)` current state.
- `team_members(team, username)` current state.
- `tokens(user_id, username, name, team, is_active, created_at)` current state of every project token
  (robot user): the team it belongs to and whether it is active.
- `token_validity(user_id, valid_from, valid_to)` validity period of every project token that is active at
  export time (`valid_to` NULL = no expiry). Disabled tokens are not listed.
- `projects(id, slug, name)`, `units(id, translation_id, language, source)` current state.
- `source_coverage(source, covered_from, covered_to, note)` what each source covers.

Operations by a project token appear in `audit_log` as rows with `action_name = 'new'` or `'change'`,
`user_id = tokens.user_id` and `unit_id = units.id`.
