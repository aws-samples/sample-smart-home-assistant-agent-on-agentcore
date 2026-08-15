"""Materialising A2A grant intent into Cognito group membership.

Two stores, one direction:

  DDB `__a2a_permissions__`   intent, authored per `__global__` and per user, with
                              per-user grants REPLACING global per sub-agent
         │  materialise on save
         ▼
  Cognito groups per user     the runtime truth: a signed `cognito:groups` claim
                              that each sub-agent Runtime's authorizer checks

Why two stores rather than one. Groups alone cannot express the replace semantics —
group membership is a flat union, so "alice gets fewer skills than global" has no
representation. DDB alone cannot be an authorization boundary, because the check has
to happen at the sub-agent's authorizer, before our code runs, on something the
caller cannot forge. So intent lives where it can be expressed and enforcement lives
where it can be enforced, and this module is the one-way bridge.

GLOBAL grants do not cross that bridge
--------------------------------------
Only PER-USER grants are materialised as memberships. A global grant is the same
value for everybody, and writing it a million times is not a scaling detail but a
wall: `AdminAddUserToGroup` sits in Cognito's `UserUpdate` category at 25 RPS,
account-wide and NOT adjustable, each write bills the user as a monthly active user,
and a user may hold at most 100 groups. So global grants are computed at token issue
by `cdk/lambda/pre-token` and written straight into the `cognito:groups` claim.

The consequence to hold on to: **a group name in the claim with no membership behind
it is normal and correct.** `AdminListGroupsForUser` answers "what was materialised
for this user", not "what may this user reach" — which is why `diff_user` and the
reconcile UI compare against per-user intent only, and why the console labels the
rest as claim-injected rather than reporting it as drift.

This module is imported by BOTH Lambdas so the merge rule has one implementation.
Two would not fail loudly; one class of user would simply end up with more access
than an admin granted.

A one-way bridge drifts, so `reconcile` exists and the UI surfaces its result. The
precedent is `sync-schedules` for scenes: a sync without a reconcile is not a sync,
it is a hope.

Only groups carrying the `a2a-` prefix are ever touched. `admin` and anything else
in the pool is left alone, so a membership this module does not own cannot be
removed by it.
"""

from __future__ import annotations

import logging

import a2a_groups

logger = logging.getLogger(__name__)

A2A_PERMS_SK = "__a2a_permissions__"
GLOBAL_SCOPE = "__global__"


# ---------------------------------------------------------------------------
# Intent
# ---------------------------------------------------------------------------

def read_intent(table, user_id: str) -> dict[str, list[str]]:
    """The raw grants stored for one scope. `{}` when the row is absent."""
    item = table.get_item(
        Key={"userId": user_id, "skillName": A2A_PERMS_SK}).get("Item") or {}
    grants = item.get("a2aGrants") or {}
    out: dict[str, list[str]] = {}
    for record_id, skills in grants.items():
        if isinstance(skills, (set, list, tuple)):
            out[str(record_id)] = sorted(str(s) for s in skills)
    return out


def effective_grants(global_grants: dict[str, list[str]],
                     user_grants: dict[str, list[str]]) -> dict[str, list[str]]:
    """Global merged with per-user, per-user REPLACING global per sub-agent.

    Mirrors the merge the agent applies to the same intent (`merged.update(...)`), which is the
    behaviour every existing deployment already has. Changing it to a union would
    silently widen effective access for every user who currently holds a narrowing
    override, which is why it is pinned by a test rather than left as a detail.

    An empty skill list is a real entry meaning "no skills on this sub-agent", not
    an absent one — that is how an admin narrows a user to nothing while global
    still grants something.
    """
    merged = dict(global_grants)
    merged.update(user_grants)
    return merged


# ---------------------------------------------------------------------------
# Materialisation
# ---------------------------------------------------------------------------

def wanted_groups(effective: dict[str, list[str]],
                  record_names: dict[str, str]) -> set[str]:
    """The group names one user should hold.

    `record_names` maps Registry recordId to AgentCard name, because grants are
    stored by recordId (stable across renames) while group names are keyed on the
    card name (what the sub-agent knows itself as). A recordId with no known name is
    skipped and logged: emitting a group for a guessed name would create a grant no
    authorizer matches.

    Two shapes per granted agent, and both are needed
    -------------------------------------------------
    One `a2a-<agent>` (what the Runtime authorizer matches) plus one
    `a2a-<agent>.<skill>` per granted skill (what the container reads to decide
    which skills the caller holds). See `shared/a2a_groups` for why the door stopped
    enumerating skills.

    The agent group is emitted only when at least one skill group was, and that
    condition is load-bearing in both directions:

      - An admin who narrows a user to zero skills on an agent stores an EMPTY list,
        which is a real entry meaning "nothing here" (see `effective_grants`). Adding
        the agent group anyway would let that user through the door for the container
        to refuse — a 200-shaped delegation failure and an apology from the model,
        where a clean 401 at the door is correct.
      - A card whose skills cannot be encoded produces no skill groups at all. Giving
        it a door key would be worse than the current silent skip, not better.
    """
    out: set[str] = set()
    for record_id, skills in effective.items():
        agent_name = record_names.get(record_id, "")
        if not agent_name:
            logger.warning(
                "grant references recordId %s with no known AgentCard name; "
                "skipped rather than guessing a group name", record_id)
            continue
        emitted = 0
        for skill in skills:
            try:
                out.add(a2a_groups.group_name(agent_name, skill))
                emitted += 1
            except a2a_groups.GroupNameError as exc:
                logger.warning("cannot encode grant %s/%s: %s",
                               agent_name, skill, exc)
        if not emitted:
            continue
        try:
            out.add(a2a_groups.agent_group_name(agent_name))
        except a2a_groups.GroupNameError as exc:
            # Unreachable while `group_name` above succeeded — it validates the same
            # agent name against the same pattern — but left explicit rather than
            # assumed: silently omitting the door key is the one failure here that
            # looks exactly like "the admin granted nothing".
            logger.warning("cannot encode the agent-level group for %s: %s",
                           agent_name, exc)
    return out


def current_groups(cognito, pool_id: str, username: str) -> set[str]:
    """The `a2a-` groups a user currently holds, paginated."""
    out: set[str] = set()
    token = None
    while True:
        kwargs = {"UserPoolId": pool_id, "Username": username, "Limit": 60}
        if token:
            kwargs["NextToken"] = token
        resp = cognito.admin_list_groups_for_user(**kwargs)
        for group in resp.get("Groups", []):
            name = group.get("GroupName", "")
            if name.startswith(a2a_groups.GROUP_PREFIX):
                out.add(name)
        token = resp.get("NextToken")
        if not token:
            return out


def _with_retry(fn, *, attempts: int = 4, **kwargs):
    """Call a Cognito admin API, backing off on throttling.

    These are rate-limited admin APIs and this runs concurrently across users, so a
    throttle is expected rather than exceptional. Without a retry the symptom is a
    user silently left without the grant an admin just gave them — the save reports
    an error, but per-user, buried in a list.
    """
    import time as _time

    for attempt in range(attempts):
        try:
            return fn(**kwargs)
        except Exception as exc:  # noqa: BLE001
            if "TooManyRequests" not in type(exc).__name__ and \
                    "TooManyRequests" not in str(exc):
                raise
            if attempt == attempts - 1:
                raise
            _time.sleep(0.4 * (2 ** attempt))


def ensure_group(cognito, pool_id: str, name: str) -> None:
    """Create the group if it does not exist. Idempotent."""
    try:
        cognito.create_group(
            GroupName=name, UserPoolId=pool_id,
            Description="A2A sub-agent grant, managed by the SubAgent Policy page")
    except cognito.exceptions.GroupExistsException:
        pass


def ensure_group_call(cognito=None, pool_id: str = "", name: str = "") -> None:
    """`ensure_group` in keyword form, so `_with_retry` can drive it."""
    ensure_group(cognito, pool_id, name)


def ensure_groups(cognito, pool_id: str, names: set[str]) -> list[str]:
    """Create every group in `names` once, up front. Returns the failures.

    Called ONCE per materialisation rather than per user, and that is the whole
    point. `CreateGroup` used to live inside the per-user loop, which for a global
    grant meant 39 users times 17 groups of racing calls; Cognito answered most of
    them with `TooManyRequestsException` and 33 of 39 users got no membership at all.
    A group is shared, so creating it per user was never anything but waste.

    Sequential, because this is a handful of calls against a rate-limited admin API
    and the parallel part is the per-user membership that follows.
    """
    failures = []
    for name in sorted(names):
        try:
            _with_retry(ensure_group_call, cognito=cognito, pool_id=pool_id, name=name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not create group %s: %s", name, exc)
            failures.append(f"{name}: {exc}")
    return failures


def materialise_user(cognito, pool_id: str, username: str,
                     wanted: set[str]) -> dict:
    """Make one user's `a2a-` memberships equal `wanted`.

    Assumes the groups already exist — see `ensure_groups`, which the caller runs
    first. Returns what changed, including whether anything was REMOVED, because the
    caller needs that to decide about forcing a token refresh: a narrowed grant
    otherwise keeps working until the current token expires.
    """
    current = current_groups(cognito, pool_id, username)
    to_add = sorted(wanted - current)
    to_remove = sorted(current - wanted)

    for name in to_add:
        _with_retry(cognito.admin_add_user_to_group,
                    UserPoolId=pool_id, Username=username, GroupName=name)
    for name in to_remove:
        _with_retry(cognito.admin_remove_user_from_group,
                    UserPoolId=pool_id, Username=username, GroupName=name)

    return {
        "username": username,
        "added": to_add,
        "removed": to_remove,
        "narrowed": bool(to_remove),
    }


def force_token_refresh(cognito, pool_id: str, username: str) -> bool:
    """Invalidate a user's sessions so their next token drops the removed groups.

    Group membership is baked into a token at issue, so a revoked grant would keep
    working for up to the token lifetime. Signing the user out closes that window at
    the cost of interrupting their chatbot session — a deliberate trade the admin is
    warned about before saving, because a revocation that silently does nothing for
    an hour is the worse failure.
    """
    try:
        cognito.admin_user_global_sign_out(UserPoolId=pool_id, Username=username)
        return True
    except Exception as exc:  # noqa: BLE001
        # Worth reporting but not worth failing the save: the membership change did
        # land, so the grant is correct and only its timing is off.
        logger.warning("could not sign out %s after narrowing grants: %s",
                       username, exc)
        return False


# ---------------------------------------------------------------------------
# Revocation sweep: a record that is no longer grantable keeps no groups
#
# Scanned from the GROUP side, not the record side, and that is the whole design.
# The obvious implementation — "for each record that is no longer grantable, work
# out its group names and revoke them" — cannot handle the case that matters most:
# a DELETED record can no longer be read, so its card name and skill list are gone
# and there is nothing to derive names from. Listing the groups instead needs
# nothing from the record but its NAME, which the group already encodes.
#
# It also closes a gap nothing else covers. A sub-agent's authorizer matches group
# names written into it at deploy time from the agent's own card, not from the
# Registry — so a group hand-made in Cognito would admit its holder even with no
# record at all. A group whose agent is not in the grantable catalog is removed
# here whether or not anything ever granted it.
#
# Cost is bounded by the number of GRANTED users, not the size of the pool: one
# ListGroups (about 25 groups), then ListUsersInGroup only for the groups that
# have to go.
# ---------------------------------------------------------------------------

def all_a2a_groups(cognito, pool_id: str) -> list[str]:
    """Every `a2a-` group in the pool, paginated.

    Only this prefix. `admin` and anything else in the pool is not this module's
    to touch, which is the same rule materialisation follows.
    """
    out: list[str] = []
    token = None
    while True:
        kwargs = {"UserPoolId": pool_id, "Limit": 60}
        if token:
            kwargs["NextToken"] = token
        resp = _with_retry(cognito.list_groups, **kwargs)
        for group in resp.get("Groups", []):
            name = group.get("GroupName", "")
            if name.startswith(a2a_groups.GROUP_PREFIX):
                out.append(name)
        token = resp.get("NextToken")
        if not token:
            return out


def users_in_group(cognito, pool_id: str, group: str) -> list[str]:
    """Usernames holding `group`, paginated."""
    out: list[str] = []
    token = None
    while True:
        kwargs = {"UserPoolId": pool_id, "GroupName": group, "Limit": 60}
        if token:
            kwargs["NextToken"] = token
        resp = _with_retry(cognito.list_users_in_group, **kwargs)
        for user in resp.get("Users", []):
            username = user.get("Username", "")
            if username:
                out.append(username)
        token = resp.get("NextToken")
        if not token:
            return out


def groups_to_revoke(group_names, grantable_agents: set[str]) -> list[str]:
    """The `a2a-` groups whose agent may not be granted right now.

    Pure, so the decision is testable without Cognito. A group whose name does not
    parse is left ALONE rather than revoked: the prefix is ours, but an unparseable
    name means something upstream changed shape, and deleting memberships on a
    guess is worse than leaving a group nobody's tools reference.

    Resolved with `agent_of_group`, NOT `parse_group`, and the difference is the
    whole point of this function. `parse_group` decodes only the skill shape, so it
    answers None for the agent-level `a2a-<agent>` — the group the Runtime authorizer
    actually matches. A sweep built on it would strip every skill group off a
    deprecated agent's holders, leave the one group that opens the door, and report
    success: the record would be gone from the API and its users would still be
    getting in.
    """
    doomed = []
    for name in group_names:
        agent_name = a2a_groups.agent_of_group(name)
        if agent_name is None:
            logger.warning("group %s carries our prefix but does not parse; "
                           "left alone rather than revoked", name)
            continue
        if agent_name not in grantable_agents:
            doomed.append(name)
    return sorted(doomed)


def revoke_groups(cognito, pool_id: str, groups: list[str],
                  sign_out: bool = True) -> dict:
    """Remove every membership of `groups`, and sign those users out.

    The sign-out is what makes this a REVOCATION rather than an intention: group
    membership is baked into a token at issue, so without it a removed grant keeps
    working until the token expires. Done once per user rather than once per
    membership — `AdminUserGlobalSignOut` shares the same 25 RPS `UserUpdate`
    quota as the removals themselves.

    Never raises. This runs on a schedule; one unlucky group must not stop the
    sweep from finishing the rest.
    """
    removed: dict[str, list[str]] = {}
    errors: list[str] = []
    for group in groups:
        try:
            holders = users_in_group(cognito, pool_id, group)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not list holders of %s: %s", group, exc)
            errors.append(f"{group}: {exc}")
            continue
        for username in holders:
            try:
                _with_retry(cognito.admin_remove_user_from_group,
                            UserPoolId=pool_id, Username=username,
                            GroupName=group)
                removed.setdefault(username, []).append(group)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not remove %s from %s: %s",
                               username, group, exc)
                errors.append(f"{username}/{group}: {exc}")

    signed_out = []
    if sign_out:
        for username in removed:
            if force_token_refresh(cognito, pool_id, username):
                signed_out.append(username)

    return {
        "revokedGroups": sorted(groups),
        "affectedUsers": {u: sorted(g) for u, g in sorted(removed.items())},
        "signedOut": sorted(signed_out),
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------

def diff_user(cognito, pool_id: str, username: str, wanted: set[str]) -> dict:
    """Intent against reality for one user, without changing anything."""
    current = current_groups(cognito, pool_id, username)
    missing = sorted(wanted - current)
    extra = sorted(current - wanted)
    return {
        "username": username,
        "missing": missing,
        "extra": extra,
        "inSync": not missing and not extra,
    }
