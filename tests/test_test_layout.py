"""A structural check on the test files themselves.

`unittest.main()` calls `sys.exit`, so anything defined after a module's
`if __name__ == "__main__"` guard never runs under a direct
`python -m tests.<module>` invocation. pytest still collects it, so the
omission does not show up in CI: the only symptom is a smaller count from a
runner nobody watches.
"""

import ast
from pathlib import Path

import pytest

TEST_FILES = sorted(Path(__file__).parent.glob("test_*.py"))


def _is_main_guard(node):
    """True for any `if` whose test mentions __name__ and "__main__".

    Written loosely on purpose: `__name__ == "__main__"`, the reversed
    operands, and an `and`-extended condition all end the module the same way.
    """
    if not isinstance(node, ast.If):
        return False
    names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
    constants = {c.value for c in ast.walk(node.test) if isinstance(c, ast.Constant)}
    return "__name__" in names and "__main__" in constants


def _guard_index(tree):
    for i, node in enumerate(tree.body):
        if _is_main_guard(node):
            return i
    return None


@pytest.mark.parametrize("path", TEST_FILES, ids=lambda p: p.name)
def test_nothing_is_defined_after_the_main_guard(path):
    tree = ast.parse(path.read_text())
    index = _guard_index(tree)
    if index is None:
        return  # No guard, nothing to hide behind.

    # Walk rather than scan the top level: a class nested inside a `try` or an
    # `if` after the guard is just as unreachable.
    hidden = [
        node.name
        for parent in tree.body[index + 1 :]
        for node in ast.walk(parent)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert not hidden, (
        f"{path.name} defines {hidden} after `if __name__ == '__main__'`, "
        "so a direct unittest run silently skips them"
    )
