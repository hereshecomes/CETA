# Organization event management policy (public)

The organization runs Indico for the events of its research groups. Every group has a category in
Indico (directly below the institute-level category); its events are created in that category. Each
event has an *event team* and each group category has a *category team*. The teams are the users
listed individually on the Protection page of the event or of the category (its access control list,
ACL). The organization's IT service desk (Indico administrators, and the managers of the
institute-level category) keeps technical access to every event so that it can help.

The application lets every user with management access change an event: the event team, the team
of the event's category, the teams of all higher-level categories (inherited access) and Indico
administrators. The organization's policy is narrower: an event is changed only by the people
accountable for it. Service-desk staff who need to change an event's content must first be added to
the event's team (or to the team of its category) by a member of that team; their technical access
alone does not authorize the change.

## Rules

P1. Every change to an event is accountable to the user recorded on its log row (the `user_id` of the
    `audit_log` row).

P2. A change to event E at time t by user U is authorized if and only if, at time t,
    (a) U is on the event team of E, or
    (b) U is on the category team of the category that directly contains E (the `category_id` of E
        in `events`).
    Membership of the team of any other category (a higher-level category such as the
    institute-level category, a sibling category, or the event's former category) and Indico
    administrator status do not authorize the change. If (a) and (b) are both shown not to hold, the
    change is unauthorized; if either holds, it is authorized.

P3. Team membership over time is established by the application's records of ACL changes:
    - Adding a user to the ACL of an event writes an `audit_log` row on that event with
      `module = 'Protection'`, `summary = 'ACL entry added'` and the user's full name in the JSON
      `data` under the key `User`; removing a user writes `summary = 'ACL entry removed'` in the same
      form. The same changes for a category are written to `category_log` (with the category's
      `category_id`). Rows written by one request share one timestamp; records are ordered by
      timestamp and then by id.
    - A user is on a team from the time of the `ACL entry added` row until the time of the next
      `ACL entry removed` row for the same user and the same event or category. A row with
      `summary = 'ACL entry changed'` changes only the rights of a user who stays listed; it neither
      adds nor removes the user. Rows whose `data` has no `User` key concern groups or other
      principals and are not team membership of a user.
    - A user is identified in these rows by the full name (`users.full_name`); full names are unique
      in this organization. The acting user of a change is identified by `user_id`.
    - When a user creates an event, the application lists the creator in the event's ACL without
      writing an `ACL entry added` row (only an `Event created` row is written). Such a membership is
      shown by the current team table and ends with an `ACL entry removed` row like any other.
    - The tables `event_team` and `category_team` list the teams as they are at export time.
    - Changes to Indico administrator status are recorded in `app_log` and do not matter for this
      policy. Requests the application refuses write nothing.

P4. If the available records do not establish the verdict under P2, the correct answer is
    `insufficient_evidence`. Absence of a record is only informative inside the coverage window of
    its source (table `source_coverage`). In particular, team membership at time t can be concluded
    from the current team tables only if the log of that team's ACL changes covers the whole period
    from t to the export and records no change of that membership after t.

## What to return

- `verdict`: `authorized`, `unauthorized`, or `insufficient_evidence`.
- `evidence`: the records that establish the verdict. Log rows are cited by table and id, for
  example `{"source": "audit_log", "id": 57}` or `{"source": "category_log", "id": 12}`. A conclusion
  drawn from a current team table is cited as a current-state claim, for example
  `{"source": "event_team", "event_id": 4, "user_name": "Ada Lovelace", "present": false}` or
  `{"source": "category_team", "category_id": 3, "user_name": "Ada Lovelace", "present": true}`.
- `principal` (the acting user's id, from the audited row) and `unresolved`: facts you could not
  establish.
