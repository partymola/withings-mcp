"""A gate for a mistake that has now happened twice.

Appending a test class to a file whose `if __name__ == "__main__"` guard is
already at the end leaves it after `unittest.main()`, which calls `sys.exit`
before the class is defined. pytest still collects it, so CI stays green and
the omission is invisible: the only symptom is a smaller count from a direct
`python -m tests.<module>` run, which nobody watches.

Vigilance did not catch it either time. This does.
"""

import ast
from pathlib import Path

import pytest

TEST_FILES = sorted(Path(__file__).parent.glob("test_*.py"))


def _guard_index(tree):
    for i, node in enumerate(tree.body):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and getattr(node.test.left, "id", None) == "__name__"
        ):
            return i
    return None


@pytest.mark.parametrize("path", TEST_FILES, ids=lambda p: p.name)
def test_nothing_is_defined_after_the_main_guard(path):
    tree = ast.parse(path.read_text())
    index = _guard_index(tree)
    if index is None:
        return  # No guard, nothing to hide behind.
    after = [
        node
        for node in tree.body[index + 1 :]
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    names = [node.name for node in after]
    assert not names, (
        f"{path.name} defines {names} after `if __name__ == '__main__'`, "
        "so a direct unittest run silently skips them"
    )
