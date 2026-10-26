"""Execute one world spec through pretix and save the private ledger and state dump.

Usage: run_world.py SPEC_JSON OUT_DIR
Outputs (private, never shown to agents): ledger.json, world_full.json, status.json.
Token secrets exist only in memory during execution and are not written anywhere.
"""
import json
import random
import sys
import traceback
import zlib
from datetime import datetime, timedelta, timezone as dt_tz
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pretix_env  # noqa: E402

spec = json.loads(Path(sys.argv[1]).read_text())
OUT = Path(sys.argv[2])
pretix_env.configure(OUT)

from django.conf import settings  # noqa: E402

# Testbed has no mail server; keep mail in memory instead of logging SMTP errors.
settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

import time_machine  # noqa: E402
from django.test import Client  # noqa: E402
from django_scopes import scopes_disabled  # noqa: E402
from pretix.base.models import (  # noqa: E402
    Event, Item, LogEntry, Order, OrderPosition, Organizer, Quota, Team, TeamAPIToken, User,
)

base = spec["base"]
T0 = datetime(2026, 3, 2, 8, 0, tzinfo=dt_tz.utc) + timedelta(days=zlib.crc32(base["base_id"].encode()) % 60)
SETUP = T0 - timedelta(days=30)
ORG = "org"
API = f"/api/v1/organizers/{ORG}"
TEAM_DEFS = {
    "admins": dict(name="Admins", all_events=True, all_event_permissions=True, all_organizer_permissions=True),
    "integrations": dict(name="Integrations", all_events=True,
                         limit_event_permissions={"event.orders:read": True, "event.orders:write": True},
                         limit_organizer_permissions={"organizer.teams:write": True}),
    "boxoffice": dict(name="Box office", all_events=False,
                      limit_event_permissions={"event.orders:read": True, "event.orders:write": True}),
}


def at(minutes):
    return time_machine.travel(T0 + timedelta(minutes=minutes), tick=False)


users, teams, pending = {}, {}, {"ev1": [], "ev2": []}
with time_machine.travel(SETUP, tick=False), scopes_disabled():
    org = Organizer.objects.create(name="Example Events", slug=ORG)
    events = {}
    for slug, title in (("ev1", "Spring Conference"), ("ev2", "Summer Workshop")):
        ev = Event.objects.create(organizer=org, name=title, slug=slug, date_from=T0 + timedelta(days=45),
                                  currency="EUR", live=True)
        item = Item.objects.create(event=ev, name="Ticket", default_price=Decimal("40.00"))
        quota = Quota.objects.create(event=ev, name="All tickets", size=500)
        quota.items.add(item)
        events[slug] = ev
        code_rng = random.Random(f"{base['base_id']}:{slug}:codes")
        for i in range(30):
            code = "".join(code_rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ3789") for _ in range(5))
            o = Order.objects.create(event=ev, code=code, status=Order.STATUS_PENDING,
                                     email=f"buyer{i}.{slug}@mail.example",
                                     total=Decimal("40.00"), datetime=SETUP, expires=T0 + timedelta(days=20),
                                     locale="en", sales_channel=org.sales_channels.get(identifier="web"))
            OrderPosition.objects.create(order=o, item=item, price=Decimal("40.00"), positionid=1)
            pending[slug].append(o.code)
    for role, handle in base["users"].items():
        users[role] = User.objects.create_user(f"{handle}@corp.example", None)
    for key, kwargs in TEAM_DEFS.items():
        teams[key] = Team.objects.create(organizer=org, **kwargs)
    teams["boxoffice"].limit_events.add(events["ev1"])
    for key, members in base["initial"].items():
        for role in members:
            teams[key].members.add(users[role])

sessions, token_clients, token_pk, ledger = {}, {}, {}, []


def session(role):
    # Fresh login per operation: pretix expires idle sessions, and the scenario clock
    # jumps by hours between steps. A login is not an authorization fact in this task.
    c = Client()
    c.force_login(users[role])
    return c


status = {"world_id": spec["world_id"], "ok": True, "failures": []}
for step in spec["timeline"]:
    kind = step["kind"]
    rec = {**step, "minute": step["t"], "t_iso": (T0 + timedelta(minutes=step["t"])).isoformat()}
    try:
        with at(step["t"]):
            if kind == "token_create":
                r = session(step["actor"]).post(f"{API}/teams/{teams[step['team']].pk}/tokens/",
                                                {"name": step["token"]}, content_type="application/json")
                expected = {201}
                if r.status_code == 201:
                    body = r.json()
                    token_clients[step["token"]] = Client(HTTP_AUTHORIZATION=f"Token {body['token']}")
                    token_pk[step["token"]] = body["id"]
                    rec["token_id"] = body["id"]
            elif kind in ("mark_paid", "extend", "session_mark_paid"):
                code = pending[step["event"]].pop(0)
                rec["order_code"] = code
                client = session(step["actor"]) if kind == "session_mark_paid" else token_clients[step["token"]]
                url = f"{API}/events/{step['event']}/orders/{code}/"
                if kind == "extend":
                    new_exp = (T0 + timedelta(minutes=step["t"], days=14)).date().isoformat()
                    r = client.post(url + "extend/", {"expires": new_exp, "force": True},
                                    content_type="application/json")
                else:
                    r = client.post(url + "mark_paid/")
                expected = {200, 403} if kind == "session_mark_paid" else {200}
                if "token" in step:
                    rec["token_id"] = token_pk.get(step["token"])
            elif kind == "member_remove":
                r = session(step["actor"]).delete(
                    f"{API}/teams/{teams[step['team']].pk}/members/{users[step['user']].pk}/")
                expected = {204}
            elif kind == "member_add":
                r = session(step["actor"]).post(f"{API}/teams/{teams[step['team']].pk}/invites/",
                                                {"email": users[step["user"]].email},
                                                content_type="application/json")
                expected = {201}
            else:
                raise ValueError(kind)
        rec["status"] = r.status_code
        rec["ok"] = r.status_code in expected
        if not rec["ok"]:
            rec["body_prefix"] = r.content[:200].decode("utf-8", "replace")
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

with scopes_disabled():
    dump = {
        "T0": T0.isoformat(), "org": ORG,
        "users": [{"id": u.pk, "email": u.email, "role": role} for role, u in users.items()],
        "teams": [{"id": t.pk, "key": k, "name": t.name, "all_events": t.all_events,
                   "limit_events": [e.slug for e in t.limit_events.all()],
                   "all_event_permissions": t.all_event_permissions,
                   "event_permissions": t.limit_event_permissions,
                   "all_organizer_permissions": t.all_organizer_permissions,
                   "organizer_permissions": t.limit_organizer_permissions,
                   "members": sorted(m.pk for m in t.members.all())} for k, t in teams.items()],
        "tokens": [{"id": tk.pk, "team_id": tk.team_id, "name": tk.name, "active": tk.active}
                   for tk in TeamAPIToken.objects.order_by("pk")],
        "events": [{"id": e.pk, "slug": e.slug, "name": str(e.name)} for e in Event.objects.order_by("pk")],
        "orders": [{"id": o.pk, "code": o.code, "event": o.event.slug, "status": o.status, "email": o.email,
                    "expires": o.expires.isoformat()} for o in Order.objects.order_by("pk")],
        "audit_log": [{"id": le.pk, "ts": le.datetime.isoformat(), "action": le.action_type,
                       "actor_user_id": le.user_id, "actor_token_id": le.api_token_id,
                       "event": le.event.slug if le.event_id else None,
                       "object_type": le.content_type.model, "object_id": le.object_id, "data": le.data}
                      for le in LogEntry.objects.select_related("event", "content_type").order_by("pk")],
    }

(OUT / "ledger.json").write_text(json.dumps({"spec": spec, "T0": T0.isoformat(), "steps": ledger,
                                             "user_ids": {r: u.pk for r, u in users.items()},
                                             "team_ids": {k: t.pk for k, t in teams.items()},
                                             "token_ids": token_pk}, indent=1))
(OUT / "world_full.json").write_text(json.dumps(dump, indent=1))
(OUT / "status.json").write_text(json.dumps(status, indent=1))
(OUT / "app.sqlite3").unlink()
print(json.dumps({"world_id": spec["world_id"], "ok": status["ok"], "n_failures": len(status["failures"])}))
