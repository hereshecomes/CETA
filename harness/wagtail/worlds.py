"""Generate private world specifications (timelines) for the Wagtail content publication policy task.

Mirrors harness/weblate/worlds.py: a base event fixes the organisation, the background activity and
the focal timing; each base event yields an AUTH and an UNAUTH world differing only in the decisive
fact, plus submitter-swap twins used solely for the answerability check (G0.5). Specs are private.

Scenario clock: T0 = 08:00 UTC of a day D (D varies per base event, see run_world.py). The export is
taken at D 23:00 (minute 900). The policy window is 48 hours (WINDOW = 2880 minutes). Background
activity starts four days before T0 so that the full-coverage variant shows older history.

Patterns (tiers fixed before any world was executed):
  core (dev+test):  approval_order, self_approval, stale_submission, other_page
  holdout (test):   revision_mismatch, resubmission
The decisive fact of every pattern concerns the focal page P:
  approval_order     AUTH: P submitted by X, approved by M (the approval publishes P).
                     UNAUTH: P submitted by X, then M publishes P directly (review cancelled); later X
                     re-submits and M2 approves (an approval AFTER the audited publication).
  self_approval      AUTH: submitted by X (an author). UNAUTH: submitted by M, who then approves it.
  stale_submission   AUTH: X's submission is inside the 48 h window. UNAUTH: it is older than 48 h.
  other_page         AUTH: P submitted and approved. UNAUTH: page Q submitted and approved; P
                     published directly by M right after.
  revision_mismatch  AUTH: approved revision = published revision. UNAUTH: M2 saves a draft of P
                     during the review, so the approval is for the earlier revision while the later
                     one goes live.
  resubmission       AUTH: submitted, changes requested, re-submitted, approved inside the window.
                     UNAUTH: the original submission is older than 48 h; only the re-submission
                     (`resume`) is recent.
Visibility variant V1b (export_case.py) cuts the log at `cutoff_b`: just after the focal submission
for the patterns whose decisive fact is the hidden submitter (self_approval, stale_submission; their
submitter-swap twins have identical views) and just before the 48 h window for the others.
"""
import random

PATTERNS_CORE = ("approval_order", "self_approval", "stale_submission", "other_page")
PATTERNS_HOLDOUT = ("revision_mismatch", "resubmission")
PATTERNS = PATTERNS_CORE + PATTERNS_HOLDOUT
HIDING_PATTERNS = ("self_approval", "stale_submission")
WINDOW = 2880                      # policy window, minutes (48 h)
EXPORT_MINUTE = 900                # D 23:00
BACKGROUND_START = -4 * 24 * 60    # D-4 08:00
GROUP_AUTHORS, GROUP_MODERATORS = "Authors", "Moderators"
N_PAGES = 12
PAGE_TITLES = ("Spring newsletter", "Office relocation", "Product launch notes", "Holiday schedule",
               "Security advisory", "Quarterly results", "Hiring update", "Customer story",
               "Maintenance window", "Partner announcement", "Community event", "Policy refresh",
               "Supplier update", "Travel guidance", "Benefits overview", "Release notes")
FOCAL_AUTHOR, FOCAL_MOD, SECOND_MOD = "autA", "modM", "modN"


def _handles(rng, n):
    seen, out = set(), []
    while len(out) < n:
        h = "u" + "".join(rng.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(5))
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def base_event(pattern, index, split):
    rng = random.Random(f"wagtail:{pattern}:{index}")
    handles = _handles(rng, 9)
    users = {
        "adm1": handles[0], "adm2": handles[1],                               # site administrators
        "autA": handles[2], "autB": handles[3], "autC": handles[4], "autD": handles[5],  # authors
        "modM": handles[6], "modN": handles[7], "modO": handles[8],           # moderators
    }
    initial = {GROUP_AUTHORS: ["autA", "autB", "autC", "autD"], GROUP_MODERATORS: ["modM", "modN", "modO"]}
    titles = list(PAGE_TITLES)
    rng.shuffle(titles)
    pages = titles[:N_PAGES]          # index 0 = focal page P, 1 = page Q, 2.. = background pages
    t_s = 420 + rng.randrange(0, 120)
    if pattern == "stale_submission":
        t_c = {"AUTH": t_s - WINDOW + rng.randrange(30, 500), "UNAUTH": t_s - WINDOW - rng.randrange(60, 600)}
    elif pattern == "resubmission":
        t_c = {"AUTH": t_s - rng.randrange(600, 2000), "UNAUTH": t_s - WINDOW - rng.randrange(60, 600)}
    else:
        tc = 60 + rng.randrange(0, 60)
        t_c = {"AUTH": tc, "UNAUTH": tc}
    if pattern in HIDING_PATTERNS:
        cutoff_b = {k: v + 30 for k, v in t_c.items()}
    else:
        cutoff_b = {k: t_s - WINDOW - 30 for k in t_c}

    background = []
    # An old review cycle on the focal page (published once before, long before the window).
    t_old = BACKGROUND_START + rng.randrange(60, 300)
    background += _cycle(rng, 0, t_old, "normal", author="autB", moderator="modO")
    # Review cycles on the background pages (sequential per page, never overlapping).
    for pi in range(2, N_PAGES):
        n_cycles = rng.choice((1, 1, 2))
        start = BACKGROUND_START + rng.randrange(60, 900)
        for _ in range(n_cycles):
            if start > EXPORT_MINUTE - 480:
                break
            kind = rng.choice(("normal", "normal", "normal", "rejected", "direct", "cancelled", "draft_only"))
            background += _cycle(rng, pi, start, kind,
                                 author=rng.choice(("autB", "autC", "autD")),
                                 moderator=rng.choice(("modN", "modO")))
            start += 480 + rng.randrange(0, 1500)
    # Membership churn, recorded by the application only as generic user edits.
    tu = rng.randrange(BACKGROUND_START + 100, EXPORT_MINUTE - 100)
    background.append({"t": tu, "kind": "user_edit", "actor": "adm1", "user": "autD",
                       "groups": [GROUP_AUTHORS, GROUP_MODERATORS]})
    background.append({"t": tu + rng.randrange(60, 600), "kind": "user_edit", "actor": "adm2", "user": "autD",
                       "groups": [GROUP_AUTHORS]})
    background.append({"t": rng.randrange(BACKGROUND_START + 100, EXPORT_MINUTE - 100), "kind": "user_edit",
                       "actor": "adm1", "user": "autC", "groups": [GROUP_AUTHORS]})
    background = [s for s in background if s["t"] < EXPORT_MINUTE - 5]
    return {
        "base_id": f"{pattern}_{index:02d}", "pattern": pattern, "split": split,
        "users": users, "initial": initial, "pages": pages,
        "t_c": t_c, "t_s": t_s, "cutoff_b": cutoff_b,
        "background": background, "rng_state": rng.randrange(10**9),
    }


def _cycle(rng, page, start, kind, author, moderator):
    """One background review cycle on a page, all steps inside ~450 minutes of `start`."""
    s = []
    if kind == "normal":
        s.append({"t": start, "kind": "submit", "actor": author, "page": page})
        s.append({"t": start + rng.randrange(20, 400), "kind": "approve", "actor": moderator, "page": page})
    elif kind == "rejected":
        s.append({"t": start, "kind": "submit", "actor": author, "page": page})
        t1 = start + rng.randrange(20, 150)
        s.append({"t": t1, "kind": "reject", "actor": moderator, "page": page})
        t2 = t1 + rng.randrange(20, 150)
        s.append({"t": t2, "kind": "resubmit", "actor": author, "page": page})
        s.append({"t": t2 + rng.randrange(20, 120), "kind": "approve", "actor": moderator, "page": page})
    elif kind == "direct":
        s.append({"t": start, "kind": "direct_publish", "actor": moderator, "page": page})
    elif kind == "cancelled":
        s.append({"t": start, "kind": "submit", "actor": author, "page": page})
        s.append({"t": start + rng.randrange(20, 200), "kind": "cancel", "actor": author, "page": page})
    elif kind == "draft_only":
        s.append({"t": start, "kind": "save_draft", "actor": author, "page": page})
    return s


def focal_steps(base, world_kind, submitter=FOCAL_AUTHOR):
    """Focal timeline steps on the focal page (index 0) and, for other_page, page Q (index 1)."""
    rng = random.Random(base["rng_state"])
    pattern, t_s = base["pattern"], base["t_s"]
    t_c = base["t_c"][world_kind]
    M, M2 = FOCAL_MOD, SECOND_MOD
    steps = []
    if pattern == "approval_order":
        steps.append({"t": t_c, "kind": "submit", "actor": submitter, "page": 0, "focal": "submission"})
        if world_kind == "AUTH":
            steps.append({"t": t_s, "kind": "approve", "actor": M, "page": 0, "focal": "seed"})
        else:
            steps.append({"t": t_s, "kind": "direct_publish", "actor": M, "page": 0, "focal": "seed"})
            t2 = t_s + rng.randrange(40, 90)
            steps.append({"t": t2, "kind": "submit", "actor": submitter, "page": 0, "focal": "later_submission"})
            steps.append({"t": t2 + rng.randrange(60, 150), "kind": "approve", "actor": M2, "page": 0,
                          "focal": "later_approval"})
    elif pattern == "self_approval":
        who = submitter if world_kind == "AUTH" else M
        steps.append({"t": t_c, "kind": "submit", "actor": who, "page": 0, "focal": "submission"})
        steps.append({"t": t_s, "kind": "approve", "actor": M, "page": 0, "focal": "seed"})
    elif pattern == "stale_submission":
        steps.append({"t": t_c, "kind": "submit", "actor": submitter, "page": 0, "focal": "submission"})
        steps.append({"t": t_s, "kind": "approve", "actor": M, "page": 0, "focal": "seed"})
    elif pattern == "other_page":
        if world_kind == "AUTH":
            steps.append({"t": t_c, "kind": "submit", "actor": submitter, "page": 0, "focal": "submission"})
            steps.append({"t": t_s, "kind": "approve", "actor": M, "page": 0, "focal": "seed"})
        else:
            steps.append({"t": t_c, "kind": "submit", "actor": submitter, "page": 1, "focal": "distractor_submission"})
            steps.append({"t": t_s - rng.randrange(5, 30), "kind": "approve", "actor": M, "page": 1,
                          "focal": "distractor_approval"})
            steps.append({"t": t_s, "kind": "direct_publish", "actor": M, "page": 0, "focal": "seed"})
    elif pattern == "revision_mismatch":
        steps.append({"t": t_c, "kind": "submit", "actor": submitter, "page": 0, "focal": "submission"})
        if world_kind == "UNAUTH":
            steps.append({"t": rng.randrange(t_c + 15, t_s - 15), "kind": "save_draft", "actor": M2, "page": 0,
                          "focal": "draft_other"})
        steps.append({"t": t_s, "kind": "approve", "actor": M, "page": 0, "focal": "seed"})
    elif pattern == "resubmission":
        steps.append({"t": t_c, "kind": "submit", "actor": submitter, "page": 0, "focal": "submission"})
        t_r = t_c + rng.randrange(30, 200)
        steps.append({"t": t_r, "kind": "reject", "actor": M, "page": 0, "focal": "rejection"})
        if world_kind == "AUTH":
            t_m = t_r + rng.randrange(30, 300)
        else:
            t_m = t_s - rng.randrange(60, 400)
        steps.append({"t": t_m, "kind": "resubmit", "actor": submitter, "page": 0, "focal": "resubmission"})
        steps.append({"t": t_s, "kind": "approve", "actor": M, "page": 0, "focal": "seed"})
    else:
        raise ValueError(pattern)
    return steps


def world_spec(base, world_kind, twin=False):
    """Build a world. A submitter-swap twin keeps the timeline but changes who made the focal
    submission(s): an author (autA) becomes the approving moderator (modM) and vice versa, so that
    where the submitter decides the truth (self_approval, stale_submission AUTH) the twin's latent
    truth is the opposite of the original's while every visible record after the submission is
    identical. For the other patterns the twin does not flip the truth and is not needed."""
    steps = focal_steps(base, world_kind)
    if twin:
        for s in steps:
            if s.get("focal") in ("submission", "resubmission", "later_submission", "distractor_submission"):
                s["actor"] = FOCAL_MOD if s["actor"] == FOCAL_AUTHOR else FOCAL_AUTHOR
    timeline = sorted(base["background"] + steps, key=lambda s: s["t"])
    wid = f"{base['base_id']}__{world_kind}{'__twin' if twin else ''}"
    return {"world_id": wid, "base": base, "world_kind": world_kind, "twin": twin,
            "timeline": timeline, "export_minute": EXPORT_MINUTE, "window_minutes": WINDOW}
