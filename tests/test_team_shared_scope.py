"""Shared is the company-wide open area: every authenticated user may write it, team readers included."""
import pytest

from team_memory.contracts import Forbidden, Scope, ScopeKind
from team_memory.registry import Registry


def test_team_reader_writes_shared_but_not_the_team(tmp_path):
    registry = Registry(tmp_path)
    registry.add_user('reader', 'Reader')
    registry.add_user('outsider', 'Outsider')
    registry.add_team('hr', 'HR')
    registry.membership('reader', 'hr', 'reader')
    for user in ('reader', 'outsider'):
        actor = registry.authenticate(registry.issue_token(user, 'test'))
        assert registry.authorize(actor, Scope(kind=ScopeKind.shared), True).writable
    reader = registry.authenticate(registry.issue_token('reader', 'test'))
    with pytest.raises(Forbidden):
        registry.authorize(reader, Scope(kind=ScopeKind.team, team_id='hr'), True)
