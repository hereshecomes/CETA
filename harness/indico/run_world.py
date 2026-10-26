"""Execute one world spec through Indico 3.3.14 and save the private ledger and state dump.

Usage: run_world.py SPEC_JSON OUT_DIR [REDIS_DB]
Outputs (private, never shown to agents): ledger.json, world_full.json, status.json.

Every step after the setup goes through Indico's own views with the acting user's session (Flask test
client; CSRF token in the session and the X-CSRF-Token header, as the browser sends it); the clock is
time-machine, frozen inside a request (rows written by one request share the step's timestamp and are
ordered by id). Users, categories, events, initial teams and administrators are created at setup, 31
days before the window, through Indico's ORM operations (the initial ACL entries are written with
Indico's own update_principal, which logs them at setup time).
"""
import json
import sys
import traceback
import zlib
from datetime import datetime, timedelta, timezone as dt_tz
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import indico_env  # noqa: E402

spec = json.loads(Path(sys.argv[1]).read_text())
OUT = Path(sys.argv[2])
REDIS_DB = int(sys.argv[3]) if len(sys.argv) > 3 else 2
OUT.mkdir(parents=True, exist_ok=True)
base = spec["base"]
DB = "w_" + format(zlib.crc32(spec["world_id"].encode()), "08x")
WORK = indico_env.WORK_ROOT / spec["world_id"]
server = indico_env.start_cluster()
indico_env.create_db(server, DB, template=indico_env.TEMPLATE_DB)
app = indico_env.configure(DB, WORK / "data", redis_db=REDIS_DB)

import time_machine  # noqa: E402
from flask import session as flask_session  # noqa: E402

from indico.core.db import db  # noqa: E402
from indico.modules.categories.models.categories import Category  # noqa: E402
from indico.modules.events.models.events import Event, EventType  # noqa: E402
from indico.modules.events.operations import create_event  # noqa: E402
from indico.modules.logs.models.entries import AppLogEntry, CategoryLogEntry, EventLogEntry  # noqa: E402
from indico.modules.users.models.users import User  # noqa: E402

T0 = datetime(2026, 3, 2, 8, 0, tzinfo=dt_tz.utc) + timedelta(days=zlib.crc32(base["base_id"].encode()) % 60)
SETUP = T0 - timedelta(days=31)
CSRF = "csrf-" + format(zlib.crc32(base["base_id"].encode()), "08x")
EXPECTED = {"edit": {200}, "event_acl_add": {302}, "event_acl_remove": {302}, "category_acl_add": {302},
            "category_acl_remove": {302}, "admin_grant": {302}, "admin_revoke": {302}}
WANT = {"edit": ("event_log", "management", None), "event_acl_add": ("event_log", "management", "ACL entry added"),
        "event_acl_remove": ("event_log", "management", "ACL entry removed"),
        "category_acl_add": ("category_log", "category", "ACL entry added"),
        "category_acl_remove": ("category_log", "category", "ACL entry removed"),
        "admin_grant": ("app_log", "admin", None), "admin_revoke": ("app_log", "admin", None)}


def at(minutes):
    return time_machine.travel(T0 + timedelta(minutes=minutes), tick=False)


def ser(v):
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if hasattr(v, "name") and hasattr(v, "value"):
        return v.name
    return v


LOGS = (("event_log", EventLogEntry, "event_id"), ("category_log", CategoryLogEntry, "category_id"),
        ("app_log", AppLogEntry, None))


def row_dict(r, fk):
    d = {"id": r.id, "ts": ser(r.logged_dt), "user_id": r.user_id, "realm": ser(r.realm), "kind": ser(r.kind),
         "module": r.module, "type": r.type, "summary": r.summary, "data": r.data, "meta": r.meta}
    if fk:
        d[fk] = getattr(r, fk)
    return d


def max_ids():
    return {n: (m.query.with_entities(m.id).order_by(m.id.desc()).first() or (0,))[0] for n, m, _ in LOGS}


def rows_after(before):
    return {n: [row_dict(r, fk) for r in m.query.filter(m.id > before[n]).order_by(m.id)] for n, m, fk in LOGS}


def client_for(user):
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = user.id  # what IndicoSession.set_session_user stores (its sanity check needs a live request)
        s["_csrf_token"] = CSRF
    return c


def acl_list(obj):
    out = []
    for e in obj.acl_entries:
        if e.principal.principal_type.name != "user":
            continue
        perms = set(e.permissions)
        if e.full_access:
            perms.add("_full_access")
        if e.read_access:
            perms.add("_read_access")
        if perms:
            out.append([{"identifier": e.principal.identifier}, sorted(perms)])
    return out


users, cats, events, ids, cat_ids, ev_ids = {}, {}, {}, {}, {}, {}
with app.app_context():
    with time_machine.travel(SETUP, tick=False):
        root = Category.get_root()
        for role, u in base["users"].items():
            user = User(first_name=u["first_name"], last_name=u["last_name"],
                        email=f"{u['first_name'].lower()}.{u['last_name'].lower()}@corp.example",
                        is_admin=role in base["initial_admins"])
            db.session.add(user)
            users[role] = user
        db.session.flush()
        cats["Institute"] = Category(parent=root, title="Institute")
        db.session.add(cats["Institute"])
        db.session.flush()
        for key, title in base["categories"].items():
            cats[key] = Category(parent=cats["Institute"], title=title)
            db.session.add(cats[key])
        db.session.flush()
        with app.test_request_context():
            flask_session["_user_id"] = users["adm1"].id  # set_session_user would memoize "no user" for this request
            for key, (cat, kind) in base["events"].items():
                events[key] = create_event(cats[cat], EventType.meeting,
                                           {"title": f"{kind} ({base['categories'][cat]})",
                                            "start_dt": T0 + timedelta(days=21), "end_dt": T0 + timedelta(days=21, hours=3),
                                            "timezone": "UTC"}, add_creator_as_manager=False)
            db.session.flush()
            for key, members in base["initial_category_teams"].items():
                for role in members:
                    cats[key].update_principal(users[role], full_access=True)
            for key, members in base["initial_event_teams"].items():
                for role in members:
                    events[key].update_principal(users[role], full_access=True)
        db.session.commit()
    ids = {k: u.id for k, u in users.items()}
    cat_ids = {k: c.id for k, c in cats.items()}
    ev_ids = {k: e.id for k, e in events.items()}


def do_step(step):
    kind, actor = step["kind"], step["actor"]
    user = db.session.get(User, ids[actor])
    c = client_for(user)
    h = {"X-CSRF-Token": CSRF}
    if kind == "edit":
        ev = db.session.get(Event, ev_ids[step["event"]])
        title, desc = ev.title, ev.description or ""
        if step["field"] == "title":
            title = f"{title.split(' [')[0]} [rev {step['t']}]"
        else:
            desc = f"Programme notes, revision {step['t']}."
        return c.post(f"/event/{ev.id}/manage/settings/data",
                      data={"title": title, "description": desc, "url_shortcut": "", "csrf_token": CSRF}, headers=h)
    if kind.startswith("event_acl_") or kind.startswith("category_acl_"):
        is_event = kind.startswith("event_acl_")
        obj = db.session.get(Event, ev_ids[step["event"]]) if is_event else db.session.get(Category, cat_ids[step["category"]])
        ident = db.session.get(User, ids[step["user"]]).identifier  # signed, as the user search returns it
        cur = [x for x in acl_list(obj) if x[0]["identifier"] != ident]
        if kind.endswith("_add"):
            cur.append([{"identifier": ident}, ["_full_access"]])
        data = {"permissions": json.dumps(cur), "protection_mode": "inheriting", "own_no_access_contact": "",
                "visibility": "", "csrf_token": CSRF}
        if is_event:
            data.update({"access_key": ""})
            if obj.subcontrib_speakers_can_submit:
                data["subcontrib_speakers"] = "y"  # unchanged setting, as the rendered form submits it
            return c.post(f"/event/{obj.id}/manage/protection", data=data, headers=h)
        data.update({"event_creation_mode": "restricted"})
        return c.post(f"/category/{obj.id}/manage/protection", data=data, headers=h)
    if kind in ("admin_grant", "admin_revoke"):
        admins = {u.id for u in User.query.filter_by(is_admin=True, is_deleted=False)}
        if kind == "admin_grant":
            admins.add(ids[step["user"]])
        else:
            admins.discard(ids[step["user"]])
        return c.post("/admin/admins", data={"admins": json.dumps([db.session.get(User, i).identifier for i in sorted(admins)]),
                                             "csrf_token": CSRF}, headers=h)
    raise ValueError(kind)


ledger = []
status = {"world_id": spec["world_id"], "ok": True, "failures": []}
for step in spec["timeline"]:
    rec = {**step, "minute": step["t"], "t_iso": (T0 + timedelta(minutes=step["t"])).isoformat()}
    with app.app_context():
        before = max_ids()
        try:
            with at(step["t"]):
                r = do_step(step)
            rec["status"] = r.status_code
            rec["ok"] = r.status_code in EXPECTED[step["kind"]]
            if not rec["ok"]:
                rec["body_prefix"] = r.get_data(as_text=True)[:300]
        except Exception as exc:  # recorded, never silently dropped
            rec["status"] = None
            rec["ok"] = False
            rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
            rec["traceback"] = traceback.format_exc()[-1200:]
        db.session.rollback()
        db.session.expire_all()
        rec["rows"] = rows_after(before)
        rec["actor_user_id"] = ids[step["actor"]]
        if "event" in step:
            rec["event_id"] = ev_ids[step["event"]]
        if "category" in step:
            rec["category_id"] = cat_ids[step["category"]]
        if "user" in step:
            rec["subject_user_id"] = ids[step["user"]]
        table, realm, summary = WANT[step["kind"]]
        got = [x for x in rec["rows"][table] if x["realm"] == realm and (summary is None or x["summary"] == summary)
               and x["user_id"] == ids[step["actor"]]]
        if rec["ok"] and not got:
            rec["ok"] = False
            rec["error"] = f"expected a {table} row ({realm}, {summary}) by the actor; got {rec['rows']}"[:600]
    if not rec["ok"]:
        status["failures"].append({"minute": step["t"], "kind": step["kind"], "focal": step.get("focal"),
                                   "status": rec.get("status"), "error": rec.get("error")})
        if step.get("focal"):
            status["ok"] = False
    ledger.append(rec)

with app.app_context():
    def team(obj):
        return sorted(e.principal.id for e in obj.acl_entries if e.principal.principal_type.name == "user")

    dump = {
        "T0": T0.isoformat(),
        "users": [{"id": u.id, "full_name": u.full_name, "email": u.email, "is_admin": u.is_admin}
                  for u in User.query.filter_by(is_system=False).order_by(User.id)],
        "categories": [{"id": c.id, "title": c.title, "parent_id": c.parent_id} for c in Category.query.order_by(Category.id)],
        "events": [{"id": e.id, "title": e.title, "category_id": e.category_id, "start_dt": e.start_dt.isoformat(),
                    "end_dt": e.end_dt.isoformat()} for e in Event.query.order_by(Event.id)],
        "event_team": {str(e.id): team(e) for e in Event.query.order_by(Event.id)},
        "category_team": {str(c.id): team(c) for c in Category.query.order_by(Category.id)},
    }
    for n, m, fk in LOGS:
        dump[n] = [row_dict(r, fk) for r in m.query.order_by(m.id)]
(OUT / "ledger.json").write_text(json.dumps({"spec": spec, "T0": T0.isoformat(), "steps": ledger, "user_ids": ids,
                                             "category_ids": cat_ids, "event_ids": ev_ids}, indent=1, default=str))
(OUT / "world_full.json").write_text(json.dumps(dump, indent=1, default=str))
(OUT / "status.json").write_text(json.dumps(status, indent=1))
indico_env.drop_db(server, DB)
indico_env.remove_work(WORK)
print(json.dumps({"world_id": spec["world_id"], "ok": status["ok"], "n_failures": len(status["failures"])}))
