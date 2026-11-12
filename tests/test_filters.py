"""A filter is read before it is run: only what a filter is for gets through."""
import os

import pandas as pd
import pytest

from finviz_cli import filters
from finviz_cli.filters import FilterError

TABLE = pd.DataFrame({"ticker": ["AAA", "BBB", "CCC"], "price": [1.0, 2.5, 30.0], "prev_close": [1.0, 2.0, 31.0], "gap": [0.0, 25.0, -3.2],
                      "streak": [0, 2, 1],
                      "relvol": [0.5, 8.0, 1.1], "float_m": [2.0, None, 900.0], "news": ["AAA Announces Reverse Stock Split", "", "CCC beats"],
                      "yesterday": ["mover, weak close", "quiet (under 50k shares)", "not covered"], "mover": [True, True, False]})


def passing(expression):
    return filters.apply(TABLE, expression).ticker.tolist()


@pytest.mark.parametrize("expression, tickers", [
    ("gap >= 5 and relvol > 2", ["BBB"]),
    ("abs(gap) <= 3 and price >= 1", ["AAA"]),
    ("gap.abs() > 3", ["BBB", "CCC"]),
    ("(price - prev_close) / prev_close * 100 > 10", ["BBB"]),
    ("yesterday == 'mover, weak close' and float_m < 20", ["AAA"]),
    ("news.str.contains('Reverse Stock Split')", ["AAA"]),
    ("news.str.lower().str.contains('split', regex=False)", ["AAA"]),
    ("news.str.contains('SPLIT', case=False, na=False)", ["AAA"]),
    ("news.str.len() == 0", ["BBB"]),
    ("ticker in ['AAA', 'CCC']", ["AAA", "CCC"]),
    ("ticker not in ('AAA', 'CCC')", ["BBB"]),
    ("ticker.isin(['BBB'])", ["BBB"]),
    ("price.between(2, 40)", ["BBB", "CCC"]),
    ("float_m.isna()", ["BBB"]),
    ("float_m.notna() and not mover", ["CCC"]),
    ("mover and price < 2", ["AAA"]),
    ("mover == True", ["AAA", "BBB"]),
    ("gap > -5 and gap < 0", ["CCC"]),
    ("(gap > 20) | (price > 20)", ["BBB", "CCC"]),
    ("~mover", ["CCC"]),
    ("price.round(0) == 2", ["BBB"]),
    ("ticker.str.startswith('A') or ticker.str.endswith('C')", ["AAA", "CCC"]),
    ("  gap >= 25  ", ["BBB"]),
    ("streak > 0", ["BBB", "CCC"]),
    ("streak > 0 and not mover", ["CCC"]),
    ("1 < price < 5", ["BBB"]),
    ("0 <= gap < 30 <= price * 20", ["BBB"]),
    ("True", ["AAA", "BBB", "CCC"]),
    ("False", []),
    ("1 == 1 and price > 2", ["BBB", "CCC"]),
    ("float_m > 1", ["AAA", "CCC"]),                                                    # a missing value is neither above nor below
    ("not float_m > 1", ["BBB"]),
    ("-gap > 3", ["CCC"]),
    ("+price > 2 and -(-streak) >= 1 and abs(-3) == 3", ["BBB", "CCC"]),
    ("streak * 2 + 1 > 2 and price // 1 % 2 == 0 and news.str.len() / 2 >= 0", ["BBB", "CCC"]),
    ("news.str.contains('@') or news.str.contains('`', regex=False)", []),              # inside quotes they are only characters
    ("ticker == 'A@B' or ticker == 'AAA'", ["AAA"]),
    ("news.str.contains('SPLIT', case=False)", ["AAA"]),
    ("news.str.contains('(a+)+$', regex=False)", []),                                # plain text: no pattern to be expensive
    ("ticker.str.contains('" + "A" * 3 + "', regex=False) and price.between(1, 2, inclusive='left')", ["AAA"]),
    ("price.clip(2, 10) == 2 and price.fillna(0) > 0", ["AAA"]),
    ("price.round(decimals=0) == 30", ["CCC"]),
    ("news.str.contains('(Reverse|Forward) Stock Split')", ["AAA"]),                 # an ordinary pattern is fine
    ("news.str.contains('Split+$')", ["AAA"]),
    ("ticker.str.match('[AB]{3}')", ["AAA", "BBB"]),
    ("price * 2 + gap / 10 - 1 > 5 and price % 2 < 1", ["BBB", "CCC"]),
])
def test_what_a_filter_is_for_gets_through(expression, tickers):
    assert passing(expression) == tickers


@pytest.mark.parametrize("expression, why", [
    ("@os.system('echo owned')", "'@' reaches outside the table"),
    ("price > @threshold", "'@' reaches outside the table"),
    ("price @ gap > 1", "'@' reaches outside the table"),
    ("price > 1 and @gap > 1", "'@' reaches outside the table"),
    ("ticker.to_csv('written_by_a_filter.csv') == None", "'.to_csv\\(\\)' is not available"),
    ("price.to_pickle('written_by_a_filter.pkl')", "'.to_pickle\\(\\)' is not available"),
    ("news.str.replace('a', 'b') == ''", "'.str.replace\\(\\)' is not available"),
    ("price.__class__ == 1", "'.__class__' on its own is not available"),
    ("price.__class__.__mro__[1].__subclasses__()", "is not available|can hold"),
    ("__import__('os').system('echo owned')", "'.system\\(\\)' is not available"),
    ("__import__('os')", "'__import__\\(...\\)' is not something a filter can call"),
    ("open('written_by_a_filter.txt', 'w')", "'open\\(...\\)' is not something a filter can call"),
    ("eval('1')", "'eval\\(...\\)' is not something a filter can call"),
    ("(lambda: 1)()", "can only call a column's own methods|can hold"),
    ("[x for x in price]", "listcomp is not something a filter can hold"),
    ("price if mover else gap", "ifexp is not something a filter can hold"),
    ("price[0] > 1", "subscript is not something a filter can hold"),
    ("price.values", "'.values' on its own is not available"),
    ("price.apply(print)", "'.apply\\(\\)' is not available"),
    ("news.str.contains(ticker)", "takes plain values"),
    ("ticker.isin(price)", "takes plain values"),
    ("news.str.contains('x', flags=2)", "does not take flags"),
    # ... nor by position: the third value of contains() is the flags, and 128 makes the regex compiler print.
    ("ticker.str.contains('A', True, 128)", "takes at most 1 value by position; name the others \\(case, na, regex\\)"),
    ("ticker.str.contains('A', True)", "takes at most 1 value by position"),
    ("ticker.str.match('A', True, 128)", "takes at most 1 value by position; name the others \\(case, na\\)"),
    ("ticker.str.fullmatch('AAA', True, 128)", "takes at most 1 value by position"),
    ("ticker.str.match('A', flags=128)", "does not take flags"),
    ("ticker.str.startswith('A', None, 1)", "takes at most 1 value by position"),
    ("ticker.str.lower('x')", "takes no value by position"),
    ("price.between(1, 2, 'left')", "takes at most 2 values by position; name the others \\(inclusive\\)"),
    ("price.isna(1)", "takes no value by position"),
    ("price.fillna(0, 'ffill') > 1", "takes at most 1 value by position"),
    ("price.fillna(method='ffill') > 1", "does not take method"),
    ("price.round(1, 2) > 1", "takes at most 1 value by position"),
    ("price.isin([1], x=1)", "does not take x"),
    # The same argument twice: the check would see one and the call would use the other.
    ("ticker.str.contains('(A+)+$', regex=False, regex=True)", "is given the same argument twice"),
    ("ticker.str.contains('A', case=True, case=False)", "is given the same argument twice"),
    ("price.between(1, 2, inclusive='left', inclusive='right')", "is given the same argument twice"),
    ("price.between(1, 2, **{})", "does not take unpacked arguments"),
    ("abs(price, gap=1) > 1", "'abs\\(\\)' takes one value and no named arguments"),
    ("abs(price, x=1) > 1", "'abs\\(\\)' takes one value and no named arguments"),
    ("abs(price, gap) > 1", "'abs\\(\\)' takes one value"),                             # numpy would write the answer into gap
    ("abs() > 1", "'abs\\(\\)' takes one value"),
    # A filter is true or false for each row. A number is not: pandas would have used it to pick rows by position.
    ("streak", "a filter needs something that is true or false for each row"),
    ("news.str.len()", "a filter needs something that is true or false"),
    ("price.round(0)", "a filter needs something that is true or false"),
    ("[0, 0]", "a filter needs something that is true or false"),
    ("0", "a filter needs something that is true or false"),
    ("'yes'", "a filter needs something that is true or false"),
    ("streak and price > 1", "each side of 'and' needs something that is true or false"),
    ("price > 1 or streak", "each side of 'or' needs something that is true or false"),
    ("not streak", "'not' needs something that is true or false"),
    ("price > 1 & gap > 0", "each side of '&' needs something that is true or false"),       # & binds tighter than >: write (a) & (b)
    ("'AAA' in ticker", "'in' takes a column and a list of values"),
    ("ticker in 'AAA'", "'in' takes a column and a list of values"),
    ("ticker == ['AAA']", "a list is compared with 'in'"),
    ("(5).abs() > 1", "is a column's method; it cannot be called on a plain value"),
    # Arithmetic is for numbers. Text times a number repeats the text, as often as the caller likes.
    ("ticker * 1000000000 == 'x'", "arithmetic is for numbers and columns of numbers"),
    ("news + news == ''", "arithmetic is for numbers"),
    ("[1] * 1000000000 == 1", "arithmetic is for numbers"),
    ("(1, 2) + (3,) == 1", "arithmetic is for numbers"),
    ("mover + mover > 1", "arithmetic is for numbers"),
    ("price + True > 1", "arithmetic is for numbers"),
    ("-ticker == 'x'", "a minus or plus sign is for numbers"),
    ("-mover", "a minus or plus sign is for numbers"),
    ("abs(ticker) == 'x'", "abs\\(\\) is for numbers"),
    ("abs([1, 2]) == 1", "abs\\(\\) is for numbers"),
    ("price > None + 1", "arithmetic is for numbers"),
    ("'abc'.str.lower() == 'abc'", "is a column's method"),
    ("volume > 100", "unknown column 'volume'"),
    ("price ** 2 > 4", "binop is not something a filter can hold"),                    # no powers
    ("price > 9 ** 9 ** 9", "binop is not something a filter can hold"),
    ("news == 'a' * 1000000000", "arithmetic is for numbers"),
    ("news + 'x' == 'x'", "arithmetic is for numbers"),
    ("news.str.contains('(a+)+$')", "repeats a repeated group"),
    ("news.str.match('(x*y*)*z')", "repeats a repeated group"),
    ("news.str.fullmatch('(ab+){2,}c')", "repeats a repeated group"),
    ("news.str.contains('" + "a" * 201 + "')", "is too long"),
    ("os.getcwd() != ''", "'.getcwd\\(\\)' is not available"),
    ("pd.read_csv('x')", "'.read_csv\\(\\)' is not available"),
    ("`price` > 1", "backticks are not needed"),
    ("price >", "not a valid expression"),
    ("price > 1; import os", "not a valid expression"),
    ("", "the filter is empty"),
    ("   ", "the filter is empty"),
    ("price == 1 or " * 400 + "price == 2", "longer than 2000 characters"),
    ("(" * 300 + "price" + ")" * 300 + " > 1", "nested too deeply|not a valid expression"),
    (f"price := 5", "not a valid expression|namedexpr"),
    ("f'{price}' == '1'", "joinedstr is not something a filter can hold"),
    ("{price} == 1", "set is not something a filter can hold"),
    ("price == b'x'", "constant is not something a filter can hold"),
])
def test_everything_else_is_refused_and_says_why(expression, why):
    with pytest.raises(FilterError, match=why):
        filters.apply(TABLE, expression)


def test_a_refused_filter_never_runs(tmp_path, monkeypatch):
    """The two things pandas would have done, had it been asked: call out of the table, and
    write a file through a column's own method."""
    monkeypatch.chdir(tmp_path)
    for expression in ("@os.getcwd() != ''", "ticker.to_csv('written_by_a_filter.csv') == None",
                       "price.to_pickle('written_by_a_filter.pkl') == None", "news.to_json('written_by_a_filter.json') == None"):
        with pytest.raises(FilterError):
            filters.apply(TABLE, expression)
    assert os.listdir(tmp_path) == []


def test_pandas_is_never_asked_to_read_the_text(monkeypatch):
    """The text is parsed once, here. pandas' own reading of a query string differs from
    Python's (it handles backticks and @ before Python sees them), and a difference between
    the reader that checks and the reader that runs is a way through. So there is one reader."""
    def not_called(*args, **kwargs):
        raise AssertionError("pandas was asked to parse a filter")

    monkeypatch.setattr(pd.DataFrame, "query", not_called)
    monkeypatch.setattr(pd.DataFrame, "eval", not_called)
    monkeypatch.setattr(pd, "eval", not_called)
    assert passing("gap >= 5 and relvol > 2 and news.str.len() == 0 and ticker in ['BBB']") == ["BBB"]
    assert passing("abs(gap) <= 3 and price.between(0, 2)") == ["AAA"]


def test_text_that_two_parsers_would_read_differently_is_read_once():
    """Three comparisons with odd text in them, to Python. pandas' pre-reading of backticks
    loses track of the quotes after the escaped backslash and would have turned the tail of
    the last text into live code (`price ** 2 > 4`, which is not even allowed)."""
    smuggled = r"""ticker == '\\' or ticker == '`"' or ticker == "`' or price ** 2 > 4 # " """.strip()
    assert passing(smuggled) == []                                    # no ticker is any of those three texts
    odd = TABLE.assign(ticker=["\\", "`\"", "`' or price ** 2 > 4 # "])
    assert filters.apply(odd, smuggled).price.tolist() == [1.0, 2.5, 30.0]             # ... and where one is, it matches as text


def test_true_or_false_with_gaps_counts_as_false_where_the_gap_is():
    """A text test on a missing value answers neither yes nor no; such a row does not pass.
    (Older pandas hands this back as a column of objects, newer as a nullable one.)"""
    gaps = pd.DataFrame({"ticker": ["AAA", "BBB", "CCC"], "note": pd.Series(["has split", None, float("nan")], dtype=object),
                         "flag": pd.Series([True, None, False], dtype=object), "sure": pd.array([True, None, False], dtype="boolean")})
    assert filters.apply(gaps, "note.str.contains('split')").ticker.tolist() == ["AAA"]
    assert filters.apply(gaps, "not note.str.contains('split', na=False)").ticker.tolist() == ["BBB", "CCC"]
    assert filters.apply(gaps, "flag").ticker.tolist() == ["AAA"] and filters.apply(gaps, "sure").ticker.tolist() == ["AAA"]
    assert filters.apply(gaps, "not sure").ticker.tolist() == ["BBB", "CCC"]
    assert filters.apply(gaps.iloc[0:0], "sure and note.str.contains('x')").ticker.tolist() == []     # and a table with no rows


def test_a_column_with_nothing_in_it_is_not_a_column_of_no():
    """A text column that was never filled (older pandas keeps it as objects, all missing) says
    neither yes nor no. Taken for "all no", `not` of it would be "all yes" and return every row."""
    days = pd.DataFrame({"ticker": ["AAA", "BBB", "CCC"], "gap": [1.0, 9.0, 12.0],
                         "optionable": pd.Series([None, float("nan"), None], dtype=object),
                         "shortable": pd.Series(["Yes", None, "No"], dtype=object)})
    for expression in ("optionable", "not optionable", "optionable and gap >= 5", "not optionable and gap >= 5", "~optionable",
                       "optionable | (gap > 5)", "shortable", "not shortable", "ticker", "not ticker"):
        with pytest.raises(FilterError, match="needs something that is true or false for each row"):
            filters.apply(days, expression)
    assert filters.apply(days, "shortable == 'Yes' and gap >= 1").ticker.tolist() == ["AAA"]              # the way to ask
    assert filters.apply(days, "optionable.isna() and gap >= 5").ticker.tolist() == ["BBB", "CCC"]
    assert filters.apply(days, "optionable.str.contains('Yes')").ticker.tolist() == []                   # a test on missing text: no
    assert filters.apply(days, "not optionable.str.startswith('Y')").ticker.tolist() == ["AAA", "BBB", "CCC"]
    # With no rows at all nothing can be known of such a column either.
    empty = days.iloc[0:0]
    for expression in ("optionable", "not optionable", "ticker", "not ticker", "shortable.str.lower()"):
        with pytest.raises(FilterError, match="needs something that is true or false for each row"):
            filters.apply(empty, expression)
    assert filters.apply(empty, "gap >= 5 and shortable == 'Yes' and ticker.str.startswith('A')").ticker.tolist() == []


def test_the_rows_that_come_back_are_the_callers_own():
    everything = filters.apply(TABLE, "True")
    assert everything is not TABLE and everything.equals(TABLE)
    everything.loc[0, "price"] = -1.0
    assert TABLE.price.tolist() == [1.0, 2.5, 30.0]


def test_a_filter_leaves_the_table_as_it_was():
    before = TABLE.copy(deep=True)
    for expression in ("abs(price) > 1", "price.abs() > 1", "price.clip(2, 10) > 1", "price.fillna(0) > 1", "price.round(1) > 1",
                       "float_m.fillna(value=0) > 1", "news.str.lower().str.contains('split')", "gap.between(0, 30)", "streak > 0",
                       "price * 2 - gap / 3 > 0", "-price < 0", "ticker.isin(['AAA'])", "not mover", "~mover", "mover & (price > 1)"):
        filters.apply(TABLE, expression)
    with pytest.raises(FilterError):
        filters.apply(TABLE, "abs(price, gap) > 1")                   # the call that used to overwrite gap
    pd.testing.assert_frame_equal(TABLE, before)


def test_names_from_outside_cannot_be_reached_even_by_an_allowed_shape():
    """`os` is not a column, so it is refused by name. Were it one, the query still runs with
    nothing but the table in scope."""
    with pytest.raises(FilterError, match="unknown column 'os'"):
        filters.apply(TABLE, "os == 1")
    shadow = TABLE.assign(os=[1, 2, 3], filters=[0, 0, 1])
    assert filters.apply(shadow, "os >= 2 and filters == 0").ticker.tolist() == ["BBB"]       # a column of that name is just a column


def test_check_returns_the_expression_and_is_typed():
    assert filters.check("  gap > 1 ", TABLE.columns) == "gap > 1"
    for not_text in (None, 5, ["gap > 1"]):
        with pytest.raises(FilterError, match="the filter is empty"):
            filters.check(not_text, TABLE.columns)
    assert issubclass(FilterError, ValueError)


def test_nothing_a_filter_may_call_writes_to_stdout(capsys):
    """Python's regex compiler prints when it is given the debug flag. No allowed call can pass it."""
    for expression in ("ticker.str.contains('A', True, 128)", "ticker.str.match('A', True, 128)", "ticker.str.fullmatch('AAA', True, 128)"):
        with pytest.raises(FilterError):
            filters.apply(TABLE, expression)
    for expression in ("ticker.str.contains('A')", "ticker.str.match('[AB]+')", "ticker.str.fullmatch('AAA', case=False)",
                       "news.str.contains('split', case=False, na=False, regex=True)"):
        filters.apply(TABLE, expression)
    assert capsys.readouterr() == ("", "")


def test_the_guide_lists_exactly_the_methods_a_filter_may_call():
    """docs/agent-guide.md says "these are all the methods". Keep it so."""
    import pathlib
    import re
    guide = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "agent-guide.md").read_text("utf-8")
    start = guide.index("These are all the methods it may call:")
    listed = guide[start:guide.index("Nothing else: a filter is data", start)]
    column, text = listed.split("- on a column's text")[0], listed.split("- on a column's text")[1].split("- and the function")[0]
    assert set(re.findall(r"`(\w+)`", column)) == set(filters.METHODS)
    assert set(re.findall(r"`(\w+)`", text)) - {"str"} == set(filters.TEXT_METHODS)


# ---------------------------------------------------------------- like with like

KINDS = pd.DataFrame({"ticker": ["AAA", "BBB", "CCC"], "date": ["2026-11-09", "2026-11-10", "2026-11-11"], "price": [1.0, 2.5, 30.0],
                      "streak": [0, 2, 1], "float_m": [2.0, None, 900.0], "mover": [True, True, False],
                      "optionable": ["Yes", "No", ""], "count": pd.Series([1, None, 3], dtype=object)})


@pytest.mark.parametrize("expression, why", [
    # Python and pandas answer all of these. None of the answers is what was meant.
    ("float_m == None", "missing values are tested with .isna\\(\\) and .notna\\(\\)"),          # no row, even where it is missing
    ("float_m != None", "missing values are tested with .isna"),                                       # every row
    ("None != float_m", "missing values are tested with .isna"),
    ("optionable == None", "missing values are tested with .isna"),
    ("float_m in [None]", "missing values are tested with .isna"),
    ("float_m.isin([1, None])", "missing values are tested with .isna"),
    ("float_m.fillna(None) > 1", "missing values are tested with .isna"),
    ("float_m is None", "'is' is not part of a filter: missing values are tested with .isna"),
    ("float_m is not None and price > 1", "'is' is not part of a filter"),
    ("optionable == True", "a comparison compares text with true/false: compare like with like"),     # no row
    ("optionable != True", "compares text with true/false"),                                           # every row
    ("streak == True", "compares a number with true/false"),                                           # the rows where streak is 1
    ("mover == 1", "compares true/false with a number"),
    ("mover == 'yes'", "compares true/false with text"),
    ("price == 'high'", "compares a number with text"),
    ("price > '5'", "compares a number with text"),
    ("ticker > 5", "compares text with a number"),
    ("1 < ticker < 5", "compares a number with text"),
    ("ticker in [1, 2]", "'in' compares text with a number"),
    ("price in ['1.0']", "'in' compares a number with text"),
    ("streak in [True]", "'in' compares a number with true/false"),
    ("ticker.isin(['AAA', 5])", "'.isin\\(\\)' compares text with a number"),
    ("price.isin(5)", "'.isin\\(\\)' takes a column and a list of values"),
    ("price.between('a', 'b')", "'.between\\(\\)' compares a number with text"),
    ("ticker.between(1, 5)", "'.between\\(\\)' compares text with a number"),
    ("price.between(1)", "'.between\\(\\)' takes a lower and an upper value"),
    ("price.between(1, 2, inclusive='sideways')", "inclusive is 'both', 'neither', 'left' or 'right'"),
    ("price.fillna('x') > 1", "'.fillna\\(\\)' compares a number with text"),
    ("optionable.fillna(0) == ''", "'.fillna\\(\\)' compares text with a number"),
    ("price.fillna() > 1", "'.fillna\\(\\)' takes the one value"),
    ("price.round(1, decimals=2) > 1", "'.round\\(\\)' is given the same argument twice, once by position and once by name"),
    ("price.clip(1, 5, upper=3) > 1", "'.clip\\(\\)' is given the same argument twice"),
    ("price.clip(1, lower=3) > 1", "'.clip\\(\\)' is given the same argument twice"),
    ("price.fillna(1, value=2) > 1", "'.fillna\\(\\)' is given the same argument twice"),
    ("price.round('a') > 1", "'.round\\(\\)' takes a whole number of decimals"),
    ("price.round(1.5) > 1", "'.round\\(\\)' takes a whole number of decimals"),
    ("price.round(True) > 1", "'.round\\(\\)' takes a whole number of decimals"),
    ("ticker.round(1) == 'A'", "'.round\\(\\)' is for numbers"),
    ("ticker.abs() == 'A'", "'.abs\\(\\)' is for numbers"),
    ("mover.abs() > 0", "'.abs\\(\\)' is for numbers"),
    ("price.clip('a', 5) > 1", "'.clip\\(\\)' takes numbers"),
    ("ticker.clip(1, 5) == 'A'", "'.clip\\(\\)' is for numbers"),
    # Text methods are for text, and take text.
    ("price.str.contains('1')", "'.str.contains\\(\\)' is for text columns; this one holds a number"),
    ("mover.str.lower() == 'true'", "'.str.lower\\(\\)' is for text columns; this one holds true/false"),
    ("ticker.str.contains(123, regex=False)", "'.str.contains\\(\\)' takes the text to look for, in quotes"),
    ("ticker.str.contains(['A', 'B'])", "takes the text to look for"),
    ("ticker.str.match(None)", "takes the text to look for"),
    ("ticker.str.contains(('A', 'B'))", "takes the text to look for"),                               # several at once is for startswith and endswith
    ("ticker.str.startswith(('A', 5))", "takes the text to look for"),
    ("ticker.str.startswith(())", "takes the text to look for"),
    ("ticker.str.match('Yes', na='False')", "na must be True, False or None"),                       # 'False' is text, and text is true
    ("ticker.str.fullmatch('Yes', na='no')", "na must be True, False or None"),
    ("ticker.str.contains('A', case='no')", "case must be True or False"),
    ("ticker.str.contains('A', regex=1)", "regex must be True or False"),
    ("ticker.str.strip(5) == 'A'", "'.str.strip\\(\\)' takes text, in quotes"),
])
def test_what_would_be_answered_wrongly_is_refused_instead(expression, why):
    with pytest.raises(FilterError, match=why):
        filters.apply(KINDS, expression)


@pytest.mark.parametrize("expression, tickers", [
    ("float_m.isna()", ["BBB"]),
    ("float_m.notna() and float_m < 10", ["AAA"]),
    ("mover == True", ["AAA", "BBB"]),
    ("mover != True", ["CCC"]),
    ("mover in [False]", ["CCC"]),
    ("optionable == 'Yes'", ["AAA"]),
    ("optionable != 'Yes'", ["BBB", "CCC"]),
    ("optionable in ['Yes', 'No']", ["AAA", "BBB"]),
    ("optionable.isin(['']) or ticker.isin([])", ["CCC"]),
    ("streak in [1, 2] and price > 2.0", ["BBB", "CCC"]),
    ("price == 30 and streak >= 1.0", ["CCC"]),                                       # whole and decimal numbers are one kind
    ("date >= '2026-11-10'", ["BBB", "CCC"]),                                         # text in order: dates written year first
    ("'2026-11-09' < date <= '2026-11-11'", ["BBB", "CCC"]),
    ("date.between('2026-11-09', '2026-11-10')", ["AAA", "BBB"]),
    ("price.between(1, 2.5, inclusive='left')", ["AAA"]),
    ("float_m.fillna(0) < 5", ["AAA", "BBB"]),
    ("optionable.fillna('') == ''", ["CCC"]),
    ("price.clip(None, 5) == 5 or price.clip(lower=2) == 2", ["AAA", "CCC"]),
    ("price.clip(2, upper=5) == 2.5", ["BBB"]),
    ("price.round() == 2 or price.round(decimals=1) == 30", ["BBB", "CCC"]),
    ("ticker.str.startswith(('A', 'C'))", ["AAA", "CCC"]),
    ("ticker.str.endswith('B', na=None) or ticker.str.match('C', na=False, case=True)", ["BBB", "CCC"]),
    ("ticker.str.strip('A') == '' or ticker.str.strip() == 'BBB'", ["AAA", "BBB"]),
    ("count > 2", ["CCC"]),                                                           # numbers kept as objects, with a gap
    ("count.isna() or count == 1", ["AAA", "BBB"]),
    ("count * 2 >= 2 and abs(count) < 3", ["AAA"]),
])
def test_like_with_like_is_answered(expression, tickers):
    assert filters.apply(KINDS, expression).ticker.tolist() == tickers


def test_a_table_with_no_rows_and_no_types_takes_any_comparison():
    """Nothing is known of an empty column of objects, and no row can be got wrong. A bare
    column is still not a yes or no."""
    empty = pd.DataFrame(columns=["ticker", "price", "mover"])
    for expression in ("price > 1 and ticker == 'A'", "price * 2 > 1", "ticker.str.contains('A')", "price in [1, 2]",
                       "price.between(1, 2)", "ticker.isin(['A'])", "abs(price) > 1", "mover == True", "price.isna()"):
        assert len(filters.apply(empty, expression)) == 0, expression
    for expression in ("price", "not ticker", "mover"):
        with pytest.raises(FilterError, match="needs something that is true or false for each row"):
            filters.apply(empty, expression)


@pytest.mark.parametrize("gap", [None, float("nan")])
@pytest.mark.parametrize("expression, tickers", [
    ("exchange > 'NASD'", ["CCC"]),
    ("exchange >= 'NASD'", ["AAA", "CCC"]),
    ("exchange < 'NYSE'", ["AAA"]),
    ("'NASD' <= exchange <= 'NYSE'", ["AAA", "CCC"]),
    ("exchange < ticker", []),
    ("ticker < exchange", ["AAA", "CCC"]),
    ("not exchange > 'A'", ["BBB"]),                                 # the gap is not above 'A', and that is all that is known of it
    ("exchange == 'NYSE' or exchange != 'NYSE'", ["AAA", "BBB", "CCC"]),      # as with numbers: a gap is not equal to anything
    ("exchange.between('NASD', 'NYSE')", ["AAA", "CCC"]),
])
def test_text_with_gaps_in_it_is_put_in_order_the_same_way_however_it_is_kept(expression, tickers, gap):
    """A day brought in from elsewhere has no exchange column, so in a table of several days
    that column has gaps. pandas 2 keeps text as objects and pandas 3 as text of its own;
    either way a gap is not above, below or equal to anything, as with numbers."""
    kept_as_objects = pd.DataFrame({"ticker": ["AAA", "BBB", "CCC"], "exchange": pd.Series(["NASD", gap, "NYSE"], dtype=object)})
    assert filters.apply(kept_as_objects, expression).ticker.tolist() == tickers
    as_pandas_reads_it = pd.DataFrame({"ticker": ["AAA", "BBB", "CCC"], "exchange": ["NASD", gap, "NYSE"]})
    assert filters.apply(as_pandas_reads_it, expression).ticker.tolist() == tickers


# ---------------------------------------------------------------- no text is one thing, however the cell came to be empty

NAN = float("nan")
NO_TEXT = {      # A has 'Yes', B has 'No', C is blank, D is a gap: each way pandas keeps a column of text
    "objects, None": lambda values: pd.Series(values, dtype=object),
    "objects, NaN": lambda values: pd.Series([NAN if v is None else v for v in values], dtype=object),
    "pandas' own text": lambda values: pd.Series(values),
    "pandas' nullable text": lambda values: pd.Series(values, dtype="string"),
}


def no_text(kept):
    make = NO_TEXT[kept]
    return pd.DataFrame({"ticker": list("ABCD"), "optionable": make(["Yes", "No", "", None]),
                         "when": make(["2026-11-09", "2026-11-10", "", None])})


@pytest.mark.parametrize("kept", list(NO_TEXT))
@pytest.mark.parametrize("expression, tickers", [
    ("optionable.isna()", "CD"), ("optionable.isnull()", "CD"), ("optionable.notna()", "AB"), ("optionable.notnull()", "AB"),
    ("optionable == ''", "CD"), ("optionable != ''", "AB"),
    ("optionable != 'Yes'", "BCD"),                                           # no text is not 'Yes'
    ("optionable.notna() and optionable != 'Yes'", "B"),                      # ... and this leaves it out, as the guide says
    ("optionable in ['', 'No']", "BCD"), ("optionable.isin([''])", "CD"), ("optionable not in ['Yes']", "BCD"),
    ("optionable < 'Z'", "AB"), ("optionable <= 'Yes'", "AB"),                # no text is not below any text ('' < 'Z', to Python)
    ("optionable > ''", "AB"), ("optionable >= ''", "AB"),
    ("not optionable < 'Z'", "CD"),
    ("when < '2026-11-10'", "A"), ("'2026-11-01' < when", "AB"), ("'2026-11-01' < when <= '2026-11-09'", "A"),
    ("when.between('2026-01-01', '2026-12-31')", "AB"), ("when.between('', '2026-12-31')", "AB"),
    ("optionable < when", ""), ("when < optionable", "AB"),
    ("optionable.fillna('none') == 'none'", "CD"), ("optionable.fillna('none') != 'none'", "AB"),
    ("optionable.fillna(value='none').isna()", ""),
    ("optionable.str.len() == 0", "CD"), ("optionable.str.len() > 0", "AB"),
    ("optionable.str.contains('e')", "A"), ("not optionable.str.contains('e')", "BCD"),
    ("optionable.str.lower() == ''", "CD"), ("optionable.str.upper().isna()", "CD"), ("optionable.str.strip('Yes').isna()", "ACD"),
])
def test_a_blank_and_a_gap_are_both_no_text(expression, tickers, kept):
    """A whole-market day has '' where a name has no headline; a day whose file had no such
    column has a gap there. The rows a program gets back show neither. A filter that told
    them apart would answer a question nobody can see the grounds for."""
    table = no_text(kept)
    before = table.copy()
    assert "".join(filters.apply(table, expression).ticker) == tickers
    pd.testing.assert_frame_equal(table, before)                              # and the caller's cells are as they were, gaps and all


def test_a_text_column_with_nothing_in_it_has_no_text_in_any_row():
    """All gaps, and kept as objects: pandas hands the lengths back as objects too."""
    table = pd.DataFrame({"ticker": list("AB"), "exchange": pd.Series([None, NAN], dtype=object)})
    for expression, tickers in (("exchange.isna()", "AB"), ("exchange == ''", "AB"), ("exchange.notna()", ""), ("exchange.str.len() == 0", "AB"),
                                ("exchange.str.len() > 0", ""), ("exchange != 'NYSE'", "AB"), ("exchange < 'Z'", "")):
        assert "".join(filters.apply(table, expression).ticker) == tickers, expression


# ---------------------------------------------------------------- != is the other side of ==

@pytest.mark.parametrize("column, expression, tickers", [
    (pd.array([1, None, 3], dtype="Int64"), "x != 1", "BC"),
    (pd.array([1.0, None, 3.0], dtype="Float64"), "x != 1", "BC"),
    (pd.array([True, None, False], dtype="boolean"), "x != True", "BC"),
    (pd.array(["Yes", None, "No"], dtype="string"), "x != 'Yes'", "BC"),
    (pd.array([1, None, 3], dtype="Int64"), "x == 1 or x != 1", "ABC"),
    (pd.array([1, None, 3], dtype="Int64"), "not x == 1", "BC"),
    (pd.array([1, None, 3], dtype="Int64"), "x not in [1]", "BC"),
    (pd.array([1, None, 3], dtype="Int64"), "x == 1", "A"),
    ([1.0, None, 3.0], "x != 1", "BC"),
    ([1.0, None, 3.0], "1 != x", "BC"),
    (pd.Series([True, None, False], dtype=object), "x != True", "BC"),
])
def test_not_equal_is_the_other_side_of_equal_however_the_column_is_kept(column, expression, tickers):
    """pandas' nullable types leave `gap != 1` unanswered, and unanswered would be a no:
    then `x == 1 or x != 1` would lose the row with the gap."""
    table = pd.DataFrame({"ticker": list("ABC"), "x": column})
    assert "".join(filters.apply(table, expression).ticker) == tickers


# ---------------------------------------------------------------- whole numbers do not wrap round

@pytest.mark.parametrize("kept", ["int64", "Int64", "uint64", "float64"])
@pytest.mark.parametrize("expression, tickers", [
    ("vol * vol / (avgvol * avgvol) > 4", ["BIG", "SMALL"]),                  # 3.1 billion squared does not fit in 64 bits
    ("(vol / avgvol) * (vol / avgvol) > 4", ["BIG", "SMALL"]),
    ("vol * vol > 0", ["BIG", "SMALL"]),
    ("vol * vol * vol > vol", ["BIG", "SMALL"]),
    ("vol * 10000000000 > 0", ["BIG", "SMALL"]),
    ("-vol * vol < 0", ["BIG", "SMALL"]),
    ("abs(vol * -vol) > 9000000000000000000", ["BIG"]),
    ("vol.abs() * vol.abs() > 9000000000000000000", ["BIG"]),
    ("vol.clip(0, 5000000) * 4000000000000 > 12000000000000000000", ["BIG"]),
    ("vol // 1000 == 3100000", ["BIG"]),
    ("vol % 1000000000 == 100000000", ["BIG"]),
    ("vol + 1 == 3000001", ["SMALL"]),                                        # and small whole numbers are still exact
    ("vol > 99999999999999999999999", []),                                    # a number past 64 bits is compared as a decimal
    ("vol < 99999999999999999999999", ["BIG", "SMALL"]),
    ("vol * 99999999999999999999999 > 1", ["BIG", "SMALL"]),
    ("vol in [3000000, 99999999999999999999999]", ["SMALL"]),
    ("9223372036854775807 * 2 > vol", ["BIG", "SMALL"]),
])
def test_arithmetic_on_whole_numbers_does_not_wrap_round(expression, tickers, kept):
    table = pd.DataFrame({"ticker": ["BIG", "SMALL"], "vol": pd.array([3_100_000_000, 3_000_000], dtype=kept),
                          "avgvol": pd.array([1_000_000_000, 1_000_000], dtype=kept)})
    before = table.copy()
    assert filters.apply(table, expression).ticker.tolist() == tickers
    pd.testing.assert_frame_equal(table, before)


@pytest.mark.parametrize("expression, why", [
    ("price > 1 / 0", "the filter divides by zero"),
    ("price > 5 // 0", "the filter divides by zero"),
    ("price > 5 % 0", "the filter divides by zero"),
    ("price < " + "9" * 400, "a number in the filter is too large to work with"),
    ("price < 2 * " + "9" * 400, "a number in the filter is too large to work with"),
    ("price.round(-2147483648) == 0", "'.round\\(\\)' takes a whole number of decimals, from -20 to 20"),     # this one took the process down
    ("price.round(2147483648) > 0", "from -20 to 20"), ("price.round(309) > 0", "from -20 to 20"),
    ("price.round(21) > 0", "from -20 to 20"), ("price.round(decimals=-21) > 0", "from -20 to 20"),
    ("price.clip(lower=price) > 1", "'.clip\\(\\)' takes plain values \\(numbers, text, lists of them\\), not expressions"),
    ("price.clip(lowest=1) > 1", "'.clip\\(\\)' does not take lowest here"),
    ("float_m.fillna(value=price) > 1", "'.fillna\\(\\)' takes plain values"),
])
def test_what_cannot_be_worked_out_says_so(expression, why):
    with pytest.raises(FilterError, match=why):
        filters.apply(KINDS, expression)


def test_rounding_takes_up_to_twenty_decimals_either_way():
    assert filters.apply(KINDS, "price.round(20) > 2 and price.round(-20) == 0 and streak.round(decimals=-1) == 0").ticker.tolist() == ["BBB", "CCC"]


def test_a_column_divided_by_zero_is_not_an_error():
    """Only two plain numbers divide by zero. A column over zero is infinite, or nothing at all, and passes no comparison it should not."""
    table = pd.DataFrame({"ticker": list("AB"), "vol": [5, 0], "zero": [0, 0]})
    assert filters.apply(table, "vol / zero > 1000000").ticker.tolist() == ["A"]
    assert filters.apply(table, "vol // zero == 1 or vol % zero == 1").empty


@pytest.mark.parametrize("kept", list(NO_TEXT))
@pytest.mark.parametrize("expression, tickers", [
    ("optionable.str.contains('e', na=True)", "ACD"),                         # A matches; C and D have no text, and were asked for
    ("optionable.str.contains('e', na=False)", "A"), ("optionable.str.contains('e', na=None)", "A"), ("optionable.str.contains('e')", "A"),
    ("optionable.str.contains('E', case=False, na=True, regex=False)", "ACD"),
    ("optionable.str.startswith('Y', na=True)", "ACD"), ("optionable.str.startswith(('Y', 'N'), na=True)", "ABCD"),
    ("optionable.str.endswith('o', na=True)", "BCD"), ("optionable.str.endswith('o')", "B"),
    ("optionable.str.match('Y', na=True)", "ACD"), ("optionable.str.fullmatch('No', na=True)", "BCD"), ("optionable.str.fullmatch('No')", "B"),
    ("optionable.isna() or optionable.str.contains('e')", "ACD"),             # the same question, asked the long way
    ("not optionable.str.contains('e', na=True)", "B"),
    ("optionable.str.lower().str.contains('y', na=True)", "ACD"),
    # A pattern that matches the empty text. No text is still not a match, unless asked for.
    ("optionable.str.match('^(?!.*Yes)')", "B"), ("optionable.str.match('^(?!.*Yes)', na=False)", "B"),
    ("optionable.str.match('^(?!.*Yes)', na=None)", "B"), ("optionable.str.match('^(?!.*Yes)', na=True)", "BCD"),
    ("optionable.str.fullmatch('.*')", "AB"), ("optionable.str.fullmatch('.*', na=False)", "AB"), ("optionable.str.fullmatch('.*', na=True)", "ABCD"),
    ("optionable.str.fullmatch('')", ""), ("optionable.str.fullmatch('', na=True)", "CD"),
    ("optionable.str.contains('')", "AB"), ("optionable.str.contains('', regex=False, na=False)", "AB"), ("optionable.str.contains('', na=True)", "ABCD"),
    ("optionable.str.startswith('')", "AB"), ("optionable.str.endswith('', na=True)", "ABCD"), ("optionable.str.startswith(('', 'Y'), na=None)", "AB"),
    ("optionable.str.lower().str.fullmatch('.*', na=False)", "AB"), ("optionable.str.upper().str.match('', na=True)", "ABCD"),
    ("optionable.str.strip('Yes').str.fullmatch('.*')", "B"),                 # stripped to nothing: no text
    ("optionable.notna() and not optionable.str.contains('Yes')", "B"),       # known, and without it
    ("not optionable.str.contains('Yes')", "BCD"),                            # not a match: that includes no text
    ("optionable.str.len() < 3 and optionable.notna()", "B"), ("optionable.str.len() < 3", "BCD"), ("optionable.str.len().isna()", ""),
])
def test_na_true_on_a_text_test_also_passes_the_rows_with_no_text(expression, tickers, kept):
    """pandas' own na= fills in only where the cell is a gap. A text column is read with no
    gaps left, so the request is honoured here: a blank and a gap alike."""
    assert "".join(filters.apply(no_text(kept), expression).ticker) == tickers
