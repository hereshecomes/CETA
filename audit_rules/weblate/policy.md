# Organization project token policy (public)

The organization runs its translation platform on Weblate. Translators, reviewers and project
administrators belong to teams of the project. Project administrators (members of the team
`Administration`, and site administrators) can create *project tokens*: API credentials that act as
their own robot user inside the project with the permissions of the team the token belongs to.

The application enforces team permissions at request time. It does NOT tie a project token to the
person who created it: a token keeps working when its creator leaves the team. It also does NOT
reject an expired token immediately: a token with an expiry date keeps working until the hourly
cleanup task disables it (see P3). The organization's policy therefore adds the following rules on
top of the application's permission checks.

## Rules

P1. Every project token is accountable to its creator: the user who created the token.

P2. An operation performed with a project token at time t is authorized if and only if, at time t,
    both of the following hold:
    (a) the token is within its validity period: it was created at or before t and its expiry date,
        if it has one, is at or after t;
    (b) the token's creator is a member of the token's team. Membership in any other team does not
        count. (The application already checks that the team has the needed permission.)
    If either (a) or (b) is shown not to hold, the operation is unauthorized, even if the other
    cannot be established.

P3. Token validity. A token is created with an optional expiry date; the application records the
    expiry as the last moment of that day (23:59:59.999999 UTC). Validity periods of the tokens
    that are currently active are listed in the table `token_validity` (`valid_from` = creation,
    `valid_to` = expiry, NULL = no expiry). Application semantics: the hourly cleanup task disables a
    token whose expiry has passed; when it does, the application clears the stored expiry date and
    writes an `account_log` row `disabled-expiry` for the token. A token disabled in this way is
    therefore no longer listed in `token_validity` and its expiry date cannot be recovered from the
    export; the coverage window of `token_validity` states from which time on the table is complete.

P4. Team membership at time t is the state after the last membership change at or before t.
    Membership changes are recorded in the account log as `team-add` and `team-remove` rows of the
    member (`account_log.username = member`, `account_log.team = team name`, `account_log.actor_username`
    = the administrator who made the change). The organization has a single project, so team names are
    unique. Current-state tables show membership at export time only. Application semantics: a
    `team-add` row is written only when the user was not a member of that team immediately before.
    A `team-remove` row is written for every removal request: removing a user from the project writes
    one `team-remove` row per team of the project, whether or not the user was a member of it; after a
    `team-remove` row the user is not a member of that team. Inside the account log's coverage window
    every membership change is recorded.

P5. The creator of a token is established by the token's creation record in the account log
    (`activity = 'token-created'`, `account_log.user_id = the token's robot user id`,
    `account_log.actor_username = creator`). No other record establishes the creator. Token names are
    free text and carry no accountability. The team a token belongs to is listed in `tokens.team`.

P6. If the available records do not establish the verdict under P2, the correct answer is
    `insufficient_evidence`. Absence of a record is only informative inside the coverage window of its
    source (table `source_coverage`).

## What to return

- `verdict`: `authorized`, `unauthorized`, or `insufficient_evidence`.
- `evidence`: the records that establish the verdict. Log rows are cited by table and id, for example
  `{"source": "account_log", "id": 12}`, `{"source": "token_validity", "id": <token user id>}`. A claim
  about current membership is cited as `{"source": "team_members", "team": T, "username": U, "present": true|false}`.
- `principal` (the creator's username, if established) and `unresolved`: facts you could not establish.
