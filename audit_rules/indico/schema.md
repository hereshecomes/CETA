# Investigation database (read-only SQLite)

- `audit_log(id, ts, event_id, user_id, realm, kind, module, summary, data)` Event log exported from
  Indico (one row per recorded action on an event). `ts` is ISO-8601 UTC. `event_id` is the event;
  `user_id` is the account that performed the action (NULL for actions without a user). `realm` is
  the log area (`event`, `management`, `participants`, `reviewing`, `emails`); `kind` is `positive`,
  `negative`, `change` or `other`; `module` names the part of Indico (`Event`, `Protection`, ...);
  `summary` is the one-line description Indico writes (for example `Title updated`,
  `Data updated`, `Event created`, `ACL entry added`, `ACL entry removed`, `ACL entry changed`).
  `data` is the JSON payload written by Indico: for changes to an event the field changes under
  `Changes`; for ACL rows the principal (`User`: the user's full name, or `Group`, `Email`, ...)
  and its rights (`Manager`, `Read Access`, `Permissions`). Only rows inside the coverage window are
  present.
- `category_log(id, ts, category_id, user_id, realm, kind, module, summary, data)` Category log
  exported from Indico, same columns as `audit_log` with the category instead of the event; ACL
  changes of a category are rows with `module = 'Protection'` and the same `summary` and `data`
  forms as for events. Only rows inside the coverage window are present.
- `app_log(id, ts, user_id, realm, kind, module, summary, data)` Indico's application log
  (administrator actions such as granting or revoking Indico administrator status). Only rows inside
  the coverage window are present.
- `users(id, full_name, email, is_admin)` current state of the user accounts.
- `categories(id, title, parent_id)` current state of the category tree (`parent_id` NULL for the
  top-level category).
- `events(id, title, category_id, start_dt, end_dt)` current state of the events; `category_id` is
  the category that directly contains the event.
- `event_team(event_id, user_name)` and `category_team(category_id, user_name)` current state: the
  users listed individually in the ACL of each event and category at export time (`user_name` is the
  user's full name).
- `source_coverage(source, covered_from, covered_to, note)` what each source covers.

Changes to an event appear in `audit_log` as rows with `realm = 'management'`; their `user_id` is the
acting user and `event_id` the event that was changed.
