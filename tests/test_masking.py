import pytest
from fastmcp.exceptions import ToolError

from gateway import acl
from gateway.tools import _expected_to_tool_error


def test_expected_failures_become_toolerror():
    for exc in (
        FileNotFoundError("not_found: x"),
        FileExistsError("exists: x"),
        ValueError("too_large: x"),
        ValueError("bad_message: empty commit message"),
        ValueError("frontmatter_unparseable: x"),
        PermissionError("path_escape: x"),
        acl.AccessDenied("vault_forbidden: x"),
    ):
        @_expected_to_tool_error
        def f(e=exc):
            raise e

        with pytest.raises(ToolError):
            f()


def test_unexpected_failures_pass_through_unmasked():
    @_expected_to_tool_error
    def f():
        raise PermissionError("Operation not permitted: /etc/secret")  # no expected prefix

    with pytest.raises(PermissionError):
        f()


def test_toolerror_is_not_rewrapped():
    @_expected_to_tool_error
    def f():
        raise ToolError("already a tool error")

    with pytest.raises(ToolError):
        f()


def test_no_tool_is_async():
    # _expected_to_tool_error wraps sync callables only: an async tool would hand back its
    # coroutine before the wrapper could map the error, and the failure would be silent.
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parents[1] / "gateway" / "tools.py").read_text())
    registered = [n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and any(isinstance(d, ast.Name) and d.id in {"tool", "wtool"} for d in n.decorator_list)]
    assert registered
    assert [n.name for n in registered if isinstance(n, ast.AsyncFunctionDef)] == []
