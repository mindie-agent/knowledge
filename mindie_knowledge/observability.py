"""Knowledge outcome interpretation; storage and redaction stay shared."""
from functools import wraps
from contextvars import ContextVar
from mindie_diagnostics import get_recorder

_ACTIVE = ContextVar("knowledge_diagnostic_operation", default=None)
_EXPECTED_EXIT = ContextVar("knowledge_expected_exit", default=None)


def cli_outcome(category, exit_code, *, caller=False, **attributes):
    """Used only at sites that know why a nonzero CLI result was produced."""
    op = _ACTIVE.get()
    if op is not None:
        _EXPECTED_EXIT.set(exit_code)
        if caller:
            op.fail(category, classification="caller", exit_code=exit_code, **attributes)
        else:
            op.event("WARNING", category, exit_code=exit_code, **attributes)


def _argparse_error(error):
    # An arbitrary business SystemExit(2) is not proof of a caller mistake.
    traceback = error.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        if frame.f_globals.get("__name__") == "argparse" and frame.f_code.co_name == "error":
            return True
        traceback = traceback.tb_next
    return False


def capture_failure(error, category="internal_error"):
    op = _ACTIVE.get()
    if op is not None:
        op.fail(category, exception=error)


def observed(name, *, level="INFO"):
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            with get_recorder("mindie-knowledge").operation(name, level=level) as op:
                token = _ACTIVE.set(op)
                expected_token = _EXPECTED_EXIT.set(None)
                try:
                    result = function(*args, **kwargs)
                    expected_exit = _EXPECTED_EXIT.get()
                except SystemExit as exc:
                    if _argparse_error(exc):
                        op.fail("argument_validation", classification="caller")
                    raise
                finally:
                    _ACTIVE.reset(token)
                    _EXPECTED_EXIT.reset(expected_token)
                if type(result) is int and result != 0 and result != expected_exit and op.status != "error":
                    op.fail("returned_failure", exit_code=result)
                elif isinstance(result, dict) and isinstance(result.get("status"), str) and result["status"] in {"pending", "unavailable", "partial", "failed"}:
                    if result.get("reason"):
                        op.fail("maintenance_unavailable", retryable=True)
                    else:
                        op.event("WARNING", "knowledge.incomplete", status=result["status"])
                elif isinstance(result, tuple) and len(result) == 2 and result[1] is True:
                    op.fail(result[0].get("error", "tool_error") if isinstance(result[0], dict) else "tool_error")
                elif isinstance(result, dict) and isinstance(result.get("error"), dict):
                    code = result["error"].get("code")
                    op.fail("protocol_response", error_code=code,
                            classification="caller" if type(code) is int and code in {-32700, -32600, -32601, -32602} else "unknown")
                return result
        return call
    return decorate
