"""Filters are data, not code.

A filter is an expression over the columns of a table: `gap >= 5 and relvol > 2`. It used to
be handed to pandas as a query string, and pandas would let such a string call anything it
could reach: `@os.system(...)`, or a column's own `.to_csv("somewhere")`. A filter typed by
a person at their own terminal is their own business. One written by a language model that
has just read a headline is not, and this CLI is built to be driven by both.

So a filter is never given to pandas to read. It is parsed once, with Python's own parser;
the tree is checked against a short list of what a filter is for (comparing, combining,
arithmetic, a few column methods); and then that same tree is worked out here, node by
node. Nothing else ever reads the text, so nothing can read it differently: there is no
second parser to disagree with the first.
"""
from __future__ import annotations

import ast
import operator
import re
import warnings

import numpy as np
import pandas as pd

# Methods that may be called on a column or on an expression over columns:
# name -> (how many values it takes by position, the arguments it takes by name).
# Both are spelled out for each method. pandas takes more than these, by position as well as
# by name: the third value of `.str.contains(pattern, case, flags)` is the regex flags, and
# the flag 128 makes Python's regex compiler print to stdout. So nothing is taken by
# position beyond what is listed, and no method here has `flags` among its names.
METHODS = {"abs": (0, ()), "isin": (1, ()), "between": (2, ("inclusive",)), "isna": (0, ()), "notna": (0, ()), "isnull": (0, ()),
           "notnull": (0, ()), "round": (1, ("decimals",)), "clip": (2, ("lower", "upper")), "fillna": (1, ("value",))}
# ... and on its text, after `.str`.
TEXT_METHODS = {"contains": (1, ("case", "na", "regex")), "startswith": (1, ("na",)), "endswith": (1, ("na",)),
                "fullmatch": (1, ("case", "na")), "match": (1, ("case", "na")), "lower": (0, ()), "upper": (0, ()), "strip": (1, ()),
                "len": (0, ())}
FUNCTIONS = frozenset({"abs"})                      # each takes exactly one value
MAX_LENGTH = 2000
# Text methods whose first argument is a pattern. A pattern can be written to take forever
# (a repeated group that is itself repeated); those are refused, and so are very long ones.
# This is a guard against the obvious cases, not a proof that a pattern is cheap.
PATTERN_METHODS = frozenset({"contains", "match", "fullmatch"})
# Text methods that answer yes or no. On a missing text they answer nothing; that is a no.
PREDICATES = frozenset({"contains", "startswith", "endswith", "match", "fullmatch"})
MAX_PATTERN = 200
# .round() takes this many decimals at most, either way. Far past it, NumPy's own rounding
# overflows a C integer: with -2**31 on Windows the process itself goes down.
MAX_DECIMALS = 20
_REPEATED_GROUP = re.compile(r"\([^()]*[*+][^()]*\)\s*(?:[*+]|\{\d*,)")

_BOOL = (ast.And, ast.Or)
_UNARY = (ast.Not, ast.USub, ast.UAdd, ast.Invert)
_ARITHMETIC = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
               ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}        # no powers: 9**9**9 never ends
_LOGIC = {ast.BitAnd: operator.and_, ast.BitOr: operator.or_}
_BINARY = (*_ARITHMETIC, *_LOGIC)
_ORDER = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}
# The names of the values a method takes by position, where it also takes them by name.
_BY_POSITION = {"round": ("decimals",), "clip": ("lower", "upper"), "fillna": ("value",)}
_COMPARE = (*_ORDER, ast.In, ast.NotIn)
NEEDS_TRUTH = "needs something that is true or false for each row (write streak > 0, not streak)"
MISSING_HINT = "missing values are tested with .isna() and .notna(), not by comparing with None"


class FilterError(ValueError):
    """A filter that is not one, with the reason in words the writer of it can act on."""


# ---------------------------------------------------------------- reading: what a filter may hold

def _plain(node: ast.AST) -> bool:
    """A value written out in the filter itself: a number, text, true/false, or a list of those."""
    if isinstance(node, ast.Constant):
        return node.value is None or isinstance(node.value, (str, int, float, bool))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return isinstance(node.operand, ast.Constant) and isinstance(node.operand.value, (int, float)) and not isinstance(node.operand.value, bool)
    if isinstance(node, (ast.List, ast.Tuple)):
        return all(_plain(e) for e in node.elts)
    return False


def _arguments(call: ast.Call, what: str, most: int, names: tuple[str, ...]) -> None:
    for arg in call.args:
        if not _plain(arg):
            raise FilterError(f"{what} takes plain values (numbers, text, lists of them), not expressions")
    if len(call.args) > most:
        named = f"; name the others ({', '.join(names)})" if names else ""
        raise FilterError(f"{what} takes {'no value' if not most else f'at most {most} value' + ('s' if most > 1 else '')} by position{named}")
    given = [kw.arg for kw in call.keywords]
    if len(set(given)) != len(given):                     # the parser allows it; which of the two would count?
        raise FilterError(f"{what} is given the same argument twice")
    for kw in call.keywords:
        if kw.arg not in names:
            raise FilterError(f"{what} does not take {kw.arg or 'unpacked arguments'} here")
        if not _plain(kw.value):
            raise FilterError(f"{what} takes plain values (numbers, text, lists of them), not expressions")


def _check(node: ast.AST, columns: frozenset[str]) -> None:
    if isinstance(node, ast.BoolOp) and isinstance(node.op, _BOOL):
        for value in node.values:
            _check(value, columns)
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, _UNARY):
        _check(node.operand, columns)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.MatMult):
        raise FilterError("'@' reaches outside the table; a filter may only name the table's columns")
    elif isinstance(node, ast.BinOp) and isinstance(node.op, _BINARY):
        for side in (node.left, node.right):
            if isinstance(side, ast.Constant) and isinstance(side.value, str):
                raise FilterError("arithmetic is for numbers; text can be compared, not added or multiplied")
            _check(side, columns)
    elif isinstance(node, ast.Compare) and any(isinstance(op, (ast.Is, ast.IsNot)) for op in node.ops):
        raise FilterError(f"'is' is not part of a filter: {MISSING_HINT.replace('by comparing with None', 'by `is None`')}")
    elif isinstance(node, ast.Compare) and all(isinstance(op, _COMPARE) for op in node.ops):
        _check(node.left, columns)
        for other in node.comparators:
            _check(other, columns)
    elif isinstance(node, ast.Name):
        if node.id not in columns:
            raise FilterError(f"unknown column '{node.id}'")
    elif _plain(node):
        return
    elif isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            if func.id not in FUNCTIONS:
                raise FilterError(f"'{func.id}(...)' is not something a filter can call; functions: {', '.join(sorted(FUNCTIONS))}")
            if len(node.args) != 1 or node.keywords:
                raise FilterError(f"'{func.id}()' takes one value and no named arguments")
            _check(node.args[0], columns)
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Attribute) and func.value.attr == "str":
            if func.attr not in TEXT_METHODS:
                raise FilterError(f"'.str.{func.attr}()' is not available in a filter; text methods: {', '.join(sorted(TEXT_METHODS))}")
            _check(func.value.value, columns)                 # whatever the text belongs to
            _arguments(node, f"'.str.{func.attr}()'", *TEXT_METHODS[func.attr])
            literal = any(kw.arg == "regex" and isinstance(kw.value, ast.Constant) and kw.value.value is False for kw in node.keywords)
            if (func.attr in PATTERN_METHODS and not literal and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                pattern = node.args[0].value
                if len(pattern) > MAX_PATTERN or _REPEATED_GROUP.search(pattern):
                    raise FilterError(f"the pattern in '.str.{func.attr}()' is too long or repeats a repeated group; "
                                      "use a simpler one, or regex=False for plain text")
        elif isinstance(func, ast.Attribute):
            if func.attr not in METHODS:
                raise FilterError(f"'.{func.attr}()' is not available in a filter; methods: {', '.join(sorted(METHODS))}")
            _check(func.value, columns)
            _arguments(node, f"'.{func.attr}()'", *METHODS[func.attr])
        else:
            raise FilterError("a filter can only call a column's own methods")
    elif isinstance(node, ast.Attribute):
        raise FilterError(f"'.{node.attr}' on its own is not available in a filter (only method calls such as .str.contains(...))")
    else:
        raise FilterError(f"{type(node).__name__.lower()} is not something a filter can hold: "
                          "compare columns with values, combine with and/or/not, and use the listed methods")


def _parse(expression: str, columns) -> ast.AST:
    """The checked tree of a filter over `columns`. FilterError when it is not one."""
    if not isinstance(expression, str) or not expression.strip():
        raise FilterError("the filter is empty")
    if len(expression) > MAX_LENGTH:
        raise FilterError(f"the filter is longer than {MAX_LENGTH} characters")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        # pandas' two extensions to the language are not Python, so they end here. (Inside
        # quoted text an @ or a backtick is just a character, and is fine.)
        if "@" in expression:
            raise FilterError("'@' reaches outside the table; a filter may only name the table's columns") from None
        if "`" in expression:
            raise FilterError("backticks are not needed: every column name is a plain word") from None
        raise FilterError("the filter is not a valid expression (example: gap >= 5 and relvol > 2)") from None
    try:
        _check(tree.body, frozenset(str(c) for c in columns))
    except RecursionError:
        raise FilterError("the filter is nested too deeply") from None
    return tree.body


def check(expression: str, columns) -> str:
    """The expression, if it holds only what a filter may hold over `columns`: FilterError
    otherwise. This is the reading. Whether its parts come to what they must (true or false
    for each row, numbers under arithmetic, a list after `in`) depends on what the columns
    hold, and is settled when the filter is worked out, by apply()."""
    _parse(expression, columns)
    return expression.strip()


# ---------------------------------------------------------------- working it out: the same tree, nothing else
#
# Every value has a kind: a number, text, true/false. Each operation takes the kinds it is
# for and refuses the rest, in words. Python and pandas would rather answer than refuse:
# `float_m != None` is true for every row, `optionable == True` for none, `streak == True`
# for the rows where streak is 1. A model writes all three, and gets rows back with nothing
# to tell it they are the wrong ones. Here it gets told.
#
# Two more places where pandas answers, and the answer is not to the question asked:
#
# - No text. A cell of a text column can be empty in two ways: blank ('') where the day's
#   file had the column and nothing in it, and a gap (NaN) where the day's file had no such
#   column. Whoever reads the rows sees neither. So in a filter the two are one thing, no
#   text: .isna() finds both, == '' finds both, and neither is above or below any text.
#
# - Whole numbers. pandas keeps them in 64 bits, and 3,100,000,000 x 3,100,000,000 does not
#   fit: it comes back as some other number, with nothing to say so. Arithmetic is done on
#   decimals, which grow instead of wrapping round.

NUMBER, TEXT, TRUTH, NOTHING, LIST, ANY, OTHER = "a number", "text", "true/false", "None", "a list", "anything", "something else"
_NUMBERS = (int, float, np.integer, np.floating)
_TRUTHS = (bool, np.bool_)
_ABOVE_BELOW = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)
_BIGGEST = 2 ** 63 - 1                              # the largest whole number pandas holds
LIKE_WITH_LIKE = "compare like with like (text in quotes, numbers as numbers, true/false with True or False)"
TOO_LARGE = "a number in the filter is too large to work with"


def _kind(value) -> str:
    """What sort of thing a value is. A column is judged by what it holds."""
    if value is None:
        return NOTHING
    if isinstance(value, _TRUTHS):
        return TRUTH
    if isinstance(value, _NUMBERS):
        return NUMBER
    if isinstance(value, str):
        return TEXT
    if isinstance(value, (list, tuple)):
        return LIST
    if isinstance(value, pd.Series):
        if pd.api.types.is_bool_dtype(value):
            return TRUTH
        if pd.api.types.is_numeric_dtype(value):
            return NUMBER
        if value.dtype == object:
            if not len(value):
                return ANY                              # no rows: nothing to judge by, and no row to get wrong
            known = value[value.notna()]
            if not len(known):
                return TEXT                             # rows, and nothing in them: a text column that was never filled
            if all(isinstance(v, _TRUTHS) for v in known):
                return TRUTH
            if all(isinstance(v, _NUMBERS) and not isinstance(v, _TRUTHS) for v in known):
                return NUMBER
            return TEXT if all(isinstance(v, str) for v in known) else OTHER
        if pd.api.types.is_string_dtype(value):
            return TEXT
    return OTHER


def _truth(value, what: str):
    """A yes or no for each row (or one for all of them): what and/or/not, & and | combine,
    and what a whole filter has to come to. A gap in a true/false column is a no. A column
    with nothing in it is not a column of no: `not` of it would be all yes."""
    if isinstance(value, _TRUTHS):
        return bool(value)
    if isinstance(value, pd.Series) and _kind(value) == TRUTH:
        return value.where(value.notna(), False).astype(bool)
    raise FilterError(f"{what} {NEEDS_TRUTH}")


def _number(value, what: str):
    """A number, or a column of numbers. Text times a number is Python's way of repeating
    the text, and a list times a number of repeating the list; neither is arithmetic, and
    either can be made as large as the caller likes."""
    if _kind(value) in (NUMBER, ANY):
        return value
    raise FilterError(f"{what} is for numbers and columns of numbers")


def _decimal(value, what: str):
    """A number to do arithmetic on. A column of whole numbers is worked on as decimals,
    which do not wrap round when a result passes 64 bits."""
    value = _number(value, what)
    if isinstance(value, pd.Series) and pd.api.types.is_integer_dtype(value):
        return value.astype("float64")
    return value


def _fits(value):
    """A plain whole number as pandas can take it: past 64 bits it goes on as a decimal."""
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > _BIGGEST:
        try:
            return float(value)
        except OverflowError:
            raise FilterError(TOO_LARGE) from None
    return value


def _blank(column: pd.Series) -> pd.Series:
    """Where a column of text has no text: a gap, or the empty text."""
    empty = column == ""
    return column.isna() | empty.where(empty.notna(), False).astype(bool)


def _same(left: str, right: str, what: str) -> None:
    """Two kinds that meet in a comparison must be one kind."""
    if NOTHING in (left, right):
        raise FilterError(MISSING_HINT)
    if LIST in (left, right):
        raise FilterError("a list is compared with 'in', not with == or <")
    if OTHER in (left, right):
        raise FilterError(f"{what} is given something a filter cannot compare")
    if ANY not in (left, right) and left != right:
        raise FilterError(f"{what} compares {left} with {right}: {LIKE_WITH_LIKE}")


def _alike(left, right, what: str) -> tuple:
    """Two things that are compared must be of one kind. Their kinds, when they are."""
    kinds = (_kind(left), _kind(right))
    _same(*kinds, what)
    return kinds


def _members(column, values, what: str) -> list:
    """The values a column is looked up in: a list, each of the column's own kind."""
    if not isinstance(column, pd.Series) or not isinstance(values, (list, tuple)):
        raise FilterError(f"{what} takes a column and a list of values: ticker in ['AAPL', 'MSFT']")
    kind = _kind(column)                                         # once: for a column of objects it means reading all of it
    for value in values:
        _same(kind, _kind(value), what)
    return list(values)


def _compare(op: ast.cmpop, left, right):
    if isinstance(op, (ast.In, ast.NotIn)):
        values = _members(left, right, "'in'")                   # refuses a left side that is not a column, before it is used as one
        found = left.isin(values)
        return ~found if isinstance(op, ast.NotIn) else found
    kinds = _alike(left, right, "a comparison")
    if isinstance(op, ast.NotEq):
        # Not equal is the other side of equal, however the column is kept. pandas' own !=
        # leaves a gap in its nullable types unanswered, and unanswered counts here as no.
        same = _truth(operator.eq(left, right), "a comparison")
        return (not same) if isinstance(same, bool) else ~same
    out = _ORDER[type(op)](left, right)       # a gap in a column of numbers is neither above nor below nor equal to anything
    if isinstance(op, _ABOVE_BELOW):          # ... and no text is neither above nor below any text ('' < 'A', to Python)
        for side, kind in zip((left, right), kinds):
            if kind == TEXT and isinstance(side, pd.Series):
                out = out & ~_blank(side)
    return out


def _receiver(value, what: str) -> pd.Series:
    if not isinstance(value, pd.Series):
        raise FilterError(f"{what} is a column's method; it cannot be called on a plain value")
    return value


def _named(kwargs: dict, name: str, kinds: tuple, what: str, words: str) -> None:
    """A named argument, where one was given, is of a kind it may be."""
    if name in kwargs and _kind(kwargs[name]) not in kinds:
        raise FilterError(f"{what}: {name} must be {words}")


def _method(column: pd.Series, name: str, args: list, kwargs: dict):
    """One of METHODS on a column, given what it is for."""
    what = f"'.{name}()'"
    if set(_BY_POSITION.get(name, ())[:len(args)]) & set(kwargs):
        raise FilterError(f"{what} is given the same argument twice, once by position and once by name")
    kind = _kind(column)
    if name in ("isna", "isnull", "notna", "notnull"):
        none = _blank(column) if kind == TEXT else column.isna()
        return none if name in ("isna", "isnull") else ~none
    if name == "isin":
        if len(args) != 1:
            raise FilterError("'.isin()' takes a list of values: ticker.isin(['AAPL', 'MSFT'])")
        return column.isin(_members(column, args[0], what))
    if name == "between":
        if len(args) != 2:
            raise FilterError("'.between()' takes a lower and an upper value: price.between(1, 20)")
        for bound in args:
            _same(kind, _kind(bound), what)
        if kwargs.get("inclusive", "both") not in ("both", "neither", "left", "right"):
            raise FilterError("'.between()': inclusive is 'both', 'neither', 'left' or 'right'")
        inside = column.between(*args, **kwargs)
        return inside & ~_blank(column) if kind == TEXT else inside
    if name == "fillna":
        if len(args) + len(kwargs) != 1:
            raise FilterError("'.fillna()' takes the one value to put where nothing is")
        value = (args or list(kwargs.values()))[0]
        _same(kind, _kind(value), what)
        return column.where(~(_blank(column) if kind == TEXT else column.isna()), value)
    column = _decimal(column, what)                              # abs, round, clip: for numbers
    for bound in [*args, *kwargs.values()]:
        if name == "round" and not (isinstance(bound, int) and not isinstance(bound, bool) and -MAX_DECIMALS <= bound <= MAX_DECIMALS):
            raise FilterError(f"'.round()' takes a whole number of decimals, from -{MAX_DECIMALS} to {MAX_DECIMALS}")
        if name == "clip" and bound is not None and _kind(bound) != NUMBER:
            raise FilterError("'.clip()' takes numbers (or None for no limit on that side)")
    return getattr(column, name)(*args, **kwargs)


def _text_method(column: pd.Series, name: str, args: list, kwargs: dict):
    """One of TEXT_METHODS on a column's text."""
    what = f"'.str.{name}()'"
    if _kind(column) not in (TEXT, ANY):
        raise FilterError(f"{what} is for text columns; this one holds {_kind(column)}")
    if name in PREDICATES:
        several = name in ("startswith", "endswith") and len(args) == 1 and isinstance(args[0], tuple) and args[0] and all(isinstance(a, str) for a in args[0])
        if not (len(args) == 1 and (isinstance(args[0], str) or several)):
            raise FilterError(f"{what} takes the text to look for, in quotes")
        _named(kwargs, "case", (TRUTH,), what, "True or False")
        _named(kwargs, "regex", (TRUTH,), what, "True or False")
        _named(kwargs, "na", (TRUTH, NOTHING), what, "True, False or None")
    elif args and not isinstance(args[0], str):
        raise FilterError(f"{what} takes text, in quotes")
    result = getattr(column.str, name)(*args, **kwargs)
    if name in PREDICATES:                                       # yes or no, whatever type pandas hands it back in
        # A row with no text is not put to the test: it passes when na=True asks for such
        # rows, and not otherwise. pandas would do that only where the cell is a gap, and a
        # column of text is read here with no gaps left; and a pattern that matches the
        # empty text ('^(?!.*offering)', '.*') would otherwise pass every row with nothing in it.
        result = result.where(result.notna(), False).astype(bool)
        result = result.where(~_blank(column), kwargs.get("na") is True)
    elif name == "len" and result.dtype == object:               # of a column with nothing in it, pandas hands back objects
        result = pd.to_numeric(result, errors="coerce")
    return result


def _column(df: pd.DataFrame, name: str) -> pd.Series:
    """A column as a filter works on it. The caller's table is not touched."""
    column = df[name]
    kind = _kind(column)
    if kind == NUMBER and column.dtype == object:
        return pd.to_numeric(column, errors="coerce")            # numbers kept as objects (with gaps): numbers all the same
    if kind == TEXT and column.isna().any():
        return column.where(column.notna(), "")                  # no text is no text, whichever way the cell came to be empty
    return column


def _value(node: ast.AST, df: pd.DataFrame):
    """What a checked node comes to: a column, a column of results, or a plain value."""
    if isinstance(node, ast.Constant):
        return _fits(node.value)
    if isinstance(node, ast.List):
        return [_value(e, df) for e in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_value(e, df) for e in node.elts)
    if isinstance(node, ast.Name):
        return _column(df, node.id)
    if isinstance(node, ast.UnaryOp):
        operand = _value(node.operand, df)
        if isinstance(node.op, (ast.Not, ast.Invert)):
            truth = _truth(operand, "'not'")
            return (not truth) if isinstance(truth, bool) else ~truth
        operand = _decimal(operand, "a minus or plus sign")
        return -operand if isinstance(node.op, ast.USub) else +operand
    if isinstance(node, ast.BoolOp):
        word = "and" if isinstance(node.op, ast.And) else "or"
        combine = operator.and_ if word == "and" else operator.or_
        parts = [_truth(_value(v, df), f"each side of '{word}'") for v in node.values]
        out = parts[0]
        for part in parts[1:]:
            out = combine(out, part)
        return out
    if isinstance(node, ast.BinOp):
        left, right = _value(node.left, df), _value(node.right, df)
        if type(node.op) in _LOGIC:
            sign = "&" if isinstance(node.op, ast.BitAnd) else "|"
            return _LOGIC[type(node.op)](_truth(left, f"each side of '{sign}'"), _truth(right, f"each side of '{sign}'"))
        left, right = _decimal(left, "arithmetic"), _decimal(right, "arithmetic")
        try:
            return _fits(_ARITHMETIC[type(node.op)](left, right))
        except ZeroDivisionError:                                # two plain numbers; a column divided by zero is just infinite
            raise FilterError("the filter divides by zero") from None
        except OverflowError:
            raise FilterError(TOO_LARGE) from None
    if isinstance(node, ast.Compare):
        left, out = _value(node.left, df), None
        for op, other in zip(node.ops, node.comparators):        # 1 < price < 5 is two comparisons, both to hold
            right = _value(other, df)
            piece = _truth(_compare(op, left, right), "a comparison")
            out = piece if out is None else operator.and_(out, piece)
            left = right
        return out
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):                           # abs(x): the one function, with its one value
            return abs(_decimal(_value(node.args[0], df), "abs()"))
        args = [_value(a, df) for a in node.args]
        kwargs = {kw.arg: _value(kw.value, df) for kw in node.keywords}
        if isinstance(func.value, ast.Attribute) and func.value.attr == "str":
            return _text_method(_receiver(_value(func.value.value, df), f"'.str.{func.attr}()'"), func.attr, args, kwargs)
        return _method(_receiver(_value(func.value, df), f"'.{func.attr}()'"), func.attr, args, kwargs)
    raise FilterError(f"{type(node).__name__.lower()} is not something a filter can hold")       # _check lets none through


def apply(df: pd.DataFrame, expression: str) -> pd.DataFrame:
    """The rows of `df` that pass the filter.

    The text is parsed and checked once, and that tree is what is worked out. pandas is
    never asked to read the text (no `query`, no `eval`), so it cannot read it differently.
    A filter has to come to true or false for each row.
    """
    tree = _parse(expression, df.columns)
    with warnings.catch_warnings():
        # pandas remarks that a pattern with a group in it could be used to pull the group out.
        # That is not what a filter is doing, and the remark would land in the caller's stderr.
        warnings.filterwarnings("ignore", message="This pattern is interpreted as a regular expression", category=UserWarning)
        keep = _truth(_value(tree, df), "a filter")
    if isinstance(keep, bool):
        return df.copy() if keep else df.iloc[0:0]
    return df[keep]
