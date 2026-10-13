"""
app/core/safe_expr.py – 安全表达式求值器（替换 eval）

背景（评估报告 6.5）：
    规则库中的条件表达式（如 PWHT 温度条件 "design_temperature >= 300"）
    此前通过 eval() 求值，存在代码注入风险（任意代码执行）。

    本模块提供白名单式安全求值：
    - 支持：数值/字符串字面量、变量、True/False/None、
      算术(+ - * / // % **)、比较(== != > >= < <= in not in)、
      逻辑(and or not，短路求值)、括号、元组/列表字面量，
      白名单函数 min / max / abs / round。
    - 不支持：属性访问、下标访问、函数调用（白名单外）、
      import / lambda / 生成器，从语法构造上杜绝注入。
    - AST 按表达式文本缓存（lru_cache），高频调用开销极小。

使用方式：
    from app.core.safe_expr import safe_eval, SafeExprError

    ctx = {"design_temperature": 320, "material": "Q345R"}
    assert safe_eval("design_temperature >= 300 and material == 'Q345R'", ctx) is True

    # 注入尝试必然失败：
    # safe_eval("__import__('os').system('id')")  -> SafeExprError
"""
import ast
import operator
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["safe_eval", "SafeExprError"]


class SafeExprError(ValueError):
    """安全表达式解析/求值错误"""

    def __init__(self, message: str, expr: Optional[str] = None, pos: Optional[int] = None):
        self.expr = expr
        self.pos = pos
        if pos is not None:
            message = f"{message}（表达式位置 {pos}）"
        super().__init__(message)


# ══════════════════════════════════════════════════
# 词法分析
# ══════════════════════════════════════════════════
_TOKEN_RE = re.compile(
    r"""
    (?P<WS>\s+)
  | (?P<COMMENT>\#[^\n]*)
  | (?P<NUMBER>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)
  | (?P<STRING>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
  | (?P<OP>==|!=|>=|<=|//|\*\*|[-+*/%<>()\[\],])
  | (?P<NAME>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<BAD>.)
""",
    re.VERBOSE,
)

# 关键字（按名称文本识别，不是独立 token 类型）
_KEYWORD_CONSTANTS = {"True": True, "False": False, "None": None}

# 白名单函数：任何其他函数名一律拒绝
_SAFE_FUNCS = {
    "min": min,
    "max": max,
    "abs": abs,
    "round": round,
}

_BIN_OPS = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": operator.truediv,
    "//": operator.floordiv,
    "%": operator.mod,
    "**": operator.pow,
}

_CMP_OPS = {
    "==": operator.eq,
    "!=": operator.ne,
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}


def _decode_string_literal(text: str) -> str:
    """解码字符串字面量（含转义）。ast.literal_eval 仅用于字面量，绝对安全。"""
    try:
        return ast.literal_eval(text)  # noqa: S307 — 只喂字符串字面量，非任意表达式
    except Exception:
        body = text[1:-1]
        return (
            body.replace("\\'", "'")
            .replace('\\"', '"')
            .replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace("\\\\", "\\")
        )


def _tokenize(expr: str) -> List[Tuple[str, Any, int]]:
    """将表达式切分为 token 流：('num'|'str'|'op'|'name', value, pos)"""
    tokens: List[Tuple[str, Any, int]] = []
    for m in _TOKEN_RE.finditer(expr):
        kind = m.lastgroup
        if kind in ("WS", "COMMENT"):
            continue
        if kind == "BAD":
            raise SafeExprError(f"非法字符 {m.group()!r}", expr=expr, pos=m.start())
        if kind == "NUMBER":
            text = m.group()
            if "." not in text and "e" not in text.lower():
                tokens.append(("num", int(text), m.start()))
            else:
                tokens.append(("num", float(text), m.start()))
        elif kind == "STRING":
            tokens.append(("str", _decode_string_literal(m.group()), m.start()))
        elif kind == "OP":
            tokens.append(("op", m.group(), m.start()))
        else:  # NAME
            tokens.append(("name", m.group(), m.start()))
    tokens.append(("eof", "", len(expr)))
    return tokens


# ══════════════════════════════════════════════════
# 语法分析（递归下降，生成元组 AST）
# AST 节点：
#   ('num', v) ('str', v) ('const', v) ('var', name)
#   ('unary', op, node)
#   ('bin', op, left, right)        # 算术/逻辑
#   ('cmp', [op,...], [node,...])   # 链式比较，Python 语义
#   ('call', name, [node,...])
#   ('tuple', [node,...]) ('list', [node,...])
# ══════════════════════════════════════════════════
class _Parser:
    def __init__(self, tokens: List[Tuple[str, Any, int]], expr: str):
        self.tokens = tokens
        self.expr = expr
        self.i = 0

    # ── 基础工具 ──
    def peek(self) -> Tuple[str, Any, int]:
        return self.tokens[self.i]

    def advance(self) -> Tuple[str, Any, int]:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def error(self, msg: str, tok: Optional[Tuple[str, Any, int]] = None) -> None:
        tok = tok or self.peek()
        raise SafeExprError(msg, expr=self.expr, pos=tok[2])

    def is_op(self, text: str) -> bool:
        t = self.peek()
        return t[0] == "op" and t[1] == text

    def is_name(self, text: str) -> bool:
        t = self.peek()
        return t[0] == "name" and t[1] == text

    def expect_op(self, text: str) -> None:
        if not self.is_op(text):
            self.error(f"缺少 {text!r}")
        self.advance()

    # ── 优先级：or < and < not < 比较 < 加减 < 乘除 < 幂 < 一元 < 原子 ──
    def parse(self):
        node = self.parse_or()
        if self.peek()[0] != "eof":
            self.error(f"意外的 token {self.peek()[1]!r}")
        return node

    def parse_or(self):
        node = self.parse_and()
        while self.is_name("or"):
            self.advance()
            node = ("bin", "or", node, self.parse_and())
        return node

    def parse_and(self):
        node = self.parse_not()
        while self.is_name("and"):
            self.advance()
            node = ("bin", "and", node, self.parse_not())
        return node

    def parse_not(self):
        if self.is_name("not"):
            self.advance()
            return ("unary", "not", self.parse_not())
        return self.parse_cmp()

    def parse_cmp(self):
        first = self.parse_additive()
        ops: List[str] = []
        values: List[Any] = [first]
        while True:
            t = self.peek()
            if t[0] == "op" and t[1] in ("==", "!=", ">", ">=", "<", "<="):
                ops.append(t[1])
                self.advance()
            elif t[0] == "name" and t[1] == "in":
                ops.append("in")
                self.advance()
            elif (
                t[0] == "name"
                and t[1] == "not"
                and self.i + 1 < len(self.tokens)
                and self.tokens[self.i + 1][:2] == ("name", "in")
            ):
                ops.append("not_in")
                self.advance()
                self.advance()
            else:
                break
            values.append(self.parse_additive())
        if not ops:
            return first
        return ("cmp", ops, values)

    def parse_additive(self):
        node = self.parse_term()
        while True:
            t = self.peek()
            if t[0] == "op" and t[1] in ("+", "-"):
                self.advance()
                node = ("bin", t[1], node, self.parse_term())
            else:
                return node

    def parse_term(self):
        node = self.parse_factor()
        while True:
            t = self.peek()
            if t[0] == "op" and t[1] in ("*", "/", "//", "%"):
                self.advance()
                node = ("bin", t[1], node, self.parse_factor())
            else:
                return node

    def parse_factor(self):
        """一元 ± 绑定弱于 **（Python 语义：-2**2 == -(2**2)）"""
        t = self.peek()
        if t[0] == "op" and t[1] in ("+", "-"):
            self.advance()
            return ("unary", t[1], self.parse_factor())
        return self.parse_power()

    def parse_power(self):
        node = self.parse_primary()
        if self.is_op("**"):
            self.advance()
            node = ("bin", "**", node, self.parse_factor())  # 右结合
        return node

    def parse_primary(self):
        t = self.advance()
        kind, value, _pos = t

        if kind == "num":
            return ("num", value)
        if kind == "str":
            return ("str", value)
        if kind == "op":
            if value == "(":
                return self._parse_group_or_tuple()
            if value == "[":
                return self._parse_list()
            self.error(f"意外的运算符 {value!r}", t)
        if kind == "name":
            if value in _KEYWORD_CONSTANTS:
                return ("const", _KEYWORD_CONSTANTS[value])
            if value in ("and", "or", "not", "in"):
                self.error(f"关键字 {value!r} 在此位置不合法", t)
            if self.is_op("("):
                self.advance()
                return self._parse_call(value)
            return ("var", value)
        self.error("意外的表达式结束", t)

    def _parse_group_or_tuple(self):
        """括号：单表达式为分组；含逗号为元组。"""
        if self.is_op(")"):
            self.error("空括号 () 不受支持")
        node = self.parse_or()
        if self.is_op(","):
            items = [node]
            while self.is_op(","):
                self.advance()
                items.append(self.parse_or())
            self.expect_op(")")
            return ("tuple", items)
        self.expect_op(")")
        return node

    def _parse_list(self):
        if self.is_op("]"):
            self.advance()
            return ("list", [])
        items = [self.parse_or()]
        while self.is_op(","):
            self.advance()
            items.append(self.parse_or())
        self.expect_op("]")
        return ("list", items)

    def _parse_call(self, fname: str):
        args = []
        if not self.is_op(")"):
            args.append(self.parse_or())
            while self.is_op(","):
                self.advance()
                args.append(self.parse_or())
        self.expect_op(")")
        return ("call", fname, args)


# ══════════════════════════════════════════════════
# 求值（无副作用、无反射，仅白名单内置函数）
# ══════════════════════════════════════════════════
def _eval(node: Tuple, env: Dict[str, Any]) -> Any:
    tag = node[0]

    if tag == "num":
        return node[1]
    if tag == "str":
        return node[1]
    if tag == "const":
        return node[1]

    if tag == "var":
        name = node[1]
        if name not in env:
            raise SafeExprError(f"未知变量: {name!r}")
        return env[name]

    if tag == "unary":
        op, child = node[1], node[2]
        v = _eval(child, env)
        if op == "not":
            return not v
        if op == "-":
            return -v
        return +v  # '+'

    if tag == "and":  # 短路
        if not _eval(node[2], env):
            return False
        return bool(_eval(node[3], env))

    if tag == "or":  # 短路
        if _eval(node[2], env):
            return True
        return bool(_eval(node[3], env))

    if tag == "cmp":  # 链式比较（Python 语义）
        ops, children = node[1], node[2]
        values = [_eval(c, env) for c in children]
        for op, a, b in zip(ops, values, values[1:]):
            try:
                if op == "in":
                    ok = a in b
                elif op == "not_in":
                    ok = a not in b
                else:
                    ok = _CMP_OPS[op](a, b)
            except TypeError as e:
                raise SafeExprError(f"比较运算类型错误: {a!r} {op} {b!r} -> {e}")
            if not ok:
                return False
        return True

    if tag == "bin":
        op, left, right = node[1], node[2], node[3]
        lv = _eval(left, env)
        rv = _eval(right, env)
        fn = _BIN_OPS[op]
        try:
            return fn(lv, rv)
        except ZeroDivisionError:
            raise SafeExprError("除零错误")
        except (TypeError, ValueError, OverflowError) as e:
            raise SafeExprError(f"算术运算失败: {lv!r} {op} {rv!r} -> {e}")

    if tag == "call":
        fname, args = node[1], node[2]
        if fname not in _SAFE_FUNCS:
            raise SafeExprError(
                f"函数 {fname!r} 不在白名单内（仅允许 {sorted(_SAFE_FUNCS)}）"
            )
        vals = [_eval(a, env) for a in args]
        try:
            return _SAFE_FUNCS[fname](*vals)
        except (TypeError, ValueError, ZeroDivisionError) as e:
            raise SafeExprError(f"函数 {fname} 调用失败: {e}")

    if tag == "tuple":
        return tuple(_eval(c, env) for c in node[1])

    if tag == "list":
        return [_eval(c, env) for c in node[1]]

    raise SafeExprError(f"内部错误：未知 AST 节点 {tag!r}")


@lru_cache(maxsize=512)
def _parse_cached(expression: str):
    """按表达式文本缓存 AST（元组不可变，可安全共享）"""
    return _Parser(_tokenize(expression), expression).parse()


def safe_eval(expression: str, context: Optional[Dict[str, Any]] = None) -> Any:
    """
    安全求值表达式。

    Args:
        expression: 表达式字符串（支持上述语法子集）
        context:    变量名 -> 值的字典

    Returns:
        数值 / 布尔 / 字符串 / 元组 / 列表（取决于表达式）

    Raises:
        SafeExprError: 解析失败、变量缺失、类型错误、注入尝试等
    """
    if not isinstance(expression, str):
        raise SafeExprError(f"表达式必须为字符串，收到 {type(expression).__name__}")
    expr = expression.strip()
    if not expr:
        raise SafeExprError("表达式为空")
    ast_node = _parse_cached(expr)
    return _eval(ast_node, context or {})