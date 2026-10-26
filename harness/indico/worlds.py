"""Generate private world specifications (timelines) for the Indico event management policy task.

Mirrors harness/wagtail/worlds.py: a base event fixes the organisation, the background activity and
the focal timing; each base event yields an AUTH and an UNAUTH world differing only in the decisive
fact, plus twins used solely for the answerability check (G0.5). Specs are private.

Organisation (per base event, random names): an institute-level category "Institute" managed by the
IT service desk (svcS, svcT); group categories G (focal), H, K below it, each with a category team;
events in each group category, each with an event team. One Indico administrator (adm1) who is on no
team. The focal actor X is a service-desk member: either svcS (manager of the institute-level
category, inherited access to every event) or adm2 (an Indico administrator); the application accepts
X's changes to any event in every world. The audited operation is X's change of the focal event E
(in G) at t_s.

Scenario clock: T0 = 08:00 UTC of a day D (D varies per base event). Focal change t_s between 15:00 and
17:00 of day D (minute 420-540); export at D 23:00 (minute 900); background from D-4.

Patterns (tiers fixed before any world was executed):
  core (dev+test):  event_removal_order, event_grant_order, category_scope, category_removal_order
  holdout (test):   team_handover, readd
  event_removal_order     X added to E's team by an organiser at t_c; removed after t_s (AUTH) or
                          before t_s (UNAUTH).
  event_grant_order       X added to E's team before t_s (AUTH) or after t_s (UNAUTH, stays on it).
  category_scope          X added to the team of G (AUTH) or of the sibling category H (UNAUTH) at t_c.
  category_removal_order  X added to G's team at t_c; removed after t_s (AUTH) or before t_s (UNAUTH).
  team_handover           X added to E's team at t_c and removed before t_s; then added to G's team
                          before t_s (AUTH) or after t_s (UNAUTH).
  readd                   X added to E's team at t_c, removed before t_s, re-added before t_s (AUTH)
                          or after t_s (UNAUTH).
Every world also has X's short stand-in membership on the team of category K (added 21-35 minutes
before t_s, removed at t_s + 30): a visible distractor in V0 that the UNAUTH twins move to G.
Visibility variants (export_case.py): V0 full logs; V1a both team logs from t_s - 20 (decided by later
records and the current teams); V1b the category log only from t_s + 60, so the category team at t_s
cannot be determined: every case whose event-team value is not true is unanswerable under V1b (both
worlds of the category patterns, the UNAUTH worlds of the event patterns); their twins have identical
V1b views and the opposite latent truth.
"""
import random

PATTERNS_CORE = ("event_removal_order", "event_grant_order", "category_scope", "category_removal_order")
PATTERNS_HOLDOUT = ("team_handover", "readd")
PATTERNS = PATTERNS_CORE + PATTERNS_HOLDOUT
CATEGORY_PATTERNS = ("category_scope", "category_removal_order", "team_handover")
EXPORT_MINUTE = 900                # D 23:00
BACKGROUND_START = -4 * 24 * 60    # D-4 08:00
STANDIN_END = 30                   # stand-in membership on K ends at t_s + 30
V1B_CATEGORY_CUT = 60              # category log cut at t_s + 60 for the category patterns
V1B_EVENT_CUT = 20                 # both logs cut at t_s - 20 for the event patterns
FIRST_NAMES = ("Ada", "Bruno", "Celia", "Dario", "Elif", "Farid", "Greta", "Hugo", "Ines", "Jonas", "Kira",
               "Luca", "Mara", "Nils", "Olga", "Pavel", "Quinn", "Rosa", "Sven", "Tara", "Umar", "Vera",
               "Wim", "Xenia", "Yusuf", "Zora", "Anton", "Bianca", "Cyril", "Dora")
LAST_NAMES = ("Achterberg", "Bianchi", "Castell", "Dimitrov", "Eriksen", "Fontaine", "Gallo", "Haas",
              "Ivanova", "Jansen", "Kowalski", "Lindqvist", "Moreau", "Novak", "Okafor", "Petrov", "Quast",
              "Rinaldi", "Schulz", "Tanaka", "Ueda", "Varga", "Weber", "Xu", "Yilmaz", "Zeller", "Arnaud",
              "Brandt", "Costa", "Duarte")
GROUP_TITLES = ("Particle Physics", "Organic Chemistry", "Cell Biology", "Applied Mathematics", "Geophysics",
                "Neuroscience", "Materials Science", "Astrophysics", "Climate Research", "Robotics")
EVENT_KINDS = ("Group meeting", "Seminar", "Journal club", "Workshop", "Colloquium", "Retreat",
               "Lab tour", "Lecture series", "Summer school", "Hackathon")
ROLES = ("adm1", "adm2", "svcS", "svcT", "orgA", "orgB", "orgC", "orgD", "grpG", "grpH", "grpK", "memM")


def _names(rng, n):
    seen, out = set(), []
    while len(out) < n:
        fn, ln = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        if fn not in {x[0] for x in out} and (fn, ln) not in seen and ln not in {x[1] for x in out}:
            seen.add((fn, ln))
            out.append((fn, ln))
    return out


def base_event(pattern, index, split):
    rng = random.Random(f"indico:{pattern}:{index}")
    names = _names(rng, len(ROLES))
    users = {role: {"first_name": fn, "last_name": ln} for role, (fn, ln) in zip(ROLES, names)}
    focal_actor = "svcS" if index % 2 == 0 else "adm2"
    groups = list(GROUP_TITLES)
    rng.shuffle(groups)
    cats = {"G": groups[0], "H": groups[1], "K": groups[2]}
    kinds = list(EVENT_KINDS)
    rng.shuffle(kinds)
    # Events: E (focal) and E2 in G; F1, F2 in H; K1 in K.
    events = {"E": ("G", kinds[0]), "E2": ("G", kinds[1]), "F1": ("H", kinds[2]), "F2": ("H", kinds[3]),
              "K1": ("K", kinds[4])}
    # Initial teams (set up at SETUP through the application's own ACL update, before any coverage).
    initial_event_teams = {"E": ["orgA"], "E2": ["orgB"], "F1": ["orgC"], "F2": ["orgD"], "K1": ["orgC"]}
    initial_category_teams = {"G": ["grpG"], "H": ["grpH"], "K": ["grpK"], "Institute": ["svcS", "svcT"]}
    initial_admins = ["adm1", "adm2"]
    t_s = 420 + rng.randrange(0, 120)
    t_c = 60 + rng.randrange(0, 90)
    # starts after every "removal before t_s" of the patterns (re-adding a listed user writes no row)
    standin_start = t_s - rng.randrange(21, 36)
    # Pattern timings (UNAUTH / AUTH variants of the decisive change).
    d = {
        "removal_after": t_s + rng.randrange(90, 300),
        "removal_before": t_s - rng.randrange(40, 120),
        "grant_before": t_s - rng.randrange(40, 150),
        "grant_after": t_s + rng.randrange(90, 300),
        "early_removal": t_c + rng.randrange(30, 90),
    }
    background = _background(rng, focal_actor, t_s)
    return {
        "base_id": f"{pattern}_{index:02d}", "pattern": pattern, "split": split,
        "users": users, "focal_actor": focal_actor, "categories": cats, "events": events,
        "initial_event_teams": initial_event_teams, "initial_category_teams": initial_category_teams,
        "initial_admins": initial_admins, "t_s": t_s, "t_c": t_c, "standin_start": standin_start, "timing": d,
        "background": background, "rng_state": rng.randrange(10**9),
    }


def _background(rng, X, t_s):
    """Background activity from D-4 to the export: authorized and unauthorized edits on other events,
    ACL changes of other users, administrator changes, organisers editing their events."""
    other_staff = "svcT" if X == "svcS" else "svcS"
    s = []
    t = BACKGROUND_START + rng.randrange(30, 200)
    while t < EXPORT_MINUTE - 60:
        kind = rng.choice(("org_edit", "org_edit", "org_edit", "grp_edit", "staff_edit", "acl_cycle",
                           "cat_acl_cycle", "member_add"))
        if kind == "org_edit":
            ev, who = rng.choice((("E", "orgA"), ("E2", "orgB"), ("F1", "orgC"), ("F2", "orgD"), ("K1", "orgC")))
            s.append({"t": t, "kind": "edit", "actor": who, "event": ev, "field": rng.choice(("title", "description"))})
        elif kind == "grp_edit":
            ev, who = rng.choice((("E2", "grpG"), ("F1", "grpH"), ("F2", "grpH"), ("K1", "grpK")))
            s.append({"t": t, "kind": "edit", "actor": who, "event": ev, "field": "description"})
        elif kind == "staff_edit":
            # a service-desk member changes an event of H or K (accepted by the application; outside the focal question)
            ev = rng.choice(("F1", "F2", "K1"))
            s.append({"t": t, "kind": "edit", "actor": rng.choice((other_staff, "adm1")), "event": ev, "field": "description"})
        elif kind == "acl_cycle":
            ev, who = rng.choice((("F1", "orgC"), ("F2", "orgD"), ("E2", "orgB")))
            s.append({"t": t, "kind": "event_acl_add", "actor": who, "event": ev, "user": "memM"})
            s.append({"t": t + rng.randrange(30, 240), "kind": "event_acl_remove", "actor": who, "event": ev, "user": "memM"})
        elif kind == "cat_acl_cycle":
            cat, who = rng.choice((("H", "grpH"), ("K", "grpK")))
            s.append({"t": t, "kind": "category_acl_add", "actor": who, "category": cat, "user": other_staff})
            s.append({"t": t + rng.randrange(30, 240), "kind": "category_acl_remove", "actor": who, "category": cat,
                      "user": other_staff})
        elif kind == "member_add":
            s.append({"t": t, "kind": "edit", "actor": "orgB", "event": "E2", "field": "title"})
        t += rng.randrange(180, 720)
    # administrator status churn for a non-focal user (app log)
    ta = rng.randrange(BACKGROUND_START + 300, EXPORT_MINUTE - 300)
    s.append({"t": ta, "kind": "admin_grant", "actor": "adm1", "user": "memM"})
    s.append({"t": ta + rng.randrange(60, 600), "kind": "admin_revoke", "actor": "adm1", "user": "memM"})
    # keep background away from the focal minutes (no two steps in the same minute as a focal step)
    return [x for x in s if abs(x["t"] - t_s) > 12 and x["t"] < EXPORT_MINUTE - 5]


def focal_steps(base, world_kind):
    pattern, t_s, t_c, d, X = base["pattern"], base["t_s"], base["t_c"], base["timing"], base["focal_actor"]
    auth = world_kind == "AUTH"
    st = []

    def eacl(t, kind, focal):
        st.append({"t": t, "kind": f"event_acl_{kind}", "actor": "orgA", "event": "E", "user": X, "focal": focal})

    def cacl(t, kind, cat, focal):
        who = {"G": "grpG", "H": "grpH", "K": "grpK"}[cat]
        st.append({"t": t, "kind": f"category_acl_{kind}", "actor": who, "category": cat, "user": X, "focal": focal})

    if pattern == "event_removal_order":
        eacl(t_c, "add", "grant")
        eacl(d["removal_after"] if auth else d["removal_before"], "remove", "removal")
    elif pattern == "event_grant_order":
        eacl(d["grant_before"] if auth else d["grant_after"], "add", "grant")
    elif pattern == "category_scope":
        cacl(t_c, "add", "G" if auth else "H", "category_grant")
    elif pattern == "category_removal_order":
        cacl(t_c, "add", "G", "category_grant")
        cacl(d["removal_after"] if auth else d["removal_before"], "remove", "G", "category_removal")
    elif pattern == "team_handover":
        eacl(t_c, "add", "grant")
        eacl(d["early_removal"], "remove", "removal")
        cacl(d["grant_before"] if auth else d["grant_after"], "add", "G", "category_grant")
    elif pattern == "readd":
        eacl(t_c, "add", "grant")
        eacl(d["early_removal"], "remove", "removal")
        eacl(d["grant_before"] if auth else d["grant_after"], "add", "regrant")
    else:
        raise ValueError(pattern)
    # stand-in membership on K (all worlds; moved to G by the twins of the category patterns)
    cacl(base["standin_start"], "add", "K", "standin_grant")
    cacl(t_s + STANDIN_END, "remove", "K", "standin_removal")
    st.append({"t": t_s, "kind": "edit", "actor": X, "event": "E", "field": "title", "focal": "seed"})
    return st


def world_spec(base, world_kind, twin=False):
    """Build a world. Twins change only category-log records that V1b hides (everything before
    t_s + 60) so that the latent truth flips while the V1b view is identical:
      - UNAUTH twin (every pattern): X's stand-in membership is on G instead of K (added before t_s,
        removed at t_s + 30, by G's team instead of K's), so X is on G's team at t_s; the team snapshot
        at export is unchanged.
      - AUTH twin (category patterns only, whose AUTH worlds are unanswerable under V1b): X's focal
        addition to G's team is moved to t_s + 15 (after the audited change), so X is not on G's team at
        t_s; the snapshot at export is unchanged. The AUTH worlds of the event patterns are answerable
        in every variant and have no twin."""
    steps = focal_steps(base, world_kind)
    if twin:
        if world_kind == "AUTH" and base["pattern"] not in CATEGORY_PATTERNS:
            raise ValueError("AUTH twins exist only for the category patterns")
        if world_kind == "AUTH":
            g = [s for s in steps if s.get("focal") == "category_grant"]
            assert len(g) == 1 and g[0]["category"] == "G"
            g[0]["t"] = base["t_s"] + 15
        else:
            for s in steps:
                if s.get("focal") in ("standin_grant", "standin_removal"):
                    s["category"] = "G"
                    s["actor"] = "grpG"
    timeline = sorted(base["background"] + steps, key=lambda s: (s["t"], 0 if s.get("focal") else 1))
    wid = f"{base['base_id']}__{world_kind}{'__twin' if twin else ''}"
    return {"world_id": wid, "base": base, "world_kind": world_kind, "twin": twin,
            "timeline": timeline, "export_minute": EXPORT_MINUTE}
