"""Canary extraction and leak classification."""
import json
import re

TEAM_CANARY = re.compile(r"CANARY-(engineering|sales|hr|finance|legal|ops|shared)-\d{4}")
PERSONAL_CANARY = re.compile(r"CANARY-personal-([a-z]+-[a-z0-9]+)-\d{2}")


def canaries(body) -> tuple[set[str], set[str]]:
    """Returns (team/shared canary owners, personal canary owners) found anywhere in a response."""
    text = json.dumps(body, ensure_ascii=False)
    return ({m.group(0) for m in TEAM_CANARY.finditer(text)},
            {m.group(0) for m in PERSONAL_CANARY.finditer(text)})


def owner(canary: str) -> str:
    match = PERSONAL_CANARY.fullmatch(canary)
    if match:
        return "personal:" + match.group(1)
    return TEAM_CANARY.fullmatch(canary).group(1)


def allowed_owners(user_id: str, teams: set[str]) -> set[str]:
    return {*teams, "shared", "personal:" + user_id}


def result_scopes(body) -> set[str]:
    out = set()
    if isinstance(body, dict):
        for item in body.get("results", []) or []:
            scope = item.get("scope")
            if isinstance(scope, dict):
                out.add(scope.get("team_id") or scope.get("kind"))
        scope = body.get("scope")
        if isinstance(scope, dict):
            out.add(scope.get("team_id") or scope.get("kind"))
    return out


def classify(body, user_id: str, teams: set[str]) -> dict:
    """Canaries and scope labels in a response that belong to areas the user may not read.

    The recall response never echoes the query, so a foreign canary in the body means a foreign record was returned.
    """
    team_found, personal_found = canaries(body)
    allowed = allowed_owners(user_id, teams)
    leaked = sorted(c for c in team_found | personal_found if owner(c) not in allowed)
    foreign_scopes = sorted(s for s in result_scopes(body) if s not in ("personal", "shared") and s not in teams)
    return {"leaked_canaries": leaked, "foreign_scopes": foreign_scopes,
            "own_canaries": sorted(c for c in team_found if owner(c) in teams)}


def ranked_canaries(body) -> list[set[str]]:
    """Per ranked result, the canaries its content carries."""
    out = []
    for item in (body or {}).get("results", []) or []:
        text = json.dumps(item.get("record", {}), ensure_ascii=False)
        out.append({m.group(0) for m in TEAM_CANARY.finditer(text)} | {m.group(0) for m in PERSONAL_CANARY.finditer(text)})
    return out
