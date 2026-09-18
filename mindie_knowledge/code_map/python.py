"""Python syntax facts, without importing or executing the inspected project."""
from __future__ import annotations

import ast
import hashlib
import io
import tokenize
import warnings


def dotted(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else ""
    return ""


def parse(data: bytes, path: str) -> dict:
    out = {"symbols": [], "references": [], "imports": [], "gaps": []}
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        source = data.decode(encoding)
        with warnings.catch_warnings(record=True) as observed:
            warnings.simplefilter("always")
            tree = ast.parse(source, filename=path)
        out["gaps"].extend({"kind": "syntax_warning", "line_start": warning.lineno,
                            "detail": str(warning.message)[:300]} for warning in observed)
    except (SyntaxError, UnicodeError, LookupError) as exc:
        out["gaps"].append({"kind": "parse_error", "line_start": getattr(exc, "lineno", None), "detail": str(exc)[:500]})
        return out

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.scope = []
            self.owners = []

        def definition(self, node, kind):
            name = ".".join([*self.scope, node.name])
            ident = f"symbol:{path}:{name}:{node.lineno}"
            signature = ast.get_source_segment(source, node) or ""
            out["symbols"].append({"id": ident, "name": node.name, "qualified_name": name,
                "kind": kind, "language": "python", "line_start": node.lineno,
                "line_end": node.end_lineno, "content_sha256": hashlib.sha256(signature.encode()).hexdigest(),
                "signature": (f"{node.name}({ast.unparse(node.args)})" if kind == "function" else node.name)[:500],
                "returns": ast.unparse(node.returns)[:300] if kind == "function" and node.returns else None,
                "decorators": [ast.unparse(item)[:200] for item in node.decorator_list[:12]]})
            self.scope.append(node.name)
            self.owners.append(ident)
            self.generic_visit(node)
            self.owners.pop()
            self.scope.pop()

        def visit_FunctionDef(self, node):
            self.definition(node, "function")

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            self.definition(node, "class")

        def visit_Import(self, node):
            for alias in node.names:
                out["imports"].append({"alias": alias.asname or alias.name.split(".")[0],
                    "module": alias.name, "member": "", "level": 0, "line_start": node.lineno,
                    "scope": ".".join(self.scope), "bind_full_module": bool(alias.asname)})

        def visit_ImportFrom(self, node):
            for alias in node.names:
                out["imports"].append({"alias": alias.asname or alias.name,
                    "module": node.module or "", "member": alias.name, "level": node.level,
                    "line_start": node.lineno, "scope": ".".join(self.scope)})
                if alias.name == "*":
                    out["gaps"].append({"kind": "wildcard_import", "line_start": node.lineno,
                                        "detail": "Wildcard export resolution needs project semantics."})

        def visit_Call(self, node):
            target = dotted(node.func)
            out["references"].append({"source": self.owners[-1] if self.owners else f"file:{path}",
                "name": target or ast.get_source_segment(source, node.func) or "<dynamic>",
                "scope": ".".join(self.scope), "line_start": node.lineno, "line_end": node.end_lineno,
                "kind": "call_expression" if target else "dynamic_expression", "language": "python"})
            self.generic_visit(node)

    Visitor().visit(tree)
    return out
