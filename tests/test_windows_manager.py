"""Windows API transport contracts without a scheduler or interactive token."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from mindie_knowledge.loop import windows_manager as manager

_XML = ('<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        '<Principals><Principal><UserId>S-1-5-21-123</UserId></Principal></Principals></Task>')


class ComError(Exception):
    def __init__(self, code, *, inner=None):
        super().__init__("private COM source text")
        self.hresult = code
        self.excepinfo = (0, None, None, None, 0, inner) if inner is not None else None


@pytest.fixture
def api(monkeypatch):
    state = dict(calls=[], init_error=None, connect_error=None, operation_error=None,
                 selected=None)

    def initialize(mode):
        state["calls"].append(("initialize", mode))
        if state["init_error"]:
            raise state["init_error"]

    class Folder:
        def GetTask(self, label):
            state["calls"].append(("get", label))
            if state["operation_error"]:
                raise state["operation_error"]
            return state["selected"]

        def RegisterTask(self, *arguments):
            state["calls"].append(("register", arguments))
            if state["operation_error"]:
                raise state["operation_error"]
            return object()

        def DeleteTask(self, *arguments):
            state["calls"].append(("delete", arguments))
            if state["operation_error"]:
                raise state["operation_error"]

    class Scheduler:
        def Connect(self):
            state["calls"].append(("connect",))
            if state["connect_error"]:
                raise state["connect_error"]

        def GetFolder(self, path):
            assert path == "\\"
            return Folder()

    def dispatch(name):
        assert name == "Schedule.Service"
        return Scheduler()

    value = SimpleNamespace(
        com=SimpleNamespace(COINIT_APARTMENTTHREADED=2, CoInitializeEx=initialize,
                            CoUninitialize=lambda: state["calls"].append(("uninitialize",))),
        dispatch=dispatch,
        security=SimpleNamespace(ConvertSidToStringSid=str,
                                 ConvertStringSidToSid=lambda value: value,
                                 LookupAccountName=lambda unused, name: ("S-1-5-21-123",)),
        zone=SimpleNamespace(TimeZoneInfo=SimpleNamespace(
            local=lambda: timezone(timedelta(hours=8)))),
    )
    monkeypatch.setattr(manager, "_api", lambda: value)
    return value, state


def test_create_and_owned_replace_use_exact_interactive_user_contract(api):
    _, state = api
    manager.register("owned", "<Task/>", "S-1-5-21-123", replace=False)
    manager.register("owned", "<Task/>", "S-1-5-21-123", replace=True)
    calls = [arguments for kind, *rest in state["calls"] if kind == "register"
             for arguments in rest]
    assert calls == [
        ("owned", "<Task/>", 2, "S-1-5-21-123", None, 3, None),
        ("owned", "<Task/>", 6, "S-1-5-21-123", None, 3, None),
    ]
    assert state["calls"].count(("uninitialize",)) == 2


def test_start_and_remove_do_not_add_triggers_or_retry(api):
    _, state = api
    state["selected"] = SimpleNamespace(Run=lambda value: state["calls"].append(("run", value)))
    manager.start("owned")
    manager.remove("owned")
    assert ("get", "owned") in state["calls"]
    assert state["calls"].count(("run", None)) == 1
    assert state["calls"].count(("delete", ("owned", 0))) == 1


@pytest.mark.parametrize("code,inner", [
    (0x80070002, None), (0x80020009, -2147024894),
])
def test_only_exact_file_not_found_is_absence(api, code, inner):
    _, state = api
    state["operation_error"] = ComError(code, inner=inner)
    assert manager.task("owned") is None
    assert state["calls"].count(("uninitialize",)) == 1


def test_access_denied_is_query_failure_and_mutation_stays_uncertain(api):
    _, state = api
    state["operation_error"] = ComError(0x80020009, inner=-2147024891)
    with pytest.raises(manager.ServiceHostError) as caught:
        manager.task("owned")
    assert caught.value.manager_hresult == 0x80070005
    assert "private" not in str(caught.value)
    with pytest.raises(manager.ServiceHostError) as caught:
        manager.register("owned", "<Task/>", "S-1-5-21-123", replace=False)
    assert caught.value.uncertain
    assert state["calls"].count(("uninitialize",)) == 2


def test_connection_failure_is_before_any_mutation(api):
    _, state = api
    state["connect_error"] = ComError(0x800706BA)
    with pytest.raises(manager.ServiceHostError) as caught:
        manager.start("owned")
    assert not caught.value.uncertain
    assert not any(call[0] in {"get", "run"} for call in state["calls"])


def test_different_existing_apartment_is_not_uninitialized(api):
    _, state = api
    state["init_error"] = ComError(-2147417850)
    manager.remove("owned")
    assert ("delete", ("owned", 0)) in state["calls"]
    assert ("uninitialize",) not in state["calls"]


def test_other_initialization_error_does_not_continue(api):
    _, state = api
    state["init_error"] = ComError(0x80070005)
    with pytest.raises(manager.ServiceHostError) as caught:
        manager.remove("owned")
    assert not caught.value.uncertain
    assert state["calls"] == [("initialize", 2)]


def test_scheduler_local_date_preserves_legacy_seven_digit_utc_format(api):
    _, state = api
    principal = SimpleNamespace(UserId="local-user", RunLevel=0)
    state["selected"] = SimpleNamespace(
        Definition=SimpleNamespace(Principal=principal), Xml=_XML, State=3,
        LastTaskResult=-2147024891,
        LastRunTime=datetime(1899, 12, 30, tzinfo=timezone.utc))
    result = manager.task("owned")
    assert result == dict(xml=_XML, sid="S-1-5-21-123", state="Ready", run_level=0,
                          last_result=0x80070005, last_run="1899-12-29T16:00:00.0000000Z")


def test_exact_xml_sid_avoids_machine_and_user_name_collision(api):
    value, state = api
    lookups = []
    value.security.LookupAccountName = lambda *args: lookups.append(args) or ("S-1-5-21",)
    conversions = []
    value.security.ConvertStringSidToSid = lambda sid: conversions.append(sid) or sid
    state["selected"] = SimpleNamespace(
        Definition=SimpleNamespace(Principal=SimpleNamespace(UserId="MACHINE", RunLevel=0)),
        Xml=_XML, State=3, LastTaskResult=267011,
        LastRunTime=datetime(1899, 12, 30))
    assert manager.task("owned")["sid"] == "S-1-5-21-123"
    assert conversions == ["S-1-5-21-123"]
    assert lookups == []


@pytest.mark.parametrize("xml", [
    "<Task/>", "<Task", _XML.replace("<UserId>S-1-5-21-123</UserId>", ""),
    _XML.replace("S-1-5-21-123", "MACHINE"),
    _XML.replace("<UserId>S-1-5-21-123</UserId>", "<UserId/>"),
    _XML.replace("</UserId>", "</UserId><UserId>S-1-5-21-123</UserId>"),
    _XML.replace("</Principal>", "</Principal><Principal/>"),
    _XML.replace("</Principals>", "</Principals><Principals/>"),
    _XML.replace("</UserId>", "<Unexpected/></UserId>"),
])
def test_missing_ambiguous_or_malformed_xml_principal_is_visible_failure(api, xml):
    value, _ = api
    with pytest.raises(manager.ServiceHostError) as caught:
        manager._principal_sid(xml, value.security)
    assert caught.value.code == "manager_status_invalid"
    assert caught.value.stage == "query"
    assert not caught.value.uncertain


def test_invalid_sid_rejected_by_windows_conversion_is_visible_failure(api):
    value, _ = api
    def reject(sid):
        raise ValueError("private invalid SID")
    value.security.ConvertStringSidToSid = reject
    with pytest.raises(manager.ServiceHostError, match="manager_status_invalid") as caught:
        manager._principal_sid(_XML.replace("S-1-5-21-123", "S-1-invalid"), value.security)
    assert "private" not in str(caught.value)


def test_private_sid_uses_actual_token_user_and_closes_token(api):
    value, state = api
    value.system = SimpleNamespace(GetCurrentProcess=lambda: "current")
    value.constants = SimpleNamespace(TOKEN_QUERY=8)
    value.security = SimpleNamespace(
        TokenUser=1,
        OpenProcessToken=lambda *args: SimpleNamespace(
            Close=lambda: state["calls"].append(("token_closed",))),
        GetTokenInformation=lambda *args: ("S-1-5-21-123", 0),
        ConvertSidToStringSid=str)
    assert manager.private_sid("private") == "S-1-5-21-123"
    assert state["calls"] == [("token_closed",)]

