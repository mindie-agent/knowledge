"""A rebuilt database may forget tenant auth; network errors must not rotate it."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mindie_knowledge.distribution import client as distribution_client
from mindie_knowledge.local import instance as module
from mindie_knowledge.local.embedding import PreparedModel
from mindie_knowledge.local.instance import LocalInstance


def test_database_rebuild_reprovisions_only_rejected_tenant_key(tmp_path, monkeypatch):
    instance = LocalInstance(tmp_path)
    credentials = {"root_key": "retained-root", "data_key": "forgotten-tenant"}
    instance._save_credentials(credentials)
    check = Mock(side_effect=[False, True])
    provision = Mock(return_value="recovered-tenant")
    monkeypatch.setattr(module, "_tenant_key_accepted", check)
    monkeypatch.setattr(distribution_client, "provision_tenant_key", provision)
    instance._ensure_data_key("http://127.0.0.1:9000", credentials)
    provision.assert_called_once_with("http://127.0.0.1:9000", root_key="retained-root")
    assert instance._credentials() == {"root_key": "retained-root", "data_key": "recovered-tenant"}
    assert [call.args[1] for call in check.call_args_list] == ["forgotten-tenant", "recovered-tenant"]


@pytest.mark.parametrize("error", [TimeoutError("timeout"), OSError("connection refused")])
def test_unavailable_server_preserves_credentials(tmp_path, monkeypatch, error):
    instance = LocalInstance(tmp_path)
    credentials = {"root_key": "retained-root", "data_key": "retained-tenant"}
    instance._save_credentials(credentials)
    monkeypatch.setattr(module, "_tenant_key_accepted", Mock(side_effect=error))
    provision = Mock()
    monkeypatch.setattr(distribution_client, "provision_tenant_key", provision)
    with pytest.raises(type(error)):
        instance._ensure_data_key("http://127.0.0.1:9000", credentials)
    provision.assert_not_called()
    assert instance._credentials() == credentials


def test_rejected_replacement_does_not_overwrite_old_key(tmp_path, monkeypatch):
    instance = LocalInstance(tmp_path)
    credentials = {"root_key": "retained-root", "data_key": "retained-tenant"}
    instance._save_credentials(credentials)
    monkeypatch.setattr(module, "_tenant_key_accepted", Mock(return_value=False))
    monkeypatch.setattr(distribution_client, "provision_tenant_key", Mock(return_value="also-invalid"))
    with pytest.raises(RuntimeError, match="recovered.*rejected"):
        instance._ensure_data_key("http://127.0.0.1:9000", credentials)
    assert instance._credentials() == credentials


def test_live_ensure_repairs_auth_without_restarting_services(tmp_path, monkeypatch):
    instance = LocalInstance(tmp_path)
    instance._save_credentials({"root_key": "root", "data_key": "old"})
    status = {"live": True, "pid": {}, "openviking_url": "http://127.0.0.1:9000"}
    monkeypatch.setattr(instance, "describe", lambda: status)
    monkeypatch.setattr(instance, "prepare_model", lambda *args, **kwargs: PreparedModel(instance.cache_dir, False, "local-load"))
    monkeypatch.setattr(module, "_tenant_key_accepted", Mock(side_effect=[False, True]))
    monkeypatch.setattr(distribution_client, "provision_tenant_key", Mock(return_value="new"))
    stop = Mock()
    monkeypatch.setattr(instance, "_stop_owned", stop)
    assert instance.ensure() == status
    stop.assert_not_called()
    assert instance.data_key() == "new"


@pytest.mark.parametrize("failure, expected", [("auth", False), ("missing", True), ("permission", None), ("network", None)])
def test_native_probe_distinguishes_auth_from_missing_resource_and_network(monkeypatch, failure, expected):
    import openviking_sdk
    from openviking_sdk.errors import NotFoundError, PermissionDeniedError, UnauthenticatedError

    errors = {"auth": UnauthenticatedError("Invalid API Key"),
              "missing": NotFoundError("viking://resources"),
              "permission": PermissionDeniedError(), "network": TimeoutError("timeout")}
    client = SimpleNamespace(initialize=Mock(), stat=Mock(side_effect=errors[failure]), close=Mock())
    constructor = Mock(return_value=client)
    monkeypatch.setattr(openviking_sdk, "SyncHTTPClient", constructor)
    if expected is None:
        with pytest.raises(type(errors[failure])):
            module._tenant_key_accepted("http://127.0.0.1:9000", "private")
    else:
        assert module._tenant_key_accepted("http://127.0.0.1:9000", "private") is expected
    client.close.assert_called_once()
    assert constructor.call_args.kwargs["timeout"] == 5.0
