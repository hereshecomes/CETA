"""Generate private world specifications (timelines) for the Weblate project-token policy task.

Mirrors harness/worlds.py (pretix): a base event fixes the organisation, background activity and
focal timing seed; each base event yields an AUTH and an UNAUTH world differing only in the decisive
fact, plus creator-swap twins used solely for the answerability check. Specs are private.

Scenario clock: T0 = 08:00 UTC of a day D. The export is taken at D+1 00:50 (minute 1010). The
deployment's celery beat runs Weblate's disable_expired task hourly at :55 (minutes 55, 115, ...,
955); the first run after the export (1015) is outside the timeline. Project tokens created with an
expiry date expire at the end of that day (application behaviour: 23:59:59.999999 of the date).
"""
import random

PATTERNS = ("removal_order", "readd", "other_team", "token_confusion", "expiry_order")
TOKEN_STEMS = ("sync", "export", "deploy", "ci", "bridge", "feed")
EXPORT_MINUTE = 1010            # D+1 00:50
CLEANUP_MINUTES = list(range(55, EXPORT_MINUTE, 60))   # 08:55 ... 23:55 on day D
END_OF_DAY_MINUTE = 16 * 60     # D 24:00 == D+1 00:00 (expiry of a token with date_expires = D)
TEAM_ADMIN, TEAM_REVIEW, TEAM_TRANSLATE = "Administration", "Review", "Translate"
UNIT_POOL = 12  # translatable units available in the component (non-plural units x languages)


def _handles(rng, n):
    seen, out = set(), []
    while len(out) < n:
        h = "u" + "".join(rng.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(5))
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def _token_name(rng, used):
    while True:
        name = f"{rng.choice(TOKEN_STEMS)}-{rng.randrange(16**3):03x}"
        if name not in used:
            used.add(name)
            return name


def base_event(pattern, index, split):
    rng = random.Random(f"weblate:{pattern}:{index}")
    handles = _handles(rng, 10)
    users = {
        "adm1": handles[0], "adm2": handles[1],                       # site administrators (superusers)
        "engE": handles[2], "engF": handles[3], "engG": handles[4], "engH": handles[5],  # project admins
        "rev1": handles[6], "rev2": handles[7],                       # reviewers
        "tr1": handles[8], "tr2": handles[9],                         # translators
    }
    # adm1 is also a member of the Administration team; adm2 is a superuser outside every team.
    initial = {
        TEAM_ADMIN: ["adm1", "engE", "engF", "engG", "engH"],
        TEAM_REVIEW: ["rev1", "rev2"],
        TEAM_TRANSLATE: ["tr1"],
    }
    if pattern == "other_team":
        initial[TEAM_REVIEW].append("engE")
    used_names = set()
    t_c = 60 + rng.randrange(0, 60)
    cutoff_b = t_c + 30
    if pattern == "expiry_order":
        t_s = 962 + rng.randrange(0, 40)       # after the end of day D, before the export
    else:
        t_s = 420 + rng.randrange(0, 120)
    focal_tokens = {"K_E": _token_name(rng, used_names)}
    if pattern == "token_confusion":
        focal_tokens["K_F"] = _token_name(rng, used_names)

    background = []
    # Tokens created before the audit window by other engineers, expiring the day before D:
    # the first hourly cleanup disables them (realistic 'disabled-expiry' noise).
    for who in ("engG", "engH"):
        tname = _token_name(rng, used_names)
        background.append({"t": -30 * 24 * 60 + rng.randrange(60, 600), "kind": "token_create", "actor": who,
                           "token": tname, "expires_days": -1})
    # Non-focal tokens created inside the window by other engineers, used later.
    for who in ("engG", "engH"):
        tname = _token_name(rng, used_names)
        tc = rng.randrange(5, 200)
        background.append({"t": tc, "kind": "token_create", "actor": who, "token": tname, "expires_days": 365})
        for _ in range(rng.randrange(2, 5)):
            background.append({"t": tc + rng.randrange(10, 700), "kind": "translate", "token": tname,
                               "unit": rng.randrange(UNIT_POOL)})
    # Reviewers and translators work with their own accounts.
    for _ in range(rng.randrange(6, 10)):
        background.append({"t": rng.randrange(0, 900), "kind": "user_translate",
                           "actor": rng.choice(("rev1", "rev2", "tr1")), "unit": rng.randrange(UNIT_POOL)})
    # Membership churn for non-focal people.
    tr = rng.randrange(100, 500)
    background.append({"t": tr, "kind": "team_remove", "actor": "adm2", "team": TEAM_REVIEW, "user": "rev2"})
    background.append({"t": tr + rng.randrange(20, 200), "kind": "team_add", "actor": "adm1",
                       "team": TEAM_REVIEW, "user": "rev2"})
    background.append({"t": rng.randrange(50, 650), "kind": "team_add", "actor": "adm1",
                       "team": TEAM_TRANSLATE, "user": "tr2"})
    background.append({"t": rng.randrange(300, 700), "kind": "project_remove", "actor": "adm1", "user": "engH"})

    return {
        "base_id": f"{pattern}_{index:02d}", "pattern": pattern, "split": split,
        "users": users, "initial": initial, "t_c": t_c, "cutoff_b": cutoff_b, "t_s": t_s,
        "seed_unit": rng.randrange(UNIT_POOL),
        "focal_tokens": focal_tokens, "background": background,
        "rng_state": rng.randrange(10**9),
    }


def focal_steps(base, world_kind, creator="engE"):
    """Focal timeline steps. world_kind in {AUTH, UNAUTH}."""
    rng = random.Random(base["rng_state"])
    t_c, t_s, cut = base["t_c"], base["t_s"], base["cutoff_b"]
    pattern = base["pattern"]
    k_e = base["focal_tokens"]["K_E"]
    steps = []
    expires_days = 365
    if pattern == "expiry_order":
        expires_days = 0 if world_kind == "UNAUTH" else 1   # date D (expired at t_s) or D+1 (valid at t_s)
    steps.append({"t": t_c, "kind": "token_create", "actor": creator, "token": k_e,
                  "expires_days": expires_days, "focal": "creation"})
    seed_token = k_e
    lo = cut + 30
    if pattern == "removal_order":
        t_r = t_s + rng.randrange(30, 300) if world_kind == "AUTH" else rng.randrange(lo, t_s - 30)
        steps.append({"t": t_r, "kind": "team_remove", "actor": "adm1", "team": TEAM_ADMIN, "user": creator,
                      "focal": "membership"})
    elif pattern == "readd":
        t_r = rng.randrange(lo, t_s - 150)
        steps.append({"t": t_r, "kind": "team_remove", "actor": "adm2", "team": TEAM_ADMIN, "user": creator,
                      "focal": "membership"})
        t_a = t_s - rng.randrange(20, 120) if world_kind == "AUTH" else t_s + rng.randrange(20, 120)
        steps.append({"t": t_a, "kind": "team_add", "actor": "adm1", "team": TEAM_ADMIN, "user": creator,
                      "focal": "membership"})
    elif pattern == "other_team":
        team = TEAM_REVIEW if world_kind == "AUTH" else TEAM_ADMIN
        steps.append({"t": rng.randrange(lo, t_s - 30), "kind": "team_remove", "actor": "adm1", "team": team,
                      "user": creator, "focal": "membership"})
    elif pattern == "token_confusion":
        k_f = base["focal_tokens"]["K_F"]
        steps.append({"t": t_c + rng.randrange(1, 20), "kind": "token_create", "actor": "engF", "token": k_f,
                      "expires_days": 365, "focal": "creation_other"})
        steps.append({"t": rng.randrange(lo, t_s - 30), "kind": "team_remove", "actor": "adm2", "team": TEAM_ADMIN,
                      "user": "engF", "focal": "membership_other"})
        for tok in (k_e, k_f):
            steps.append({"t": t_c + rng.randrange(25, 80), "kind": "translate", "token": tok,
                          "unit": rng.randrange(UNIT_POOL)})
        seed_token = k_e if world_kind == "AUTH" else k_f
    elif pattern == "expiry_order":
        # Membership stays intact; the decisive fact is the token's expiry date. A couple of
        # operations by the focal token while it is valid (day D) give it a normal history.
        for _ in range(2):
            steps.append({"t": t_c + rng.randrange(30, 600), "kind": "translate", "token": k_e,
                          "unit": rng.randrange(UNIT_POOL)})
    steps.append({"t": t_s, "kind": "translate", "token": seed_token, "unit": base["seed_unit"], "focal": "seed"})
    return steps


def world_spec(base, world_kind, twin=False):
    """Build a world. A creator-swap twin keeps the timeline but changes who created the seed token
    so that the twin's latent truth is the opposite of the original's where membership decides:
    AUTH originals get a creator outside the token's team (adm2, a site administrator who is not a
    member of the Administration team); UNAUTH originals get a creator who stays in the team (engG).
    For expiry_order UNAUTH originals the truth is decided by the expiry, so the twin keeps it."""
    steps = focal_steps(base, world_kind, creator="engE")
    seed_token = next(s["token"] for s in steps if s.get("focal") == "seed")
    if twin:
        swap = "adm2" if world_kind == "AUTH" else "engG"
        for s in steps:
            if s["kind"] == "token_create" and s["token"] == seed_token:
                s["actor"] = swap
    timeline = sorted(base["background"] + steps, key=lambda s: (s["t"], s.get("focal") != "seed"))
    wid = f"{base['base_id']}__{world_kind}{'__twin' if twin else ''}"
    return {"world_id": wid, "base": base, "world_kind": world_kind, "twin": twin,
            "seed_token": seed_token, "timeline": timeline,
            "cleanup_minutes": CLEANUP_MINUTES, "export_minute": EXPORT_MINUTE}
