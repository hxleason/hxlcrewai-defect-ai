import pytest
from app.core.safe_expr import safe_eval, SafeExprError


def test_arithmetic():
    assert safe_eval("1 + 2 * 3") == 7
    assert safe_eval("7 // 2") == 3
    assert safe_eval("7 % 3") == 1
    assert safe_eval("3 / 2") == 1.5
    assert safe_eval("10 - 4 - 3") == 3


def test_power_right_assoc_and_unary():
    assert safe_eval("2**3**2") == 512
    assert safe_eval("-2**2") == -4


def test_comparisons_chained():
    assert safe_eval("1 < 2 <= 2") is True
    assert safe_eval("3 >= 3") is True


def test_membership():
    assert safe_eval("'a' in ['a', 'b']") is True
    assert safe_eval("'x' not in ['a', 'b']") is True


def test_logic_not():
    assert safe_eval("not True") is False


def test_logic_and():
    assert safe_eval("True and False") is False
    assert safe_eval("x > 1 and x < 5", {"x": 3}) is True


def test_logic_or():
    assert safe_eval("False or True") is True
    assert safe_eval("a > 10 or b == 'Q345R'", {"a": 1, "b": "Q345R"}) is True


def test_whitelist_functions():
    assert safe_eval("min(3, 1, 2)") == 1
    assert safe_eval("max(3, 1, 2)") == 3
    assert safe_eval("abs(-5)") == 5
    assert safe_eval("round(3.14159, 2)") == 3.14


def test_context_and_literals():
    assert safe_eval("design_temperature >= 300",
                     {"design_temperature": 320}) is True
    assert safe_eval("'a' + 'b'") == "ab"
    assert safe_eval("(1, 2)") == (1, 2)
    assert safe_eval("[1, 2]") == [1, 2]
    assert safe_eval("None") is None


@pytest.mark.parametrize("payload", [
    "__import__('os').system('id')",
    "open('/etc/passwd')",
    "x.__class__",
    "a[0]",
    "lambda: 0",
    "print('hi')",
    "eval('1+1')",
])
def test_injection_rejected(payload):
    with pytest.raises(SafeExprError):
        safe_eval(payload, {"a": [1, 2, 3], "x": "s"})


@pytest.mark.parametrize("expr,ctx", [
    ("nope > 1", {}),
    ("1 / 0", {}),
    ("", {}),
])
def test_errors(expr, ctx):
    with pytest.raises(SafeExprError):
        safe_eval(expr, ctx)


def test_non_string_rejected():
    with pytest.raises(SafeExprError):
        safe_eval(123)
