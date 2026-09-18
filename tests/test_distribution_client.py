"""Client seam: tenant provisioning, pins and tenant-key CLI."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mindie_knowledge.distribution.__main__ import main as cli_main
from mindie_knowledge.distribution.client import provision_tenant_key
from mindie_knowledge.distribution.errors import DistributionError


class FakeAdmin:
    def __init__(self, *, exists: bool = False, key: str = "tenant-key-1"):
        self.exists = exists
        self.key = key
        self.calls: list[tuple] = []
        self.closed = False

    def admin_create_account(self, account_id: str, user_id: str):
        self.calls.append(("create", account_id, user_id))
        if self.exists:
            from openviking_sdk.errors import AlreadyExistsError

            raise AlreadyExistsError("exists")
        return {"user_key": self.key}

    def admin_register_user(self, account_id: str, user_id: str, role: str = "user"):
        self.calls.append(("register", account_id, user_id, role))
        return {"user_key": self.key + "-registered"}

    def close(self):
        self.closed = True


def test_provision_creates_tenant():
    admin = FakeAdmin()
    key = provision_tenant_key(
        "http://127.0.0.1:1", root_key="root", user_id="ci", connect=lambda *a, **k: admin
    )
    assert key == "tenant-key-1"
    assert admin.calls == [("create", "default", "ci")]
    assert admin.closed


def test_provision_reuses_existing_account():
    admin = FakeAdmin(exists=True)
    key = provision_tenant_key(
        "http://127.0.0.1:1", root_key="root", user_id="ci", connect=lambda *a, **k: admin
    )
    assert key == "tenant-key-1-registered"
    assert [call[0] for call in admin.calls] == ["create", "register"]
    assert admin.calls[1][3] == "admin"


def test_provision_requires_a_key():
    class EmptyAdmin(FakeAdmin):
        def admin_create_account(self, account_id, user_id):
            return {"no_key": True}

    with pytest.raises(DistributionError, match="user_key"):
        provision_tenant_key(
            "http://127.0.0.1:1", root_key="root", connect=lambda *a, **k: EmptyAdmin()
        )


def test_pins_prints_the_contract(capsys):
    assert cli_main(["pins"]) == 0
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["openviking"] == "0.4.19"
    assert payload["embedding_dimension"] == 384
    assert payload["vector_mode"] == "require"
    assert payload["embedding_model"].startswith("sentence-transformers/")


def test_tenant_key_cli_requires_env(capsys, monkeypatch):
    monkeypatch.delenv("OV_ROOT_KEY", raising=False)
    assert (
        cli_main(["tenant-key", "--openviking-url", "http://127.0.0.1:1"]) == 1
    )
    assert "OV_ROOT_KEY" in capsys.readouterr().err


def test_tenant_key_cli_prints_bare_key(capsys, monkeypatch):
    monkeypatch.setenv("OV_ROOT_KEY", "root")
    import mindie_knowledge.distribution.client as client_module

    admin = FakeAdmin(key="printed-key")
    original = client_module.provision_tenant_key
    # The CLI imports provision_tenant_key lazily inside the handler; the
    # production default of `connect` binds at def time, so inject via the
    # module attribute here.
    monkeypatch.setattr(
        client_module,
        "provision_tenant_key",
        lambda url, *, root_key, account_id="default", user_id="knowledge-data", connect=None: original(
            url, root_key=root_key, account_id=account_id, user_id=user_id,
            connect=lambda *a, **k: admin,
        ),
    )
    assert cli_main(["tenant-key", "--openviking-url", "http://127.0.0.1:1"]) == 0
    assert capsys.readouterr().out.strip() == "printed-key"
