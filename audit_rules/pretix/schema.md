# Investigation database (read-only SQLite)

- `audit_log(id, ts, action, actor_user_id, actor_token_id, event, object_type, object_id, data)`
  Application audit log exported from pretix. `ts` is ISO-8601 UTC. `actor_user_id` is set for
  actions by a logged-in user, `actor_token_id` for actions by a team API token. `data` is the JSON
  payload written by the application. Rendered e-mail bodies are removed from e-mail log rows.
  Only rows inside the coverage window are present.
- `users(id, email)`
- `teams(id, name, all_events, limit_events, all_event_permissions, event_permissions,
  all_organizer_permissions, organizer_permissions)` current state.
- `team_members(team_id, user_id)` current state.
- `team_tokens(id, team_id, name, active)` current state.
- `events(id, slug, name)`, `orders(id, code, event, status, email, expires)` current state.
- `source_coverage(source, covered_from, covered_to, note)` what each source covers.

Order operations by a token appear as rows such as `pretix.event.order.paid` or
`pretix.event.order.expirychanged` with `object_type = 'order'` and `object_id = orders.id`.
