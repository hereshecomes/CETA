# Annotation guide

These are the instructions the annotators received with the annotation packet (built by
`evaluation/annotation/make_packets.py`).

## 1 What to produce

Each annotator fills in eight forms, two for each of the four applications.

The object of annotation is an operation that the application allowed (an alert). Two questions are answered:

1. **Form 1 · world verdict**: under the organization's policy, was the operation authorized or unauthorized at
   the time t at which it was performed? Decide from the complete record; one row per world.
2. **Form 2 · view verdict**: from only the data a method actually received (a *view*), can the case be decided?
   If so, give the verdict and the evidence; if not, mark it as insufficient evidence. Each world has three views,
   so three rows.

| Application | Worlds (form 1 rows) | Views (form 2 rows) | Label prefix |
|---|---|---|---|
| pretix | 48 | 144 | PX |
| Weblate | 60 | 180 | WB |
| Wagtail | 88 | 264 | WT |
| Indico | 88 | 264 | IN |
| Total | 284 | 852 | |

## 2 What the packet contains

```
annotation_packet/
  pretix/
    policy.md                        the organization's policy (the basis of every judgment; read first)
    schema.md                        the meaning of every table and column (read first)
    worlds/PX-001.md ... PX-048.md   one file per world
    form1_world_verdicts_pretix.csv
    form2_view_verdicts_pretix.csv
  weblate/  wagtail/  indico/        same structure
```

**Each world file has three parts:**

1. **Alert**: the operation to be judged, with its log row id (`audit_log_id`), its time `ts` (that is, t), the
   action, the actor and the object.
2. **Part 1 · complete record**: all data of the world. Log tables start at their earliest row and are not
   truncated; the other tables are the current state at export time. One record per line (JSON), id first.
   Used for form 1.
3. **Part 2 · three views A / B / C**: the data a method actually saw. The three views differ only in the
   coverage of the logs. To avoid repetition, each view lists only
   - the coverage table `source_coverage`: from when each source is complete (`covered_from`);
   - for each table, its relation to the complete record: "identical to the complete record", or "ids ...
     visible, ids ... not visible", or the full content.

**Notes:**

- View letters are shuffled: A is not the most complete view, and the letters follow no pattern. The order of
  the world numbers is shuffled as well.
- All three views may be decidable, or none of them; there is no quota.
- The packet contains no answers, pattern names or method outputs.

**Two forms** (CSV; open with any spreadsheet program and save again as CSV, UTF-8):

| Form | Columns |
|---|---|
| Form 1 · world verdict | world; verdict (`authorized` or `unauthorized`); key records; uncertain ("yes" or empty); notes |
| Form 2 · view verdict | world; view (A/B/C); verdict (`authorized`, `unauthorized` or `insufficient_evidence`); evidence; uncertain; notes |

The world and view columns are pre-filled. Fill in only the remaining columns; do not add or delete rows and do
not rename columns. Write every verdict as the lowercase English code.

## 3 General rules

1. **The policy document decides.** Each application's policy.md is the only basis for judgment. The application
   notes in this guide are an introduction; where they conflict with policy.md, policy.md wins.
2. **Use only the information in the packet.** Do not read code, look at experimental results, ask anyone
   involved in the experiments, or use any AI tool to help decide. Otherwise the human labels are not independent.
3. **Work independently.** Until every annotator has completed all forms, do not discuss any specific case.
   Discussing how a sentence of a policy document should be read is allowed, but write the conclusion into the
   notes and tell the coordinator.
4. **Form 1 first, then form 2.** First decide the world verdict from the complete record, then judge the three
   views one by one. When judging a view, "forget" the complete record: use only the rows visible in that view.
5. **Flag uncertainty.** Give your most likely answer, write "yes" in the uncertain column and one sentence of
   reason in the notes. Never leave the verdict empty.
6. **Do not revise after comparing.** You may review and revise your own forms before returning them, but not
   after comparing answers with another annotator.

## 4 Form 1: world verdict from the complete record

The complete record holds all the information, so form 1 has only two options: `authorized` or `unauthorized`.
Proceed in this order:

1. **Read the alert**: note the time t (`ts`), the log row (`audit_log_id`), the acting principal (token or user)
   and the object.
2. **Determine the responsible principal** as policy.md defines it. On pretix, for example, the token's issuer is
   found through the token's creation record; on Indico and Wagtail it is the user on the log row.
3. **List the conditions the policy requires**, for example "the principal is a member of a team at t", "the
   token is valid at t", "someone else submitted the page for review within 48 hours". Note whether the
   conditions are required together (AND) or are alternatives (OR).
4. **Reconstruct the state at time t.** This is the step most prone to errors:
   - Current-state tables (such as `team_members`, `event_team`) reflect the **export time**, not t.
   - The state at t is the state after the last change before t: search backwards from t for the nearest
     "added" or "removed".
   - Alternatively, work backwards from the current table: if the first change after t is an addition, the
     subject was not a member at t; if it is a removal, the subject was. With no change at all after t, the state
     at t equals the current table.
   - Records with the same time are ordered by id: a record at the time of the alert with a smaller id counts as
     before the operation.
   - Only the team or object named by the policy counts; membership in other teams does not.
5. **Give the verdict** by combining the conditions as the policy says: among required conditions, one failing
   condition means `unauthorized`; among alternative paths, every path must fail for `unauthorized`.
6. **Fill in the key records**: the decisive records your judgment rests on, written as in section 6; usually one
   to three.

If even the complete record does not let you decide, give the most likely answer, mark it as uncertain, and
write in the notes what is missing. This should be very rare.

## 5 Form 2: can one view alone decide?

Form 2 asks whether an auditor without the complete picture, given only this view's data, could reach a
conclusion under the policy. There are three options: `authorized`, `unauthorized`, `insufficient_evidence`.

**How to read a view**

- Each log source is complete only from its `covered_from` on. Events before the coverage start are invisible
  and must **not** be treated as "did not happen".
- The current-state tables are present in every view but describe only the export time.

**Decision rules** (consistent with the policies' statement that a missing record is meaningful only inside the
coverage window):

1. **The last change before t** can be used only if it is visible in the view. If it is visible, the interval
   from it to t is covered.
2. **Inferring from the first change after t** is allowed only if the source's `covered_from` is not later than
   t; otherwise invisible changes may lie between t and the start of the coverage.
3. **Using the current-state table as the state at t** requires both that `covered_from` is not later than t
   (so everything from t to the export is covered) and that there is no relevant change for this person in that
   interval.
4. **Proving that something did not happen** (for example, no submission for review within 48 hours) requires
   the coverage to include the entire period to be checked.
5. **If the principal cannot be determined** (for example, the token's creation record lies before the
   coverage), every condition about the principal is undecidable. Do not guess from names, token names or other
   clues.
6. **Combining conditions:**
   - Among required conditions, one proven to fail → `unauthorized`, even if the others are undecidable.
   - All proven to hold → `authorized`.
   - Otherwise → `insufficient_evidence`.
   - Among alternative paths, one proven to hold → `authorized`; all proven to fail → `unauthorized`;
     otherwise → `insufficient_evidence`.
   - Each application's policy.md states how its conditions combine; it decides.

**Three common mistakes**

- Filling gaps in a view with knowledge from the complete record, or inferring the view from the form 1 verdict.
  Form 2 may well be `insufficient_evidence` when form 1 has a clear verdict.
- Taking the current-state table as the state at t without checking the preconditions of rule 3.
- Treating "not found" as "absent" without looking at the coverage.

**`insufficient_evidence` and "uncertain" are different things**: the first is your judgment of the view (the
data cannot decide); the second is your confidence in your own answer. Both can occur together.

## 6 How to write evidence

The key records of form 1 and the evidence of form 2 use the same notation; separate several items with `;`.

| Evidence | Notation | Example |
|---|---|---|
| A log row | `table#id` | `audit_log#28`, `category_log#19` |
| A fact of a current-state table | `table(current): who in / not in where` | `event_team(current): Tara Quast not in event 1` |
| A conclusion from "no record inside the coverage" | `coverage:source` | `coverage:audit_log` |

- Write the **fewest records that suffice** for the conclusion. Typically: the record that determines the
  principal (such as the token's creation record) plus the record that decides the condition (such as the last
  removal before t).
- The alert's own row may be omitted.
- In form 2, when the verdict is `insufficient_evidence`, leave the evidence empty and write in the notes what is
  missing, for example "the token's creation record lies before the coverage".
- The evidence in form 2 must be visible in that view.

## 7 Notes on the four applications

This is an introduction only; read each application's policy.md and schema.md in full before starting.

| Application | Responsible principal | Authorization conditions | Key tables and records |
|---|---|---|---|
| pretix (ticketing) | the alert's `actor_token_id` → in `audit_log`, the row `pretix.team.token.created` whose `data.id` is that token; its `actor_user_id` is the issuer. Only this record determines the issuer | the issuer is a member at t of the team the token belongs to (`team_tokens`); other teams do not count | `pretix.team.member.added / removed` (`object_id` = team, `data.user` = user); current table `team_members`. If the issuer cannot be determined, the answer can only be insufficient evidence |
| Weblate (translation) | the alert's `actor_user_id` is the token's bot user → in `account_log`, the `actor_username` of the `token-created` row is the creator | both required: (a) the token is valid at t (created, not expired); (b) the creator is a member at t of the token's team. Either proven to fail → unauthorized | `team-add / team-remove` and `disabled-expiry` in `account_log`; `token_validity` (lists only tokens still valid at export, with its own coverage start); `tokens.team`; an expiry date counts until 23:59:59 of that day |
| Wagtail (content publication) | `user_id` of the alert's `wagtail.publish` row | both required: (a) within the 48 hours before publication, someone other than the publisher submitted the page for review (`wagtail.workflow.start`); (b) in the same window, the published revision has an approval (`wagtail.workflow.approve` with the same `revision_id`) | `resume` does not restart the clock; which revision was approved is read only from the approve row's `revision_id`; proving "none within 48 hours" requires coverage of the whole window |
| Indico (conferences) | `user_id` of the alert row (full name in `users`) | either suffices: (a) on the event's team; (b) on the team of the event's direct category. Higher categories and administrator status do not count. Unauthorized only if both are proven false | `ACL entry added / removed` in `audit_log` (event) and `category_log` (category); members are recorded by full name in `data.User`; `changed` is neither an addition nor a removal; the creator of an event is on its team automatically, without an `added` row; current tables `event_team`, `category_team` |

## 8 A complete example (invented; not in the packet)

**Alert** (Indico): user 7 (full name Dana Lee) edited event 3 at t = March 12, 10:00. Event 3 belongs to
category 2.

**Relevant rows of the complete record:**

- `audit_log#812`: March 5, event 3, `ACL entry removed`, User = Dana Lee.
- `category_log#57`: March 20, category 2, `ACL entry added`, User = Dana Lee.
- Current tables: `event_team` does not list Dana Lee for event 3; `category_team` lists Dana Lee for category 2.

**Form 1**: event team: the last change before t is the removal on March 5 → not a member at t. Category team: no
change before t; the first change after t is the addition on March 20 → not a member at t. Both paths fail. The
current category team lists her, but that is the state after March 20.

| World | Verdict | Key records |
|---|---|---|
| IN-example | unauthorized | audit_log#812; category_log#57 |

**Form 2**, three views with different coverage starts:

| View | Coverage start | Verdict | Evidence | Notes |
|---|---|---|---|---|
| A | both logs from March 1 | unauthorized | audit_log#812; category_log#57 | as in form 1; both rows are visible |
| B | audit_log from March 1; category_log from March 15 | insufficient_evidence | | the category log's coverage starts after t: #57 is visible but cannot be shown to be the first change after t, and the current table cannot be used either. The event path fails, the category path is undecidable |
| C | audit_log from March 10; category_log from March 1 | unauthorized | event_team(current): Dana Lee not in event 3; coverage:audit_log; category_log#57 | #812 is not visible, but the event log covers t through the export and shows no change for her after t, so the current table is the state at t |

View C shows that an invisible record does not necessarily make a view undecidable: check whether the
conclusion follows from other visible data.

## 9 Returning the forms

1. Each annotator unpacks their own copy of the packet and fills in only that copy.
2. When done, append your code to the names of the eight CSV files, for example
   `form1_world_verdicts_pretix_A.csv`. A letter or a number suffices.
3. Return the eight files to the coordinator. If you agreed on a shared reading of a policy before starting,
   attach a short note.
4. Applications may be returned one at a time.

Annotators do not see the labels derived from the execution ledger before returning their forms.

## 10 Frequently asked questions

**Which tool should I use to read the world files?** Any text editor; search for ids, user names or action names.
You may keep draft notes, but do not use AI tools to help decide.

**Which time zone are the timestamps in?** All are UTC (`+00:00`); timestamps can be compared as strings.

**Does a change at the same moment as the alert count as before or after?** By id: a change with a smaller id
than the alert row counts as before the operation. Each policy.md explains the rows written by a single request.

**Can the alert row itself lie outside the coverage?** No; every view shows the alert row.

**Names or token names seem to reveal the issuer; may I use them?** No. Use only the records the policy names.
Token names are free text and do not indicate ownership.

**What if the data seem contradictory?** Do not "correct" the data. Answer by your understanding, mark the answer
as uncertain, and write in the notes which rows conflict and how.

**The CSV looks garbled in my spreadsheet program.** The files are UTF-8 with a byte-order mark. If they look
garbled, import them as a UTF-8 text/CSV file, and save as "CSV UTF-8".

**How much evidence should I write?** The fewest records that suffice for the conclusion. Writing more is not
wrong.

**Halfway through, I realize I misread a rule of a policy.** Correct the rows you already filled in, and say when
returning the forms what you changed and why.
