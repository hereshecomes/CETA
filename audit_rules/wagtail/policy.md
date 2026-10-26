# Organization content publication policy (public)

The organization runs its website on Wagtail. Authors write and edit pages; moderators (the members
of the group named in the moderation workflow) review them. A page goes live when one of its
revisions is *published*. The intended procedure is: an author submits a page for moderation, a
moderator reviews it and approves it, and the approval publishes the page.

The application enforces page permissions (who may edit, who may publish) and the mechanics of the
workflow, but it does NOT enforce the organization's review rules: a user with publish permission
can publish a page directly, with no review at all, even while a review is in progress; a moderator
can approve a page they submitted themselves; nothing limits how old a submission may be when it is
approved; and on approval the application publishes the page's latest revision, which can differ
from the revision that was reviewed. The organization's policy therefore adds the following rules on
top of the application's checks.

## Rules

P1. Every publication is accountable to the user recorded on its publish record (the `user_id` of
    the `wagtail.publish` row in `audit_log`). When the application publishes a page because a
    workflow was approved, that user is the approving moderator.

P2. A publication of revision r of page g at time t by user P is authorized if and only if, at
    time t, both of the following hold:
    (a) the page was submitted for moderation by someone other than P within the 48 hours before the
        publication: a `wagtail.workflow.start` row on page g with `user_id` different from P,
        recorded before the publication (see P3 for the order of records) and not more than 48 hours
        before t;
    (b) the revision that went live was approved within the same window: a `wagtail.workflow.approve`
        row with `revision_id = r`, recorded before the publication and not more than 48 hours
        before t.
    If either (a) or (b) is shown not to hold, the publication is unauthorized, even if the other
    cannot be established. Both must hold for a publication to be authorized.

P3. Application semantics that matter. All rows below are `audit_log` rows on the page. Rows written
    during one request share one timestamp; records are ordered by timestamp and then by id, so a
    record counts as recorded before the publication if its timestamp is earlier than the publish
    row's, or equal with a smaller id.
    - "Submit for moderation" writes `wagtail.edit` (the revision that is submitted) followed by
      `wagtail.workflow.start` (`user_id` = the submitter, `revision_id` = that revision).
    - Approving the final task of the workflow writes `wagtail.workflow.approve` (`user_id` = the
      approver, `revision_id` = the revision the task was opened on) immediately followed by
      `wagtail.publish` (`user_id` = the same approver, `revision_id` = the page's latest revision
      at that moment, which is the approved revision unless someone saved a newer draft during the
      review). An approval of a non-final task writes only `wagtail.workflow.approve`.
    - "Publish" in the page editor writes `wagtail.edit` (a new revision) followed by
      `wagtail.publish` by the same user; if a review was in progress it is cancelled
      (`wagtail.workflow.cancel`). The application allows this for every user with publish
      permission, whether or not the page was ever submitted or approved.
    - "Request changes" writes `wagtail.workflow.reject`. A re-submission after that writes
      `wagtail.edit` and `wagtail.workflow.resume`, not a new `wagtail.workflow.start`; the window
      of P2(a) runs from the `wagtail.workflow.start` row and a `resume` row does not restart it.
    - Cancelling a review writes `wagtail.workflow.cancel`. Saving a draft writes `wagtail.edit`
      only.
    - Requests the application refuses write nothing. A "Publish" by a user without publish
      permission is treated as saving a draft (`wagtail.edit` only, no `wagtail.publish`).
    - The application does not record who is a moderator over time. Changes to users and groups
      appear in `model_log` only as generic `wagtail.edit` rows without details. Group membership
      is therefore not a condition of this policy; the application already restricts approving to
      the members of the task's group.

P4. The submitter of a page is established only by the `wagtail.workflow.start` row (its
    `user_id`). The author of a revision, the owner of a page and `wagtail.edit` rows do not
    establish it. The approved revision is established only by the `revision_id` of the
    `wagtail.workflow.approve` row.

P5. If the available records do not establish the verdict under P2, the correct answer is
    `insufficient_evidence`. Absence of a record is only informative inside the coverage window of
    its source (table `source_coverage`): that no qualifying submission or approval exists in the
    48 hours before t can only be concluded if `audit_log` covers that whole period.

## What to return

- `verdict`: `authorized`, `unauthorized`, or `insufficient_evidence`.
- `evidence`: the records that establish the verdict. Log rows are cited by table and id, for
  example `{"source": "audit_log", "id": 57}`. A conclusion drawn from the absence of a record is
  cited by the coverage record of the source, `{"source": "source_coverage", "for": "audit_log"}`.
- `principal` (the publisher's user id, from the publish row) and `unresolved`: facts you could not
  establish.
