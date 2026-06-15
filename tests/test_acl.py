import pytest

from gateway import acl

CFG = {
    "tok_full": {"sub": "alice", "vaults": ["avl", "ops", "teamwiki"], "write": True},
    "tok_team": {"sub": "bob", "vaults": ["teamwiki"], "write": True},
    "tok_ro": {"sub": "ci", "vaults": ["teamwiki"], "write": False},
}


def scopes(token):
    return acl.scopes_for(acl.build_registry(CFG)[token])


def test_allowed_vaults_are_scoped_per_token():
    assert acl.allowed_vaults(scopes("tok_team")) == {"teamwiki"}
    assert acl.allowed_vaults(scopes("tok_full")) == {"avl", "ops", "teamwiki"}


def test_personal_vault_invisible_to_team_token():
    # The hard requirement: a teammate token must not reach a personal vault.
    with pytest.raises(acl.AccessDenied):
        acl.authorize(scopes("tok_team"), "avl", write=False)
    with pytest.raises(acl.AccessDenied):
        acl.authorize(scopes("tok_team"), "ops", write=False)


def test_write_denied_for_readonly_token():
    with pytest.raises(acl.AccessDenied):
        acl.authorize(scopes("tok_ro"), "teamwiki", write=True)
    acl.authorize(scopes("tok_ro"), "teamwiki", write=False)  # read is fine


def test_forbidden_message_is_opaque():
    with pytest.raises(acl.AccessDenied) as exc:
        acl.authorize(scopes("tok_team"), "ops", write=False)
    # Same shape as a non-existent vault — existence is not revealed.
    assert str(exc.value) == "vault_forbidden: ops"


def test_full_token_can_write_everywhere_it_owns():
    acl.authorize(scopes("tok_full"), "avl", write=True)
    acl.authorize(scopes("tok_full"), "teamwiki", write=True)


def test_build_registry_rejects_string_vaults():
    with pytest.raises(ValueError):
        acl.build_registry({"t": {"sub": "x", "vaults": "teamwiki", "write": True}})


def test_build_registry_rejects_non_bool_write():
    # `write: "false"` is a string — must not be accepted as truthy write access.
    with pytest.raises(ValueError):
        acl.build_registry({"t": {"sub": "x", "vaults": ["a"], "write": "false"}})


def test_build_registry_reads_email():
    reg = acl.build_registry({"t": {"sub": "x", "vaults": ["a"], "write": True, "email": "x@e.com"}})
    assert reg["t"].email == "x@e.com"
