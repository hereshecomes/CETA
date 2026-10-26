# Investigation database (read-only SQLite)

- `audit_log(id, ts, action, user_id, page_id, revision_id, content_changed, data)` Page log
  exported from Wagtail (one row per recorded action on a page). `ts` is ISO-8601 UTC. `action` is
  Wagtail's action string: `wagtail.create`, `wagtail.edit` (a revision was saved), `wagtail.publish`
  (a revision went live), `wagtail.unpublish`, `wagtail.workflow.start` (submitted for moderation),
  `wagtail.workflow.approve`, `wagtail.workflow.reject`, `wagtail.workflow.resume` (re-submitted
  after changes were requested), `wagtail.workflow.cancel`, `wagtail.rename`, among others.
  `user_id` is the account that performed the action. `page_id` is the page; `revision_id` is the
  revision the action refers to (NULL when none). `data` is the JSON payload written by the
  application (for workflow actions: workflow and task names, task state id, comment). Only rows
  inside the coverage window are present.
- `model_log(id, ts, action, user_id, object_type, object_id, data)` Log of actions on non-page
  objects (users, groups) exported from Wagtail: `action` is `wagtail.create`, `wagtail.edit` or
  `wagtail.delete`; `object_type` is `auth.user` or `auth.group`; `object_id` the object's id;
  `data` is the application's payload (empty for these actions). Only rows inside the coverage
  window are present.
- `users(id, username, first_name, last_name, is_superuser, is_active)` current state.
- `groups(id, name)` and `group_members(group_name, username)` current state of the groups and
  their members.
- `pages(id, title, slug, parent_id, live, live_revision_id, latest_revision_id,
  first_published_at, last_published_at)` current state of the site's pages.
- `revisions(id, page_id, created_at)` current state: every saved revision of every page.
- `workflows(id, name)`, `workflow_tasks(workflow_id, sort_order, task_name, approver_group)` (the
  group whose members may approve the task) and `workflow_pages(page_id, workflow_id)` (the section
  page a workflow is assigned to; it applies to the pages below it) current state.
- `source_coverage(source, covered_from, covered_to, note)` what each source covers.

Publications appear in `audit_log` as rows with `action = 'wagtail.publish'`; their `user_id` is
the publisher, `page_id` the page and `revision_id` the revision that went live. The workflow state
tables of the application are not part of the export; workflow actions are recorded in `audit_log`.
