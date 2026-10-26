"""Generate private world specifications (timelines) for the token-issuer policy task.

A base event fixes the organisation, background activity and focal timing seed.
Each base event yields two worlds that differ only in the decisive authorization
fact (AUTH / UNAUTH), plus issuer-swap twins used solely for the answerability check.
Specs are private: they are never shown to investigating agents.
"""
import random

PATTERNS = ("removal_order", "readd", "other_team", "token_confusion")
TOKEN_STEMS = ("sync", "export", "pos", "report", "bridge", "feed")


def _handles(rng, n):
    seen = set()
    out = []
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
    rng = random.Random(f"{pattern}:{index}")
    handles = _handles(rng, 9)
    users = {
        "adm1": handles[0], "adm2": handles[1],
        "engE": handles[2], "engF": handles[3], "engG": handles[4], "engH": handles[5],
        "box1": handles[6], "box2": handles[7], "box3": handles[8],
    }
    initial = {
        "admins": ["adm1", "adm2"],
        "integrations": ["engE", "engF", "engG", "engH"],
        "boxoffice": ["box1", "box2"],
    }
    if pattern == "other_team":
        initial["boxoffice"].append("engE")
    used_names = set()
    t_c = 60 + rng.randrange(0, 60)
    cutoff_b = t_c + 30
    t_s = 420 + rng.randrange(0, 120)
    seed_op = rng.choice(("mark_paid", "extend"))
    focal_tokens = {"K_E": _token_name(rng, used_names)}
    if pattern == "token_confusion":
        focal_tokens["K_F"] = _token_name(rng, used_names)

    background = []
    # Non-focal tokens created by other engineers, used later.
    for who in ("engG", "engH"):
        tname = _token_name(rng, used_names)
        tc = rng.randrange(5, 200)
        background.append({"t": tc, "kind": "token_create", "actor": who, "team": "integrations", "token": tname})
        for _ in range(rng.randrange(2, 5)):
            background.append({"t": tc + rng.randrange(10, 600), "kind": rng.choice(("mark_paid", "extend")),
                               "token": tname, "event": rng.choice(("ev1", "ev2"))})
    # Box-office staff operate through their own sessions.
    for _ in range(rng.randrange(5, 9)):
        background.append({"t": rng.randrange(0, 700), "kind": "session_mark_paid",
                           "actor": rng.choice(("box1", "box2")), "event": "ev1"})
    # Membership churn for non-focal people.
    tr = rng.randrange(100, 500)
    background.append({"t": tr, "kind": "member_remove", "actor": "adm2", "team": "boxoffice", "user": "box2"})
    background.append({"t": tr + rng.randrange(20, 200), "kind": "member_add", "actor": "adm1",
                       "team": "boxoffice", "user": "box2"})
    background.append({"t": rng.randrange(50, 650), "kind": "member_add", "actor": "adm1",
                       "team": "boxoffice", "user": "box3"})
    background.append({"t": rng.randrange(300, 700), "kind": "member_remove", "actor": "adm1",
                       "team": "integrations", "user": "engH"})

    return {
        "base_id": f"{pattern}_{index:02d}", "pattern": pattern, "split": split,
        "users": users, "initial": initial, "t_c": t_c, "cutoff_b": cutoff_b, "t_s": t_s,
        "seed_op": seed_op, "seed_event": rng.choice(("ev1", "ev2")),
        "focal_tokens": focal_tokens, "background": background,
        "rng_state": rng.randrange(10**9),
    }


def focal_steps(base, world_kind, issuer="engE"):
    """Return focal timeline steps. world_kind in {AUTH, UNAUTH}."""
    rng = random.Random(base["rng_state"])
    t_c, t_s, cut = base["t_c"], base["t_s"], base["cutoff_b"]
    steps = []
    pattern = base["pattern"]
    k_e = base["focal_tokens"]["K_E"]
    steps.append({"t": t_c, "kind": "token_create", "actor": issuer, "team": "integrations", "token": k_e,
                  "focal": "creation"})
    seed_token = k_e
    lo = cut + 30
    if pattern == "removal_order":
        if world_kind == "AUTH":
            steps.append({"t": t_s + rng.randrange(30, 300), "kind": "member_remove", "actor": "adm1",
                          "team": "integrations", "user": issuer, "focal": "membership"})
        else:
            steps.append({"t": rng.randrange(lo, t_s - 30), "kind": "member_remove", "actor": "adm1",
                          "team": "integrations", "user": issuer, "focal": "membership"})
    elif pattern == "readd":
        t_r = rng.randrange(lo, t_s - 150)
        steps.append({"t": t_r, "kind": "member_remove", "actor": "adm2", "team": "integrations",
                      "user": issuer, "focal": "membership"})
        t_a = t_s - rng.randrange(20, 120) if world_kind == "AUTH" else t_s + rng.randrange(20, 120)
        steps.append({"t": t_a, "kind": "member_add", "actor": "adm1", "team": "integrations",
                      "user": issuer, "focal": "membership"})
    elif pattern == "other_team":
        team = "boxoffice" if world_kind == "AUTH" else "integrations"
        steps.append({"t": rng.randrange(lo, t_s - 30), "kind": "member_remove", "actor": "adm1",
                      "team": team, "user": issuer, "focal": "membership"})
    elif pattern == "token_confusion":
        k_f = base["focal_tokens"]["K_F"]
        steps.append({"t": t_c + rng.randrange(1, 20), "kind": "token_create", "actor": "engF",
                      "team": "integrations", "token": k_f, "focal": "creation_other"})
        steps.append({"t": rng.randrange(lo, t_s - 30), "kind": "member_remove", "actor": "adm2",
                      "team": "integrations", "user": "engF", "focal": "membership_other"})
        for tok in (k_e, k_f):
            steps.append({"t": t_c + rng.randrange(25, 80), "kind": rng.choice(("mark_paid", "extend")),
                          "token": tok, "event": rng.choice(("ev1", "ev2"))})
        seed_token = k_e if world_kind == "AUTH" else k_f
    steps.append({"t": t_s, "kind": base["seed_op"], "token": seed_token, "event": base["seed_event"],
                  "focal": "seed"})
    return steps


def world_spec(base, world_kind, twin=False):
    """Build a world. An issuer-swap twin keeps the timeline but changes who created the
    seed token so that the twin's latent truth is the opposite of the original world's:
    AUTH originals get an issuer outside the token's team (adm2); UNAUTH originals get an
    issuer who stays in the team (engG)."""
    steps = focal_steps(base, world_kind, issuer="engE")
    seed_token = next(s["token"] for s in steps if s.get("focal") == "seed")
    if twin:
        swap = "adm2" if world_kind == "AUTH" else "engG"
        for s in steps:
            if s["kind"] == "token_create" and s["token"] == seed_token:
                s["actor"] = swap
    timeline = sorted(base["background"] + steps, key=lambda s: (s["t"], s.get("focal") != "seed"))
    wid = f"{base['base_id']}__{world_kind}{'__twin' if twin else ''}"
    return {"world_id": wid, "base": base, "world_kind": world_kind, "twin": twin,
            "seed_token": seed_token, "timeline": timeline}
