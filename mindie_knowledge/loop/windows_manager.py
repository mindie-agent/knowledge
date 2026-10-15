"""Task Scheduler and ACL transport through the installed Windows APIs.

Launch intents, ownership validation and reconciliation belong to
windows_service. This module does not retain COM objects across calls/threads.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

from .service_host import ServiceHostError

_CHANGED_MODE = 0x80010106
_DISPATCH_EXCEPTION = 0x80020009
_NOT_FOUND = 0x80070002
_STATES = {1: "Disabled", 2: "Queued", 3: "Ready", 4: "Running"}


def _api():
    import pythoncom
    import win32api
    import win32con
    import win32security
    import win32timezone
    from win32com.client import dynamic
    return SimpleNamespace(com=pythoncom, system=win32api, constants=win32con,
                           security=win32security, zone=win32timezone,
                           dispatch=dynamic.Dispatch)


def _hresult(exc):
    value = getattr(exc, "hresult", None)
    if type(value) is not int:
        return None
    value &= 0xFFFFFFFF
    if value == _DISPATCH_EXCEPTION:
        info = getattr(exc, "excepinfo", None)
        if isinstance(info, tuple) and len(info) > 5 and type(info[5]) is int:
            return info[5] & 0xFFFFFFFF
    return value


def _call(stage, operation):
    initialized = False
    scheduler = folder = None
    api = None
    failure = None
    invoked = False
    try:
        api = _api()
        try:
            api.com.CoInitializeEx(api.com.COINIT_APARTMENTTHREADED)
            initialized = True
        except Exception as exc:
            if _hresult(exc) != _CHANGED_MODE:
                raise
            # A caller-owned apartment is valid, but this call did not acquire
            # another initialization reference and must not release one.
        scheduler = api.dispatch("Schedule.Service")
        scheduler.Connect()
        folder = scheduler.GetFolder("\\")
        invoked = True
        result = operation(folder, api)
    except Exception as exc:
        failure = (exc if isinstance(exc, ServiceHostError) else
                   ServiceHostError("manager_operation_failed", stage=stage,
                                    uncertain=invoked and stage in {"register", "start", "remove"},
                                    manager_hresult=_hresult(exc)))
        # A COM exception traceback can retain operation arguments. Drop those
        # references before closing an apartment, including on failed calls.
        exc.__traceback__ = None
    finally:
        folder = None
        scheduler = None
        if initialized:
            api.com.CoUninitialize()
    if failure is not None:
        raise failure from None
    return result


def _utc_text(value, local_zone):
    if not isinstance(value, datetime):
        raise ServiceHostError("manager_status_invalid", stage="query")
    # Scheduler's DATE is local wall time; pywin32 attaches a generic timezone.
    # Match the old .NET unspecified DateTime.ToUniversalTime roundtrip format,
    # including the never-run sentinel, rather than changing launch evidence.
    utc = value.replace(tzinfo=local_zone).astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond:06d}0Z"


def task(label):
    def read(folder, api):
        selected = principal = None
        try:
            try:
                selected = folder.GetTask(label)
            except Exception as exc:
                if _hresult(exc) == _NOT_FOUND:
                    return None
                raise
            principal = selected.Definition.Principal
            sid = principal.UserId
            if not sid.startswith("S-1-"):
                sid = api.security.ConvertSidToStringSid(
                    api.security.LookupAccountName(None, sid)[0])
            return dict(xml=selected.Xml, sid=sid, state=_STATES.get(selected.State),
                        run_level=principal.RunLevel,
                        last_result=int(selected.LastTaskResult) & 0xFFFFFFFF,
                        last_run=_utc_text(selected.LastRunTime, api.zone.TimeZoneInfo.local()))
        finally:
            principal = None
            selected = None
    return _call("query", read)


def register(label, xml, sid, *, replace):
    # Only windows_service's exact verified ownership permits replacement.
    return _call("register", lambda folder, api: _discard(
        folder.RegisterTask(label, xml, 6 if replace else 2, sid, None, 3, None)))


def start(label):
    return _call("start", lambda folder, api: _discard(folder.GetTask(label).Run(None)))


def remove(label):
    return _call("remove", lambda folder, api: folder.DeleteTask(label, 0))


def _discard(value):
    # Registered/running task wrappers must be released inside the apartment.
    return None


def private_sid(path):
    try:
        api = _api()
        token = api.security.OpenProcessToken(api.system.GetCurrentProcess(), api.constants.TOKEN_QUERY)
        try:
            sid = api.security.GetTokenInformation(token, api.security.TokenUser)[0]
        finally:
            token.Close()
        current = api.security.ConvertSidToStringSid(sid)
        descriptor = api.security.GetNamedSecurityInfo(
            str(path), api.security.SE_FILE_OBJECT,
            api.security.OWNER_SECURITY_INFORMATION | api.security.DACL_SECURITY_INFORMATION)
        owner = api.security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())
        acl = descriptor.GetSecurityDescriptorDacl()
        if owner != current or acl is None:
            raise ValueError()
        allowed = {current, "S-1-5-18", "S-1-5-32-544"}
        for index in range(acl.GetAceCount()):
            ace = acl.GetAce(index)
            kind = ace[0][0]
            if kind == api.security.ACCESS_ALLOWED_ACE_TYPE:
                if api.security.ConvertSidToStringSid(ace[-1]) not in allowed:
                    raise ValueError()
            elif kind != api.security.ACCESS_DENIED_ACE_TYPE:
                raise ValueError()
        return current
    except Exception:
        raise ServiceHostError("private_directory_unavailable", stage="ownership") from None
