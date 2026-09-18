"""Tree-sitter C++ syntax plus bounded PyTorch/Ascend registration adapters.

No preprocessor, compiler, shared library or inspected code is executed. Unknown
macro expansion, runtime dispatch and overload resolution remain explicit gaps.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re

SUPPORTED_PARSER_VERSIONS = {"tree-sitter": "0.25.2", "tree-sitter-cpp": "0.23.4"}


def parse(data: bytes, path: str) -> dict:
    out = {"symbols": [], "references": [], "imports": [], "gaps": []}
    # Metadata is safe to inspect before importing either native extension.
    # Unverified parser/grammar pairs can terminate the process, so a Python
    # exception handler around Language/Parser cannot provide this boundary.
    observed = {}
    for package in SUPPORTED_PARSER_VERSIONS:
        try:
            version = importlib.metadata.version(package)
            observed[package] = version if isinstance(version, str) else "unavailable"
        except (importlib.metadata.PackageNotFoundError, OSError, ValueError):
            observed[package] = "unavailable"
    if observed != SUPPORTED_PARSER_VERSIONS:
        required = ", ".join(f"{name}=={version}" for name, version in SUPPORTED_PARSER_VERSIONS.items())
        found = ", ".join(f"{name}={version[:80] or 'unknown'}" for name, version in observed.items())
        out["gaps"].append({"kind": "parser_unavailable", "detail":
            f"C++ requires the supported optional code extra: {required}. Found {found}; native parser was not loaded."})
        return out
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_cpp
    except ImportError:
        out["gaps"].append({"kind": "parser_unavailable", "detail": "C++ needs the optional code extra: tree-sitter==0.25.2, tree-sitter-cpp==0.23.4."})
        return out
    parser = Parser(Language(tree_sitter_cpp.language()))
    tree = parser.parse(data)

    def text(node):
        return data[node.start_byte:node.end_byte].decode("utf-8", errors="replace") if node else ""

    def literal(node):
        if node is None:
            return None
        if node.type == "concatenated_string":
            pieces = [literal(child) for child in node.named_children]
            return "".join(pieces) if all(isinstance(p, str) for p in pieces) else None
        if node.type == "string_literal":
            try:
                return json.loads(re.sub(r"\\\r?\n", "", text(node)))
            except (ValueError, TypeError):
                return None
        return None

    def declarator_name(node):
        if node is None:
            return ""
        if node.type in {"identifier", "field_identifier", "qualified_identifier", "destructor_name", "operator_name"}:
            return text(node)
        return declarator_name(node.child_by_field_name("declarator"))

    def symbol(node, name, scope, kind):
        qualified = name if "::" in name else "::".join([*scope, name])
        ident = f"symbol:{path}:{qualified}:{node.start_point.row + 1}"
        out["symbols"].append({"id": ident, "name": name.split("::")[-1], "qualified_name": qualified,
            "kind": kind, "language": "cpp", "line_start": node.start_point.row + 1,
            "line_end": node.end_point.row + 1, "content_sha256": hashlib.sha256(data[node.start_byte:node.end_byte]).hexdigest(),
            "signature": " ".join(text(node.child_by_field_name("declarator")).split())[:500] if kind != "class" else name,
            "returns": text(node.child_by_field_name("type"))[:300] if kind != "class" else None})
        return ident

    def walk(node, scope=(), owner=None, library=None, guards=()):
        if node.type == "comment":
            return
        line = node.start_point.row + 1
        if node.type == "ERROR" or node.is_missing:
            out["gaps"].append({"kind": "parse_error", "line_start": line, "detail": text(node)[:200]})
        if node.type in {"preproc_if", "preproc_ifdef", "preproc_elif", "preproc_else"}:
            guards = (*guards, text(node).splitlines()[0][:200])
        if node.type in {"preproc_def", "preproc_function_def"}:
            out["gaps"].append({"kind": "unexpanded_macro", "line_start": line,
                "detail": text(node.child_by_field_name("name")) or text(node).splitlines()[0][:120]})
            return
        if node.type == "namespace_definition":
            scope = (*scope, text(node.child_by_field_name("name")) or "<anonymous>")
        if node.type in {"class_specifier", "struct_specifier"}:
            name = text(node.child_by_field_name("name"))
            if name:
                owner = symbol(node, name, scope, "class")
                scope = (*scope, name)
        if node.type == "function_definition":
            decl = node.child_by_field_name("declarator")
            name = declarator_name(decl)
            if name.startswith("TORCH_LIBRARY"):
                params = decl.child_by_field_name("parameters") if decl else None
                args = [text(c) for c in params.named_children] if params else []
                namespace = re.sub(r"\s+", "", args[0]) if args else ""
                concat = re.fullmatch(r"CONCAT\(([A-Za-z_]\w*),([A-Za-z_]\w*)\)", namespace)
                if concat:
                    namespace = "".join(concat.groups())
                handle = args[-1].strip() if args else ""
                dispatch = args[1].strip() if "IMPL" in name and len(args) > 2 else ""
                if re.fullmatch(r"[A-Za-z_]\w*", namespace) and re.fullmatch(r"[A-Za-z_]\w*", handle):
                    library = (namespace, handle, dispatch)
                else:
                    out["gaps"].append({"kind": "registration_namespace_unresolved", "line_start": line, "detail": text(decl)[:200]})
            elif name:
                owner = symbol(node, name, scope, "function")
        if node.type == "declaration":
            decl = node.child_by_field_name("declarator")
            cursor = decl
            while cursor and cursor.type != "function_declarator":
                cursor = cursor.child_by_field_name("declarator")
            if cursor:
                name = declarator_name(cursor)
                if name:
                    symbol(node, name, scope, "function_declaration")
        if node.type == "preproc_include":
            out["imports"].append({"module": text(node.child_by_field_name("path")).strip('<>"'), "line_start": line})
        if node.type == "call_expression":
            func = node.child_by_field_name("function")
            name = text(func)
            argnode = node.child_by_field_name("arguments")
            args = list(argnode.named_children) if argnode else []
            ref = {"source": owner or f"file:{path}", "name": name, "scope": "::".join(scope),
                "line_start": line, "line_end": node.end_point.row + 1, "kind": "call_expression",
                "language": "cpp", "guards": list(guards)}
            if name == "EXEC_NPU_CMD" and args:
                ref.update(kind="dynamic_api_name", name=text(args[0]))
            elif name in {"OP_ADD", "REGISTER_TILING_DATA_CLASS"} and args:
                ref.update(kind="ascend_registration", name=text(args[0]), registration=name)
                if len(args) > 1:
                    ref["implementation"] = text(args[1])
            elif library and name in {library[1] + ".def", library[1] + ".impl"} and args:
                value = literal(args[0])
                if value:
                    op = value.split("(", 1)[0].strip()
                    ref.update(kind="torch_schema" if name.endswith(".def") else "torch_registration",
                        name=f"{library[0]}::{op}", schema=value, dispatch=library[2])
                    if name.endswith(".impl") and len(args) >= 2:
                        target = text(args[-1]).lstrip("&").strip()
                        if re.fullmatch(r"(?:[A-Za-z_]\w*::)*[A-Za-z_]\w*", target):
                            ref["implementation"] = target
                        else:
                            ref["implementation_unresolved"] = target[:200]
                        if len(args) >= 3:
                            ref["dispatch"] = text(args[1])
                else:
                    out["gaps"].append({"kind": "registration_schema_unresolved", "line_start": line, "detail": name})
            out["references"].append(ref)
        for child in node.named_children:
            walk(child, scope, owner, library, guards)

    walk(tree.root_node)
    out["gaps"].append({"kind": "static_scope", "detail": "Syntax and explicit registrations only; no preprocessor evaluation, template instantiation, overload resolution or runtime execution evidence."})
    return out
