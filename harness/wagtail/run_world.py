"""Execute one world spec through Wagtail and save the private ledger and state dump.

Usage: run_world.py SPEC_JSON OUT_DIR
Outputs (private, never shown to agents): ledger.json, world_full.json, status.json.

Every step after the setup goes through Wagtail's own admin views with the acting user's session
(Django test client); the clock is time-machine (frozen inside a request so that the rows written
by one request share the step's timestamp and are ordered by id). The organisation, users, groups, page permissions,
pages and the moderation workflow are created via ORM at setup, 31 days before the window.
"""
import json
import sys
import traceback
import zlib
from datetime import datetime, timedelta, timezone as dt_tz
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wagtail_env  # noqa: E402
import worlds  # noqa: E402

spec = json.loads(Path(sys.argv[1]).read_text())
OUT = Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
WORK = wagtail_env.WORK_ROOT / spec["world_id"]
wagtail_env.ensure_template()
DB = wagtail_env.clone_template(WORK / "db.sqlite3")
wagtail_env.configure(DB, WORK / "data")

import time_machine  # noqa: E402
from django.contrib.auth.models import Group, Permission, User  # noqa: E402
from django.db import connections  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import reverse  # noqa: E402
from django.utils.text import slugify  # noqa: E402
from wagtail.models import (  # noqa: E402
    GroupApprovalTask, GroupPagePermission, ModelLogEntry, Page, PageLogEntry, Revision, Workflow,
    WorkflowPage, WorkflowTask,
)
from testsite.models import ArticlePage, SectionPage  # noqa: E402

base = spec["base"]
T0 = datetime(2026, 3, 2, 8, 0, tzinfo=dt_tz.utc) + timedelta(days=zlib.crc32(base["base_id"].encode()) % 60)
SETUP = T0 - timedelta(days=31)
EXPECTED = {"submit": {302}, "resubmit": {302}, "approve": {302}, "reject": {302}, "direct_publish": {302},
            "save_draft": {302}, "cancel": {200}, "user_edit": {302}}


def at(minutes):
    # Frozen clock per request: every row a request writes carries the step's timestamp, so paired
    # worlds and twins are byte-identical except for the decisive records (order within a request = id).
    return time_machine.travel(T0 + timedelta(minutes=minutes), tick=False)


def session(user):
    c = Client()
    c.force_login(user)
    return c


users, groups, pages, ledger = {}, {}, {}, []
with time_machine.travel(SETUP, tick=False):
    Group.objects.filter(name__in=["Editors", "Moderators"]).delete()  # Wagtail's default groups
    access_admin = Permission.objects.get(content_type__app_label="wagtailadmin", codename="access_admin")
    for name in (worlds.GROUP_AUTHORS, worlds.GROUP_MODERATORS):
        g = Group.objects.create(name=name)
        g.permissions.add(access_admin)
        groups[name] = g
    for role, handle in base["users"].items():
        email = f"{handle}@corp.example"
        names = {"first_name": handle[1:].capitalize(), "last_name": "Staff"}  # the admin user form requires names
        if role.startswith("adm"):
            users[role] = User.objects.create_superuser(handle, email, None, **names)
        else:
            users[role] = User.objects.create_user(handle, email, None, **names)
    for gname, members in base["initial"].items():
        for role in members:
            users[role].groups.add(groups[gname])
    home = Page.objects.get(id=2)
    section = SectionPage(title="News", slug="news")
    home.add_child(instance=section)
    for gname, types in ((worlds.GROUP_AUTHORS, ("add", "change")), (worlds.GROUP_MODERATORS, ("add", "change", "publish"))):
        for t in types:
            GroupPagePermission.objects.create(group=groups[gname], page=section, permission_type=t)
    for i, title in enumerate(base["pages"]):
        a = ArticlePage(title=title, slug=slugify(title), body=f"{slugify(title)} draft", owner=users["adm1"], live=False)
        section.add_child(instance=a)
        a.save_revision(user=users["adm1"])
        pages[i] = a
    wf = Workflow.objects.create(name="Editorial review")
    task = GroupApprovalTask.objects.create(name="Moderator approval")
    task.groups.add(groups[worlds.GROUP_MODERATORS])
    WorkflowTask.objects.create(workflow=wf, task=task, sort_order=1)
    WorkflowPage.objects.create(workflow=wf, page=section)

op_counter = 0


def page_post(role, page_index, action, extra=None):
    """POST the page editor form for the page's latest content; `action` is the action button."""
    global op_counter
    page = ArticlePage.objects.get(pk=pages[page_index].pk)
    latest = page.get_latest_revision_as_object() if page.get_latest_revision() else page
    data = {"title": latest.title, "slug": latest.slug, "body": latest.body}
    if action in ("submit", "publish", "save"):
        op_counter += 1
        data["body"] = f"{latest.slug} v{op_counter}"
    if action in ("submit", "publish", "cancel-workflow"):
        data[f"action-{action}"] = "True"
    if extra:
        data.update(extra)
    return session(users[role]).post(f"/admin/pages/{page.pk}/edit/", data)


def workflow_post(role, page_index, action, data=None):
    page = Page.objects.get(pk=pages[page_index].pk)
    ts = page.current_workflow_task_state
    return session(users[role]).post(f"/admin/pages/workflow/action/{page.pk}/{action}/{ts.id if ts else 0}/", data or {})


def user_edit(role, target_role, group_names):
    target = users[target_role]
    data = {"username": target.username, "email": target.email, "first_name": target.first_name,
            "last_name": target.last_name, "is_active": "on", "password1": "", "password2": "",
            "groups": [groups[g].pk for g in group_names]}
    return session(users[role]).post(reverse("wagtailusers_users:edit", args=(target.pk,)), data)


def row_dict(r):
    return {"id": r.id, "ts": r.timestamp.isoformat(), "action": r.action, "user_id": r.user_id, "page_id": r.page_id,
            "revision_id": r.revision_id, "content_changed": r.content_changed, "data": r.data, "label": r.label}


def mrow_dict(r):
    return {"id": r.id, "ts": r.timestamp.isoformat(), "action": r.action, "user_id": r.user_id,
            "object_type": f"{r.content_type.app_label}.{r.content_type.model}" if r.content_type else None,
            "object_id": r.object_id, "data": r.data}


def max_ids():
    p = PageLogEntry.objects.order_by("-id").values_list("id", flat=True).first() or 0
    m = ModelLogEntry.objects.order_by("-id").values_list("id", flat=True).first() or 0
    return p, m


status = {"world_id": spec["world_id"], "ok": True, "failures": []}
for step in spec["timeline"]:
    kind = step["kind"]
    rec = {**step, "minute": step["t"], "t_iso": (T0 + timedelta(minutes=step["t"])).isoformat()}
    p0, m0 = max_ids()
    try:
        with at(step["t"]):
            if kind in ("submit", "resubmit"):
                r = page_post(step["actor"], step["page"], "submit")
            elif kind == "direct_publish":
                r = page_post(step["actor"], step["page"], "publish")
            elif kind == "save_draft":
                r = page_post(step["actor"], step["page"], "save")
            elif kind == "cancel":
                r = page_post(step["actor"], step["page"], "cancel-workflow")
            elif kind == "approve":
                r = workflow_post(step["actor"], step["page"], "approve")
            elif kind == "reject":
                r = workflow_post(step["actor"], step["page"], "reject", {"comment": "Please revise."})
            elif kind == "user_edit":
                r = user_edit(step["actor"], step["user"], step["groups"])
            else:
                raise ValueError(kind)
        rec["status"] = r.status_code
        rec["ok"] = r.status_code in EXPECTED[kind]
        if not rec["ok"]:
            rec["body_prefix"] = r.content[:200].decode("utf-8", "replace")
        rec["rows"] = [row_dict(x) for x in PageLogEntry.objects.filter(id__gt=p0).order_by("id")]
        rec["model_rows"] = [mrow_dict(x) for x in ModelLogEntry.objects.filter(id__gt=m0).order_by("id")]
        if "page" in step:
            rec["page_id"] = pages[step["page"]].pk
        rec["actor_user_id"] = users[step["actor"]].pk
        # Sanity: the expected record kinds of each step (application semantics as probed).
        acts = [x["action"] for x in rec["rows"]]
        want = {"submit": ["wagtail.workflow.start"], "resubmit": ["wagtail.workflow.resume"], "approve": ["wagtail.workflow.approve"],
                "reject": ["wagtail.workflow.reject"], "direct_publish": ["wagtail.publish"], "save_draft": ["wagtail.edit"],
                "cancel": ["wagtail.workflow.cancel"], "user_edit": []}[kind]
        if rec["ok"] and not all(w in acts for w in want):
            rec["ok"] = False
            rec["error"] = f"expected rows {want}, got {acts}"
    except Exception as exc:  # recorded, never silently dropped
        rec["status"] = None
        rec["ok"] = False
        rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
        rec["traceback"] = traceback.format_exc()[-800:]
    if not rec["ok"]:
        status["failures"].append({"minute": step["t"], "kind": kind, "focal": step.get("focal"),
                                   "status": rec.get("status"), "error": rec.get("error")})
        if step.get("focal"):
            status["ok"] = False
    ledger.append(rec)

section_pk = section.pk
dump = {
    "T0": T0.isoformat(),
    "users": list(User.objects.order_by("pk").values("id", "username", "first_name", "last_name", "is_superuser", "is_active")),
    "groups": [{"id": g.pk, "name": g.name, "members": sorted(g.user_set.values_list("id", flat=True))}
               for g in Group.objects.order_by("pk")],
    "pages": [{"id": p.pk, "title": p.title, "slug": p.slug, "parent_id": p.get_parent().pk, "live": p.live,
               "live_revision_id": p.live_revision_id, "latest_revision_id": p.latest_revision_id,
               "first_published_at": p.first_published_at.isoformat() if p.first_published_at else None,
               "last_published_at": p.last_published_at.isoformat() if p.last_published_at else None}
              for p in Page.objects.filter(depth__gte=3).order_by("pk")],
    "revisions": [{"id": r.pk, "page_id": int(r.object_id), "created_at": r.created_at.isoformat(), "user_id": r.user_id}
                  for r in Revision.objects.order_by("pk")],
    "workflows": [{"id": w.pk, "name": w.name} for w in Workflow.objects.order_by("pk")],
    "workflow_tasks": [{"workflow_id": wt.workflow_id, "sort_order": wt.sort_order, "task_name": wt.task.name,
                        "approver_group": ", ".join(wt.task.specific.groups.values_list("name", flat=True))}
                       for wt in WorkflowTask.objects.order_by("pk")],
    "workflow_pages": [{"page_id": wp.page_id, "workflow_id": wp.workflow_id} for wp in WorkflowPage.objects.order_by("pk")],
    "page_log": [row_dict(r) for r in PageLogEntry.objects.order_by("id")],
    "model_log": [mrow_dict(r) for r in ModelLogEntry.objects.select_related("content_type").order_by("id")],
}
(OUT / "ledger.json").write_text(json.dumps({"spec": spec, "T0": T0.isoformat(), "steps": ledger,
                                             "user_ids": {r: u.pk for r, u in users.items()},
                                             "usernames": {r: u.username for r, u in users.items()},
                                             "page_ids": {str(i): p.pk for i, p in pages.items()},
                                             "section_id": section_pk}, indent=1))
(OUT / "world_full.json").write_text(json.dumps(dump, indent=1))
(OUT / "status.json").write_text(json.dumps(status, indent=1))
connections.close_all()
wagtail_env.remove_work(WORK)
print(json.dumps({"world_id": spec["world_id"], "ok": status["ok"], "n_failures": len(status["failures"])}))
