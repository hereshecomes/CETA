"""Execute one world spec through Weblate and save the private ledger and state dump.

Usage: run_world.py SPEC_JSON OUT_DIR
Outputs (private, never shown to agents): ledger.json, world_full.json, status.json.
Token secrets exist only in memory during execution and are not written anywhere.

Every step after the setup goes through Weblate's own HTTP views/API with the acting user's session
or API token; the hourly disable_expired task is invoked exactly as celery beat would invoke it.
"""
import json
import sys
import traceback
import zlib
from datetime import datetime, timedelta, timezone as dt_tz
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import weblate_env  # noqa: E402

spec = json.loads(Path(sys.argv[1]).read_text())
OUT = Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
WORK = weblate_env.WORK_ROOT / spec["world_id"]
server, host, port = weblate_env.start_cluster()
DB = "w_" + zlib.crc32(spec["world_id"].encode()).__format__("08x")
weblate_env.create_db(server, DB, template=weblate_env.TEMPLATE_DB)
weblate_env.configure(DB, host, port, WORK / "data")

import time_machine  # noqa: E402
from django.db import connections  # noqa: E402
from django.test import Client  # noqa: E402
from weblate.accounts.models import AuditLog  # noqa: E402
from weblate.auth.models import Group, TeamMembership, User  # noqa: E402
from weblate.auth.tasks import disable_expired  # noqa: E402
from weblate.trans.models import Change, Component, Project, Unit  # noqa: E402

base = spec["base"]
T0 = datetime(2026, 3, 2, 8, 0, tzinfo=dt_tz.utc) + timedelta(days=zlib.crc32(base["base_id"].encode()) % 60)
SETUP = T0 - timedelta(days=31)
PROJECT_SLUG, PROJECT_NAME = "spring", "Spring Site"
ZIP = Path(weblate_env.HERE) / "fixtures" / "translations.zip"
STATE_TRANSLATED = 20


def at(minutes):
    return time_machine.travel(T0 + timedelta(minutes=minutes), tick=False)


def session(user):
    c = Client()
    c.force_login(user)
    return c


def api(key):
    return Client(HTTP_AUTHORIZATION=f"Token {key}")


users, groups, ledger = {}, {}, []
with time_machine.travel(SETUP, tick=False):
    for role, handle in base["users"].items():
        email = f"{handle}@corp.example"
        if role in ("adm1", "adm2"):
            users[role] = User.objects.create_superuser(handle, email, None, full_name=f"User {handle}")
        else:
            users[role] = User.objects.create_user(handle, email, None, full_name=f"User {handle}")
    project = Project.objects.create(name=PROJECT_NAME, slug=PROJECT_SLUG, web="https://example.invalid/",
                                     access_control=Project.ACCESS_PROTECTED, translation_review=True)
    with open(ZIP, "rb") as fh:
        r = api(users["adm1"].auth_token.key).post(
            f"/api/projects/{PROJECT_SLUG}/components/",
            {"name": "App", "slug": "app", "file_format": "po", "filemask": "*.po", "new_base": "hello.pot",
             "zipfile": fh})
        assert r.status_code == 201, ("component creation failed", r.status_code, r.content[:300])
    component = Component.objects.get(project=project, slug="app")
    for team, members in base["initial"].items():
        for role in members:
            project.add_user(users[role], team)
    for g in Group.objects.filter(defining_project=project):
        groups[g.name] = g
    # Translatable units: non-plural units of the non-source languages, in a fixed order.
    unit_pool = list(Unit.objects.filter(translation__component=component)
                     .exclude(translation__language__code="en").exclude(source__contains="banana")
                     .order_by("translation__language__code", "pk"))

token_clients, token_user_id, user_api = {}, {}, {}


def user_client(role):
    if role not in user_api:
        user_api[role] = api(users[role].auth_token.key)
    return user_api[role]


status = {"world_id": spec["world_id"], "ok": True, "failures": []}
timeline = list(spec["timeline"])
# The deployment's hourly cleanup is part of the executed timeline.
for m in spec["cleanup_minutes"]:
    timeline.append({"t": m, "kind": "cleanup"})
timeline.sort(key=lambda s: (s["t"], s.get("focal") != "seed", s["kind"] == "cleanup"))

op_counter = 0
for step in timeline:
    kind = step["kind"]
    rec = {**step, "minute": step["t"], "t_iso": (T0 + timedelta(minutes=step["t"])).isoformat()}
    try:
        with at(step["t"]):
            if kind == "cleanup":
                disable_expired()
                r, expected = None, set()
            elif kind == "token_create":
                expires = (T0 + timedelta(days=step["expires_days"])).date().isoformat()
                r = session(step["actor"] and users[step["actor"]]).post(
                    f"/token/{PROJECT_SLUG}/create/", {"full_name": step["token"], "date_expires": expires})
                expected = {302}
                if r.status_code == 302:
                    bot = User.objects.get(is_bot=True, full_name=step["token"])
                    token_clients[step["token"]] = api(bot.auth_token.key)
                    token_user_id[step["token"]] = bot.pk
                    rec["token_id"] = bot.pk
                    rec["expires_iso"] = bot.date_expires.isoformat() if bot.date_expires else None
            elif kind in ("translate", "user_translate"):
                unit = unit_pool[step["unit"] % len(unit_pool)]
                op_counter += 1
                text = f"{unit.source.strip()[:20]} v{op_counter}"
                client = user_client(step["actor"]) if kind == "user_translate" else token_clients[step["token"]]
                r = client.patch(f"/api/units/{unit.pk}/", json.dumps({"target": [text], "state": STATE_TRANSLATED}),
                                 content_type="application/json")
                expected = {200}
                rec["unit_id"] = unit.pk
                rec["target"] = text
                if "token" in step:
                    rec["token_id"] = token_user_id.get(step["token"])
                else:
                    rec["actor_user_id"] = users[step["actor"]].pk
            elif kind == "team_add":
                r = user_client(step["actor"]).post(
                    f"/api/users/{users[step['user']].username}/groups/",
                    json.dumps({"group_id": groups[step["team"]].pk}), content_type="application/json")
                expected = {200}
            elif kind == "team_remove":
                r = user_client(step["actor"]).delete(
                    f"/api/users/{users[step['user']].username}/groups/",
                    json.dumps({"group_id": groups[step["team"]].pk}), content_type="application/json")
                expected = {200}
            elif kind == "project_remove":
                r = session(users[step["actor"]]).post(f"/access/{PROJECT_SLUG}/remove/",
                                                       {"user": users[step["user"]].username})
                expected = {302}
            else:
                raise ValueError(kind)
        if r is not None:
            rec["status"] = r.status_code
            rec["ok"] = r.status_code in expected
            if not rec["ok"]:
                rec["body_prefix"] = r.content[:200].decode("utf-8", "replace")
        else:
            rec["status"], rec["ok"] = None, True
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

dump = {
    "T0": T0.isoformat(), "project": {"id": project.pk, "slug": project.slug, "name": project.name},
    "users": [{"id": u.pk, "username": u.username, "full_name": u.full_name, "is_bot": u.is_bot,
               "is_active": u.is_active, "is_superuser": u.is_superuser, "date_joined": u.date_joined.isoformat(),
               "date_expires": u.date_expires.isoformat() if u.date_expires else None,
               "role": next((r for r, x in users.items() if x.pk == u.pk), None)}
              for u in User.objects.order_by("pk")],
    "teams": [{"id": g.pk, "name": g.name, "project_id": g.defining_project_id,
               "members": sorted(g.user_set.values_list("id", flat=True))}
              for g in Group.objects.filter(defining_project=project).order_by("pk")],
    "memberships": list(TeamMembership.objects.order_by("pk").values("id", "user_id", "group_id")),
    "components": [{"id": component.pk, "slug": component.slug, "name": component.name}],
    "units": [{"id": u.pk, "translation_id": u.translation_id, "language": u.translation.language.code,
               "source": u.source} for u in unit_pool],
    "account_log": [{"id": a.pk, "ts": a.timestamp.isoformat(), "user_id": a.user_id, "activity": a.activity,
                     "params": a.params} for a in AuditLog.objects.order_by("pk")],
    "changes": [{"id": c.pk, "ts": c.timestamp.isoformat(), "action": c.action, "user_id": c.user_id,
                 "author_id": c.author_id, "project_id": c.project_id, "component_id": c.component_id,
                 "translation_id": c.translation_id, "unit_id": c.unit_id, "target": c.target, "details": c.details}
                for c in Change.objects.filter(project=project).order_by("pk")],
}
(OUT / "ledger.json").write_text(json.dumps({"spec": spec, "T0": T0.isoformat(), "steps": ledger,
                                             "user_ids": {r: u.pk for r, u in users.items()},
                                             "usernames": {r: u.username for r, u in users.items()},
                                             "team_ids": {k: g.pk for k, g in groups.items()},
                                             "token_ids": token_user_id}, indent=1))
(OUT / "world_full.json").write_text(json.dumps(dump, indent=1))
(OUT / "status.json").write_text(json.dumps(status, indent=1))
connections.close_all()
weblate_env.drop_db(server, DB)
weblate_env.remove_work(WORK)
print(json.dumps({"world_id": spec["world_id"], "ok": status["ok"], "n_failures": len(status["failures"])}))
