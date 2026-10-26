# Organization API token policy (public)

The organization runs its ticketing back office on pretix. Staff belong to teams. A team grants its
members permissions on the organizer and its events. Team API tokens are created by a team member
(or an organizer administrator) and act with the permissions of the token's team.

The application enforces team permissions. It does NOT tie a team API token to the person who
created it: a token keeps working when its creator leaves the team. The organization's policy
therefore adds an accountability rule on top of the application's permission checks.

## Rules

P1. Every team API token is accountable to its issuer: the user who created the token.

P2. An operation performed with a team API token at time t is authorized if and only if, at time t,
    the token's issuer is a member of the token's own team. Membership in any other team does not
    count. (The application already checks that the token's team has the needed permission.)

P3. Team membership at time t is the state after the last membership change at or before t.
    Membership changes are recorded in the audit log as `pretix.team.member.added` and
    `pretix.team.member.removed` on the team (`object_type = 'team'`, `object_id = team id`,
    `data.user = user id`). Current-state tables show membership at export time only.
    Application semantics: pretix records `member.removed` only for a user who was a member of that
    team immediately before, and `member.added` only for a user who was not a member immediately
    before. Inside the audit log's coverage window every membership change is recorded.

P4. The issuer of a token is established by its creation record in the audit log
    (`pretix.team.token.created` on the token's team, `data.id = token id`, `actor_user_id = issuer`).
    No other record establishes the issuer. Token names are free text and carry no accountability.

P5. If the available records do not establish both the issuer and the issuer's membership in the
    token's team at time t, the correct answer is `insufficient_evidence`. Absence of a record is
    only informative inside the audit log's coverage window (table `source_coverage`).

## What to return

- `verdict`: `authorized`, `unauthorized`, or `insufficient_evidence`.
- `evidence`: the records that establish the verdict. Audit log rows are cited by id. A claim about
  current membership is cited as `{"source": "team_members", "team_id": T, "user_id": U, "member": true|false}`.
- `issuer_user_id` (if established) and `unresolved`: facts you could not establish.
