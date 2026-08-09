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


def _hidden_names(source):
    """Every definition unreachable to a direct `python -m tests.<module>` run.

    Shared with the tests below rather than duplicated, so a test cannot pass
    against a copy of this logic while the real check is empty.
    """
    tree = ast.parse(source)
    index = _guard_index(tree)
    if index is None:
        return []  # No guard, nothing to hide behind.

    # Walk rather than scan the top level: a class nested inside a `try` or an
    # `if` after the guard is just as unreachable.
    return [
        node.name
        for parent in tree.body[index + 1 :]
        for node in ast.walk(parent)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    ]


@pytest.mark.parametrize("path", TEST_FILES, ids=lambda p: p.name)
def test_nothing_is_defined_after_the_main_guard(path):
    hidden = _hidden_names(path.read_text())
    assert not hidden, (
        f"{path.name} defines {hidden} after `if __name__ == '__main__'`, "
        "so a direct unittest run silently skips them"
    )


# The detector needs its own tests: a guard that silently stops detecting is
# the same failure it was written to catch, one level up.
_AFTER_THE_GUARD = """
import unittest


class TestEarly(unittest.TestCase):
    pass


if __name__ == "__main__":
    unittest.main()


class TestLate(unittest.TestCase):
    pass
"""

_NESTED_AFTER_THE_GUARD = """
import unittest

if __name__ == "__main__":
    unittest.main()

try:
    class TestNested(unittest.TestCase):
        pass
except Exception:
    pass
"""

_REVERSED_GUARD = """
import unittest

if "__main__" == __name__:
    unittest.main()


class TestLate(unittest.TestCase):
    pass
"""

_CLEAN = """
import unittest


class TestEarly(unittest.TestCase):
    pass


if __name__ == "__main__":
    unittest.main()
"""


def test_it_finds_a_class_after_the_guard():
    assert _hidden_names(_AFTER_THE_GUARD) == ["TestLate"]


def test_it_finds_a_class_nested_after_the_guard():
    assert _hidden_names(_NESTED_AFTER_THE_GUARD) == ["TestNested"]


def test_it_finds_the_guard_with_reversed_operands():
    assert _hidden_names(_REVERSED_GUARD) == ["TestLate"]


def test_it_passes_a_correctly_ordered_module():
    assert _hidden_names(_CLEAN) == []
