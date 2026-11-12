"""The exports beyond the screener and price history: options, statements, calendars, market
tables, managers and funds. Offline: a fake client answers with rows shaped like Finviz's."""
import json
import math

import pandas as pd
import pytest
from click.testing import CliRunner

from finviz_cli import cli as cli_mod
from finviz_cli import endpoints
from finviz_cli.cli import cli
from finviz_cli.client import FinvizError

OPTIONS = ("Contract Name,Last Trade,Expiry,Strike,Last Close,Bid,Ask,Change $,Change %,Volume,Open Int.,Type,IV,Delta,Gamma,Theta,Vega,Rho\n"
           '"RUN261218P00175000",11/6/2026 10:07:56 AM,12/18/2026,175,0.02,,0.04,0,0.00%,0,763,put,1.1234,-0.0005,0.0000,-0.0048,0.0032,-0.0003\n'
           '"RUN270115C00402500",11/11/2026 12:48:49 PM,1/15/2027,402.5,23.17,21.4,23.6,-8.76,-6.64%,11,5916,call,0.3959,0.9755,0.0008,-0.0883,0.0930,0.3900\n')
ONE_STRIKE = ("Contract Name,Last Trade,Last Close,Bid,Ask,Change $,Change %,Volume,Open Int.,Type,IV,Delta,Gamma,Theta,Vega,Rho\n"
              '"RUN261218P00400000",11/11/2026 12:32:01 PM,0.67,0.61,0.69,0.04,6.35%,223,11671,put,0.4434,-0.0238,0.0008,-0.0546,0.0930,-0.0133\n')
STATEMENT = ("Period,Q1 2027,Q4 2026\n"
             "Period End Date,9/30/2026,6/30/2026\n"
             "Cash & Short Term Investments,64509000000,76843000000\n"
             '"Property, Plant & Equipment",-1200,\n'
             "EPS (Diluted),3.72,3.65\n")
ECONOMIC = ('Event,Date,Time,Impact,For,Actual,Expected,Prior\n"Fed Bowman Speech",2026-11-09,08:40,2,"","","",""\n'
            '"CB Employment Trends Index",2026-11-09,10:00,1,"Oct","108.36","","107.56"\n')
DIVIDENDS = 'Ticker,Company,Ex-Date,Amount,Special,Dividend Est. Yield\nECC,"Eagle Point Credit Company",2026-11-11,0.06,0,48.15\n'
FUTURES = ("No.,Ticker,Name,Price,Performance (5 Minutes),Performance (1 Hour),Performance (Day),Performance (Week),Performance (Month),"
           "Performance (Month To Date),Performance (Quarter),Performance (Half Year),Performance (Year To Date),Performance (Year)\n"
           "1,@CC,Cocoa,5065,-0.02,0.22,4.02,-8.8,-12.08,-8.72,-11.91,1.26,-21.89,-20.22\n")
PIPS = FUTURES.replace("Performance (", "Performance in Pips (").replace("@CC,Cocoa,5065", "USD/JPY,Japanese Yen,159.847")
MANAGERS = ("Name,Portfolio Manager,Investor ID,Report Date,Portfolio Value,# Investments,New Purchased,Sold Out,Added,Reduced,"
            "Top 10 Concentration (%),Turnover (%),Time Held Top 10,Time Held All\n"
            '"BERKSHIRE HATHAWAY INC","Warren Buffett",1067983,2026-06-30,299253556246,29,1,1,7,6,88.47,6.9,29.9,21\n')
FUND_HOLDINGS = ("Ticker,Name,Asset Category,Balance,Units,Market Value,% Portfolio,Prev. % Portfolio,% Change,Payoff Profile,Country\n"
                 '"AAA","Example Corp","EC",72382695,"NS",14530826021,14.68,15.02,0.59,"Long","US"\n')
NEWS = '"Title","Source","Date","Url","Category","Ticker"\n"A headline","Wire",2026-11-11 13:08:17,"https://example.com/a",Stock,AAA\n'
INSIDERS = ("Ticker,Owner,Owner CIK,Relationship,Date,Transaction,Cost,#Shares,Value ($),#Shares Total,SEC Form 4,SEC Form 4 Link\n"
            "AAA,Big Holder LLC,1228754,10% Owner,11/04/2026,Sale,25.76,100,2576,900,11/06/2026 16:15,http://www.sec.gov/x\n")
SHORT_INTEREST = "Settlement Date,Short Interest,Shares Float,Avg. Daily Volume,Short Float,Short Ratio\n2026-10-30,6207615,39600000,2400000,15.68,2.5\n"
FILINGS = "Filing Date,Report Date,Form,Description,Filing,Document\n2026-11-06,2026-09-30,10-Q,Quarterly report,https://www.sec.gov/f,https://www.sec.gov/d\n"
GROUPS = '"No.","Name","Market Cap","P/E","Dividend Yield","Average Volume","Change","Volume","Stocks"\n1,"Technology",25000000,30.1,0.60%,2500.5,1.20%,3000000000,750\n'
HOLDINGS = ("Ticker,Name,Type,Sector,Industry,Shares Held,Market Value,% Portfolio,Prev. % Portfolio,Change in Shares,% Change,% Ownership,"
            "Avg. Price,Q 1st Owned\n"
            '"AAA","Example Corp",Call,"Technology","SERVICES, ETC.",15471400,2643443404,0.2,0,15471400,100,0.11,169.15,"2026 Q2"\n')


class Feed:
    """Answers by path; remembers what was asked. `filename` is what the real client reports
    of the file Finviz attached."""
    answers = {}
    calls = []
    filename = ""

    def __init__(self):
        self.last_filename = Feed.filename

    def export(self, path, params=None):
        clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}       # as the real client drops them
        Feed.calls.append((path, clean))
        if path not in Feed.answers:
            raise FinvizError(f"Finviz answered HTTP 400 for /export/{path}: Invalid query")
        answer = Feed.answers[path]
        return answer(clean) if callable(answer) else answer


@pytest.fixture
def feed(home, monkeypatch):
    Feed.calls, Feed.filename = [], ""
    Feed.answers = {"stock": OPTIONS, "calendar/economic": ECONOMIC, "calendar/dividends": DIVIDENDS, "futures/performance": FUTURES,
                    "forex/performance": PIPS, "crypto/performance": FUTURES, "managers": MANAGERS, "funds": MANAGERS,
                    "managers/1067983/holdings": HOLDINGS, "funds/S000007195/holdings": FUND_HOLDINGS,
                    "managers/big-fund-llp-1067983/holdings": HOLDINGS}
    monkeypatch.setattr(cli_mod, "Client", Feed)
    return Feed


def asked():
    return Feed.calls[-1]


# ---------------------------------------------------------------- options

def test_the_options_chain_has_every_column_whatever_was_filtered(feed):
    chain = endpoints.options(Feed(), "run")
    assert asked() == ("stock", {"t": "RUN", "ty": "oc"})
    assert list(chain.columns)[:5] == ["ticker", "contract_name", "last_trade", "expiry", "strike"]
    assert chain.expiry.tolist() == ["2026-12-18", "2027-01-15"] and chain.strike.tolist() == [175.0, 402.5]      # dates as elsewhere
    assert chain.change_pct.tolist() == [0.0, -6.64] and chain.iv.tolist() == [1.1234, 0.3959] and math.isnan(chain.bid[0])
    assert chain.type.tolist() == ["put", "call"] and chain.ticker.tolist() == ["RUN", "RUN"]
    # Filtered on both: Finviz leaves both columns out, and they are put back.
    Feed.answers["stock"] = ONE_STRIKE
    one = endpoints.options(Feed(), "RUN", "2026-12-18", 400)
    assert asked() == ("stock", {"t": "RUN", "ty": "oc", "e": "2026-12-18", "s": 400})
    assert list(one.columns)[:5] == ["ticker", "contract_name", "last_trade", "expiry", "strike"]
    assert (one.expiry[0], one.strike[0], one.open_int[0]) == ("2026-12-18", 400.0, 11671)
    assert isinstance(asked()[1]["s"], int)                                        # a whole strike is sent whole: 400, not 400.0
    Feed.answers["stock"] = OPTIONS.replace(",Strike,", ",").replace(",175,", ",").replace(",402.5,", ",")
    assert endpoints.options(Feed(), "RUN", strike=402.5).strike.tolist() == [402.5, 402.5] and asked()[1]["s"] == 402.5
    Feed.calls = []
    for bad in (0, -5, float("nan"), float("inf"), "soon", True):
        with pytest.raises(FinvizError, match="the strike must be a positive number"):
            endpoints.options(Feed(), "RUN", strike=bad)
    assert Feed.calls == []


def test_an_options_answer_that_is_not_a_chain_is_refused(feed):
    with pytest.raises(FinvizError, match="the expiry must be a date"):
        endpoints.options(Feed(), "RUN", "next friday")
    for body, why in (("Date,Open,High,Low,Close,Volume\n11/10/2026,1,2,1,1.5,100\n", "came back without: contract_name, type"),
                      (OPTIONS.rstrip("\n"), "did not arrive whole"),
                      (OPTIONS.replace("12/18/2026", "someday"), "could not read the expiry dates"),
                      (OPTIONS.replace("\n", "\r"), "could not be read as a table")):
        Feed.answers["stock"] = body
        with pytest.raises(FinvizError, match=why):
            endpoints.options(Feed(), "RUN")
    Feed.answers["stock"] = OPTIONS.split("\n")[0] + "\n"                         # a chain with no contracts
    assert len(endpoints.options(Feed(), "RUN")) == 0


def test_an_options_column_is_put_back_only_when_its_filter_explains_it(feed):
    """Finviz leaves out the column that was filtered on. A chain that lacks one that was
    NOT filtered on is some other answer, not a chain with unknown expiries."""
    no_strike = OPTIONS.replace(",Strike,", ",").replace(",175,", ",").replace(",402.5,", ",")
    Feed.answers["stock"] = ONE_STRIKE                                             # neither column
    for kwargs, why in (({}, "came back without: expiry, strike"), ({"expiry": "2026-12-18"}, "came back without: strike"),
                        ({"strike": 400}, "came back without: expiry")):
        with pytest.raises(FinvizError, match=why):
            endpoints.options(Feed(), "RUN", **kwargs)
    Feed.answers["stock"] = no_strike
    with pytest.raises(FinvizError, match="came back without: strike"):
        endpoints.options(Feed(), "RUN")
    assert endpoints.options(Feed(), "RUN", strike=402.5).expiry.tolist() == ["2026-12-18", "2027-01-15"]


@pytest.mark.parametrize("call, path, good", [
    (lambda: endpoints.options(Feed(), "RUN"), "stock", OPTIONS),
    (lambda: endpoints.calendar(Feed(), "economic", "2026-11-09"), "calendar/economic", ECONOMIC),
    (lambda: endpoints.calendar(Feed(), "dividends", "2026-11-11"), "calendar/dividends", DIVIDENDS),
    (lambda: endpoints.performance(Feed(), "futures"), "futures/performance", FUTURES),
    (lambda: endpoints.holders(Feed(), "managers"), "managers", MANAGERS),
    (lambda: endpoints.holdings(Feed(), "1067983"), "managers/1067983/holdings", HOLDINGS),
    (lambda: endpoints.news(Feed(), ["AAA"]), "news", NEWS),
    (lambda: endpoints.insiders(Feed(), "AAA"), "insiders", INSIDERS),
    (lambda: endpoints.short_interest(Feed(), "AAA"), "stock", SHORT_INTEREST),
    (lambda: endpoints.filings(Feed(), "AAA"), "stock", FILINGS),
    (lambda: endpoints.groups(Feed(), "sector"), "groups", GROUPS),
])
def test_every_table_is_read_strictly(feed, call, path, good):
    """What is not the table asked for is an error, never a table with fewer rows or a
    plausible wrong number: an answer cut short, quoting that never closes, a row wider or
    narrower than the header, a line of something else."""
    Feed.answers[path] = good
    assert len(call()) >= 1
    header, first = good.split("\n")[0], good.split("\n")[1]
    for body, why in ((good.rstrip("\n"), "did not arrive whole"),
                      (header + "\n" + first + ',"never closed\n', "could not be read as a table|did not arrive whole"),
                      (header + "\n" + first + ",one,too,many\n", "a row does not have the header's columns"),
                      (header + "\n" + first.rsplit(",", 1)[0] + "\n", "a row does not have the header's columns"),
                      ("Error\n", "came back without"), ("Service temporarily unavailable\n", "came back without")):
        Feed.answers[path] = body
        with pytest.raises(FinvizError, match=why):
            call()
    Feed.answers[path] = header + "\n"                                              # the table with nothing in it is not an error
    assert len(call()) == 0


# ---------------------------------------------------------------- statements

def test_a_statement_keeps_numbers_as_numbers_and_says_its_currency(feed):
    Feed.answers["stock"], Feed.filename = STATEMENT, "finviz_run_balanceQuarterly_sgd_11-11-2026_13-44-28.csv"
    table = endpoints.statements(Feed(), "run", "balance", quarterly=True, growth=("yoy_pct", "qoq"))
    assert asked() == ("stock", {"t": "RUN", "ty": "statements", "s": "BQ", "growth": "yoy_pct,qoq"})
    assert list(table.columns) == ["ticker", "currency", "item", "q1_2027", "q4_2026"]
    assert table.currency.unique().tolist() == ["SGD"] and table.item.tolist()[2] == "Property, Plant & Equipment"
    assert table.q1_2027.tolist() == ["9/30/2026", 64509000000, -1200, 3.72]                  # a date, whole numbers, a decimal
    assert table.q4_2026.tolist()[:2] == ["6/30/2026", 76843000000] and table.q4_2026.tolist()[2] is None
    assert isinstance(table.q1_2027[1], int) and isinstance(table.q1_2027[3], float)
    endpoints.statements(Feed(), "RUN", "income", reuters=True, oldest_first=True)
    assert asked() == ("stock", {"t": "RUN", "ty": "statements", "s": "IA", "so": "R", "order": "oldest"})
    endpoints.statements(Feed(), "RUN", "cash", quarterly=True)
    assert asked()[1]["s"] == "CQ"


@pytest.mark.parametrize("name", ["", "finviz.csv", "finviz_run_balanceQuarterly_11-11-2026_13-44-28.csv", "something_else_usd_1.csv",
                                  "finviz_run_balanceQuarterly_dollars_11-11-2026_13-44-28.csv"])
def test_a_file_name_without_a_currency_gives_none_rather_than_a_guess(feed, name):
    Feed.answers["stock"], Feed.filename = STATEMENT, name
    assert endpoints.statements(Feed(), "RUN").currency.unique().tolist() == [""]


def test_a_statement_that_is_something_else_is_refused(feed):
    with pytest.raises(FinvizError, match="statement must be one of income, balance, cash"):
        endpoints.statements(Feed(), "RUN", "equity")
    with pytest.raises(FinvizError, match="growth rows are: yoy, yoy_pct, qoq, qoq_pct"):
        endpoints.statements(Feed(), "RUN", growth=("yoy", "cagr"))
    for body, why in ((OPTIONS, "came back without its Period column"), (STATEMENT.rstrip("\n"), "did not arrive whole"),
                      (STATEMENT + "Short Row,1\n", "did not arrive whole"), ("Period,Q1 2027,Q1 2027\nX,1,2\n", "came back without"),
                      ("Period,Q1 2027,Q1-2027\nX,1,2\n", "names a period twice"), ('Period,Q1 2027\n"never closed,1\n', "could not be read")):
        Feed.answers["stock"] = body
        with pytest.raises(FinvizError, match=why):
            endpoints.statements(Feed(), "RUN")


# ---------------------------------------------------------------- calendars

def test_the_economic_and_dividend_calendars(feed):
    week = endpoints.calendar(Feed(), "economic", "2026-11-09")
    assert asked() == ("calendar/economic", {"dateFrom": "2026-11-09"})
    assert list(week.columns) == ["event", "date", "time", "impact", "for", "actual", "expected", "prior"]
    assert week.time.tolist() == ["08:40", "10:00"] and week.impact.tolist() == [2, 1]
    # The figures are text, always: a release is a percent, a count in thousands, an index
    # level. Read as numbers they would be numbers one week and text the next, without the %.
    assert week.actual.tolist() == ["", "108.36"] and week.prior.tolist() == ["", "107.56"] and week["for"].tolist() == ["", "Oct"]
    Feed.answers["calendar/economic"] = ('Event,Date,Time,Impact,For,Actual,Expected,Prior\n"CPI YoY",2026-11-12,08:30,3,"2026","2.6%","2.5%","2.4%"\n'
                                         '"Jobless Claims",2026-11-12,08:30,2,"2026","","220K","218K"\n')
    mixed = endpoints.calendar(Feed(), "economic", "2026-11-12", "2026-11-12")
    assert mixed.actual.tolist() == ["2.6%", ""] and mixed.expected.tolist() == ["2.5%", "220K"] and mixed["for"].tolist() == ["2026", "2026"]
    Feed.answers["calendar/economic"] = ECONOMIC
    endpoints.calendar(Feed(), "economic", "2026-11-09", "2026-12-31")
    assert asked()[1] == {"dateFrom": "2026-11-09", "dateTo": "2026-12-31"}
    day = endpoints.calendar(Feed(), "dividends", "2026-11-11", sort="-yield")
    assert asked() == ("calendar/dividends", {"dateFrom": "2026-11-11", "sort": "-yield"})          # no last day: that one day
    assert day.ex_date.tolist() == ["2026-11-11"] and day.dividend_est_yield.tolist() == [48.15]
    # A last day that was named is always sent. Without one the economic calendar gives the
    # whole week, so a named Monday must not turn into its week.
    endpoints.calendar(Feed(), "economic", "2026-11-09", "2026-11-09")
    assert asked() == ("calendar/economic", {"dateFrom": "2026-11-09", "dateTo": "2026-11-09"})
    endpoints.calendar(Feed(), "dividends", "2026-11-11", "2026-11-11")
    assert asked() == ("calendar/dividends", {"dateFrom": "2026-11-11", "dateTo": "2026-11-11"})
    # The range is as long as Finviz serves: the last day may be that many days past the first.
    endpoints.calendar(Feed(), "dividends", "2026-01-01", "2026-04-01")                              # 90 days past: the most
    assert asked()[1] == {"dateFrom": "2026-01-01", "dateTo": "2026-04-01"}
    endpoints.calendar(Feed(), "economic", "2026-01-05", "2027-01-05")                               # 365 days past
    assert asked()[1]["dateTo"] == "2027-01-05"


@pytest.mark.parametrize("args, why", [
    (("weather", "2026-11-09"), "calendar must be one of economic, dividends"),
    (("economic", "11/09/2026"), "the first day must be a date"),
    (("economic", "2026-11-09", "soon"), "the last day must be a date"),
    (("economic", "2026-11-09", "2026-11-01"), "the last day is before the first"),
    (("economic", "2026-01-05", "2027-01-06"), "at most 365 days"),                # one day more than Finviz serves
    (("dividends", "2026-01-01", "2026-04-02"), "at most 90 days"),
    (("economic", "2026-11-09", "", "date"), "the economic calendar has no sort order"),
    (("dividends", "2026-11-09", "", "amount"), "can be sorted by: ticker, company, exdate, ordinary, special, yield"),
    (("dividends", "2026-11-09", "", "--yield"), "can be sorted by"),
])
def test_a_calendar_request_that_makes_no_sense_never_leaves(feed, args, why):
    with pytest.raises(FinvizError, match=why):
        endpoints.calendar(Feed(), *args)
    assert Feed.calls == []


def test_a_calendar_answer_must_be_that_calendar(feed):
    Feed.answers["calendar/economic"] = DIVIDENDS
    with pytest.raises(FinvizError, match="the economic calendar came back without: event, date"):
        endpoints.calendar(Feed(), "economic", "2026-11-09")
    Feed.answers["calendar/dividends"] = DIVIDENDS.split("\n")[0] + "\n"          # a day with no dividends is not an error
    assert len(endpoints.calendar(Feed(), "dividends", "2026-11-11")) == 0


# ---------------------------------------------------------------- futures, forex, crypto

def test_market_tables_sort_the_best_first_unless_turned_round(feed):
    table = endpoints.performance(Feed(), "futures")
    assert asked() == ("futures/performance", {"sort": "-perfDayPct"})
    assert list(table.columns) == ["no", "ticker", "name", "price", "perf_5m", "perf_1h", "perf_day", "perf_week", "perf_month",
                                   "perf_mtd", "perf_quarter", "perf_half", "perf_ytd", "perf_year"]
    assert (table.ticker[0], table.price[0], table.perf_day[0]) == ("@CC", 5065, 4.02)
    for args, sort in (({"sort": "week", "reverse": True}, "perfWeekPct"), ({"sort": "ticker"}, "ticker"),
                       ({"sort": "name", "reverse": True}, "-name"), ({"sort": "price"}, "-last"), ({"sort": "half"}, "-perfHalfYearPct")):
        endpoints.performance(Feed(), "futures", **args)
        assert asked()[1] == {"sort": sort}
    pips = endpoints.performance(Feed(), "forex", "week", pips=True)
    assert asked() == ("forex/performance", {"sort": "-perfWeekPips", "unit": "pips"})
    assert list(pips.columns)[4:6] == ["perf_5m", "perf_1h"] and pips.ticker[0] == "USD/JPY"       # the same names in pips
    endpoints.performance(Feed(), "crypto", quote="btc")
    assert asked() == ("crypto/performance", {"sort": "-perfDayPct", "c": "BTC"})


@pytest.mark.parametrize("args, why", [
    (("stocks",), "market must be one of futures, forex, crypto"),
    (("futures", "decade"), "sort must be one of day, 5min, hour"),
    (("futures", "day", False, True), "pips are for forex"),
    (("forex", "day", False, False, "EUR"), "a quote currency is for crypto"),
    (("crypto", "day", False, False, "DOGE"), "one of USD, USDT, EUR, BTC"),
])
def test_a_market_table_request_that_makes_no_sense_never_leaves(feed, args, why):
    with pytest.raises(FinvizError, match=why):
        endpoints.performance(Feed(), *args)
    assert Feed.calls == []


# ---------------------------------------------------------------- managers and funds

def test_managers_funds_and_their_holdings(feed):
    names = endpoints.holders(Feed(), "managers", "berkshire", "-portfolio_value")
    assert asked() == ("managers", {"search": "berkshire", "sort": "-portfolio_value"})
    assert (names.name[0], names.investor_id[0], names.report_date[0], names.investments[0]) == ("BERKSHIRE HATHAWAY INC", 1067983, "2026-06-30", 29)
    endpoints.holders(Feed(), "funds")
    assert asked() == ("funds", {})
    held = endpoints.holdings(Feed(), "1067983", "2026-06-30", "new", "call", sort="-valueUsd")
    assert asked() == ("managers/1067983/holdings", {"reportEndDate": "2026-06-30", "transactionType": "new", "optionType": "call", "sort": "-valueUsd"})
    assert (held.ticker[0], held.shares_held[0], held.q_1st_owned[0], held.pct_portfolio[0]) == ("AAA", 15471400, "2026 Q2", 0.2)
    fund = endpoints.holdings(Feed(), "S000007195", asset_category="ec", sort="issuerName")
    assert asked() == ("funds/S000007195/holdings", {"assetCategory": "EC", "sort": "issuerName"})
    assert list(fund.columns) == ["ticker", "name", "asset_category", "balance", "units", "market_value", "pct_portfolio", "prev_pct_portfolio",
                                  "pct_change", "payoff_profile", "country"]                  # a fund's own columns, not a manager's
    assert (fund.asset_category[0], fund.balance[0], fund.units[0], fund.country[0]) == ("EC", 72382695, "NS", "US")
    endpoints.holdings(Feed(), " big-fund-llp-1067983 ")                                      # the site's slug, as copied
    assert asked() == ("managers/big-fund-llp-1067983/holdings", {})
    endpoints.holdings(Feed(), names.investor_id.iloc[0])                                     # the id as the list gives it: a number
    assert asked() == ("managers/1067983/holdings", {})
    for not_an_id in (None, True, 1.5, ""):
        with pytest.raises(FinvizError, match="give an investor id"):
            endpoints.holdings(Feed(), not_an_id)


@pytest.mark.parametrize("call, why", [
    (lambda: endpoints.holders(Feed(), "banks"), "kind must be managers or funds"),
    (lambda: endpoints.holders(Feed(), "funds", sort="size"), "the list of funds can be sorted by: most_popular"),
    (lambda: endpoints.holdings(Feed(), "berkshire"), "give an investor id"),
    (lambda: endpoints.holdings(Feed(), "../screener?x=1067983"), "give an investor id"),
    (lambda: endpoints.holdings(Feed(), "1067983/../../screener"), "give an investor id"),
    (lambda: endpoints.holdings(Feed(), "1067983", "last quarter"), "the report date must be a date"),
    (lambda: endpoints.holdings(Feed(), "1067983", change="sold"), "change must be one of new, added, reduced, closed_out, no_change"),
    (lambda: endpoints.holdings(Feed(), "S000007195", option_type="put"), "an option type is for a manager's holdings"),
    (lambda: endpoints.holdings(Feed(), "1067983", asset_category="EC"), "an asset category is for a fund's holdings"),
    (lambda: endpoints.holdings(Feed(), "S000007195", asset_category="GOLD"), "an asset category is for a fund's holdings"),
    (lambda: endpoints.holdings(Feed(), "1067983", sort="issuerName"), "a manager's holdings can be sorted by: valueUsd"),
    (lambda: endpoints.holdings(Feed(), "\u0661\u0662\u0663"), "give an investor id"),                       # digits, but not plain ones
    (lambda: endpoints.insiders(Feed(), "AAA", owner_cik="\u00b2\u00b3"), "an owner's CIK is a number"),
    (lambda: endpoints.insiders(Feed(), "AAA", kind="gift"), "kind must be one of all, buy, sale, option"),
    (lambda: endpoints.earnings(Feed(), "next week"), "the first day must be a date"),
    (lambda: endpoints.earnings(Feed(), "2026-11-11", "tomorrow"), "the last day must be a date"),
    (lambda: endpoints.earnings(Feed(), "2026-11-11", "2026-11-01"), "the last day is before the first"),
])
def test_a_holders_request_that_makes_no_sense_never_leaves(feed, call, why):
    with pytest.raises(FinvizError, match=why):
        call()
    assert Feed.calls == []


# ---------------------------------------------------------------- the commands

def run(*args):
    return CliRunner().invoke(cli, [*args, "--format", "ndjson"])


def rows_of(result):
    return [json.loads(x) for x in result.stdout.splitlines() if x.startswith("{")]


def test_the_options_command(feed):
    r = run("options", "run")
    assert r.exit_code == 0, r.output
    assert [x["contract_name"] for x in rows_of(r)] == ["RUN261218P00175000", "RUN270115C00402500"]
    assert "RUN options: 2 contracts, 2 expiries 2026-12-18 .. 2027-01-15" in r.output
    r = run("options", "RUN", "--type", "call", "--show", "ticker,expiry,strike,type,iv")
    assert rows_of(r) == [{"ticker": "RUN", "expiry": "2027-01-15", "strike": 402.5, "type": "call", "iv": 0.3959}]
    Feed.answers["stock"] = ONE_STRIKE
    r = run("options", "RUN", "--expiry", "2026-12-18", "--strike", "400")
    assert asked() == ("stock", {"t": "RUN", "ty": "oc", "e": "2026-12-18", "s": 400.0}) and rows_of(r)[0]["strike"] == 400.0
    assert run("options", "RUN", "--expiry", "soon").exit_code == 2 and run("options", "RUN", "--strike", "0").exit_code == 2
    assert run("options", "RUN", "--type", "straddle").exit_code == 2 and run("options").exit_code == 2
    Feed.answers["stock"] = OPTIONS.split("\n")[0] + "\n"
    r = run("options", "RUN")
    assert r.exit_code == 0 and rows_of(r) == [] and "RUN options: 0 contracts" in r.output
    Feed.answers["stock"] = OPTIONS                                                  # in a table the Greeks keep their digits
    table = CliRunner().invoke(cli, ["options", "RUN", "--show", "type,iv,delta,gamma,theta,vega,rho,bid", "--format", "table"])
    assert next(x for x in table.output.splitlines() if x.startswith("call")).split() == ["call", "0.3959", "0.9755", "0.0008", "-0.0883", "0.0930", "0.3900", "21.4"]
    assert next(x for x in table.output.splitlines() if x.startswith("put")).split()[:4] == ["put", "1.1234", "-0.0005", "0.0000"]


def test_the_statements_command(feed):
    Feed.answers["stock"], Feed.filename = STATEMENT, "finviz_run_incomeAnnual_usd_11-11-2026_13-44-28.csv"
    r = run("statements", "run", "--statement", "balance", "--quarterly", "--growth", "yoy, qoq_pct", "--oldest-first", "--reuters")
    assert r.exit_code == 0, r.output
    assert asked() == ("stock", {"t": "RUN", "ty": "statements", "s": "BQ", "so": "R", "order": "oldest", "growth": "yoy,qoq_pct"})
    got = rows_of(r)
    assert got[1] == {"ticker": "RUN", "currency": "USD", "item": "Cash & Short Term Investments", "q1_2027": 64509000000, "q4_2026": 76843000000}
    assert got[2]["q4_2026"] is None and got[0]["q1_2027"] == "9/30/2026"
    assert "RUN balance statement, quarterly: 4 lines, 2 periods, amounts in USD" in r.output
    Feed.filename = ""
    assert "Finviz did not say which currency" in run("statements", "RUN").output
    assert run("statements", "RUN", "--statement", "equity").exit_code == 2
    r = run("statements", "RUN", "--growth", "cagr")
    assert r.exit_code != 0 and "growth rows are" in str(r.exception)
    table = CliRunner().invoke(cli, ["statements", "RUN", "--format", "table"])              # numbers print whole, blanks as blanks
    assert next(x for x in table.output.splitlines() if "Cash & Short" in x).split()[-2:] == ["64509000000", "76843000000"]


def test_the_calendar_commands(feed, monkeypatch):
    import datetime as dt
    from finviz_cli import config
    monkeypatch.setattr(config, "now_et", lambda: dt.datetime(2026, 11, 11, 12, 0, tzinfo=config.ET))
    r = run("economic")
    assert r.exit_code == 0 and asked() == ("calendar/economic", {"dateFrom": "2026-11-11"})          # today, when no day is given
    assert [x["event"] for x in rows_of(r)] == ["Fed Bowman Speech", "CB Employment Trends Index"] and "2 events" in r.output
    run("economic", "--from", "2026-11-02", "--to", "2026-11-20")
    assert asked()[1] == {"dateFrom": "2026-11-02", "dateTo": "2026-11-20"}
    r = run("dividends", "--sort=-yield")
    assert r.exit_code == 0 and asked() == ("calendar/dividends", {"dateFrom": "2026-11-11", "sort": "-yield"})
    assert rows_of(r)[0]["ticker"] == "ECC" and "dividends 2026-11-11 .. 2026-11-11: 1 rows" in r.output
    r = run("dividends", "--sort", "amount")
    assert r.exit_code != 0 and "can be sorted by" in str(r.exception)
    assert run("economic", "--from", "11/02/2026").exit_code == 2
    assert "at most 90 days" in str(run("dividends", "--from", "2026-01-01", "--to", "2026-06-01").exception)


def test_the_market_commands(feed):
    r = run("futures")
    assert r.exit_code == 0 and asked() == ("futures/performance", {"sort": "-perfDayPct"}) and rows_of(r)[0]["perf_day"] == 4.02
    run("futures", "--sort", "ticker", "--reverse")
    assert asked()[1] == {"sort": "-ticker"}
    r = run("forex", "--pips", "--sort", "week")
    assert asked() == ("forex/performance", {"sort": "-perfWeekPips", "unit": "pips"}) and "in pips" in r.output
    r = run("crypto", "--quote", "btc")
    assert asked() == ("crypto/performance", {"sort": "-perfDayPct", "c": "BTC"}) and "quoted in BTC" in r.output
    assert run("crypto", "--quote", "DOGE").exit_code == 2 and run("futures", "--sort", "decade").exit_code == 2
    assert run("futures", "--pips").exit_code == 2 and run("forex", "--quote", "BTC").exit_code == 2         # not offered where they do not apply


def test_the_managers_funds_and_holdings_commands(feed):
    r = run("managers", "berkshire", "--sort=-portfolio_value")
    assert r.exit_code == 0 and asked() == ("managers", {"search": "berkshire", "sort": "-portfolio_value"})
    assert rows_of(r)[0]["investor_id"] == 1067983 and "managers matching 'berkshire': 1 rows" in r.output
    run("funds")
    assert asked() == ("funds", {})
    r = run("holdings", "1067983", "--change", "new", "--option-type", "call", "--report-date", "2026-06-30")
    assert asked() == ("managers/1067983/holdings", {"reportEndDate": "2026-06-30", "transactionType": "new", "optionType": "call"})
    assert rows_of(r)[0]["ticker"] == "AAA" and "holdings of 1067983: 1 positions" in r.output
    run("holdings", "S000007195", "--asset-category", "ec", "--sort=-valueUsd")
    assert asked() == ("funds/S000007195/holdings", {"assetCategory": "EC", "sort": "-valueUsd"})
    Feed.calls = []
    for bad in (["holdings", "berkshire"], ["holdings", "1067983", "--asset-category", "EC"], ["managers", "--sort", "size"]):
        r = run(*bad)
        assert r.exit_code != 0 and isinstance(r.exception, FinvizError), bad
    assert run("holdings", "1067983", "--change", "sold").exit_code == 2 and Feed.calls == []


def test_the_filters_added_to_older_commands(feed):
    Feed.answers.update({"news": '"Title","Source","Date","Url","Category"\n"A headline","Wire",2026-11-11 13:08:17,"https://example.com/a",Market\n',
                         "insiders": "Ticker,Owner,Owner CIK,Relationship,Date,Transaction,Cost,#Shares,Value ($),#Shares Total,SEC Form 4,SEC Form 4 Link\n"
                                     "AAA,Big Holder LLC,1228754,10% Owner,11/04/2026,Sale,25.76,100,2576,900,11/06/2026 16:15,http://www.sec.gov/x\n",
                         "calendar/earnings": "Date,Ticker,Company,Market Cap\n2026-11-11 16:30,AAA,Example Corp,48980.28\n"})
    r = run("news", "--feed", "market", "--only", "blogs")
    assert r.exit_code == 0 and asked() == ("news", {"v": "1", "c": "2"}) and rows_of(r)[0]["title"] == "A headline"
    run("news", "--feed", "market", "--by-source")
    assert asked() == ("news", {"v": "2"})
    for bad in (["news", "--only", "news"], ["news", "aapl", "--by-source"], ["news", "aapl", "--feed", "market"]):
        r = run(*bad)
        assert r.exit_code != 0 and isinstance(r.exception, FinvizError), bad             # these belong to the market feed, which has no tickers
    r = run("insiders", "--owner-cik", "1228754", "--ten-percent", "exclude", "--sort=-transactionValue")
    assert r.exit_code == 0 and asked() == ("insiders", {"tc": "7", "oc": "1228754", "or": "-10", "o": "-transactionValue"})
    run("insiders", "aaa", "--ten-percent", "only")
    assert asked() == ("insiders", {"t": "AAA", "tc": "7", "or": "10"})
    assert "an owner's CIK is a number" in str(run("insiders", "--owner-cik", "Big Holder").exception)
    assert "insider transactions can be sorted by" in str(run("insiders", "--sort", "size").exception)
    r = run("earnings", "--from", "2026-11-11", "--sort=-marketCap")
    assert r.exit_code == 0 and asked() == ("calendar/earnings", {"dateFrom": "2026-11-11", "sort": "-marketCap"})
    assert "the earnings calendar can be sorted by" in str(run("earnings", "--sort", "size").exception)


def test_a_statement_figure_longer_than_a_float_is_kept_exactly(feed):
    Feed.answers["stock"] = "Period,FY 2026\nTotal Assets,9007199254740993\nNet Margin,-12.50\nShares,+1500\nOdd,1e3\nEmpty,\nText,n/a\nNot a number,nan\n"
    cells = endpoints.statements(Feed(), "RUN").fy_2026.tolist()
    assert cells == [9007199254740993, -12.5, 1500, 1000.0, None, "n/a", "nan"]
    assert isinstance(cells[0], int) and isinstance(cells[2], int) and isinstance(cells[3], float)


def test_a_sort_across_several_earnings_requests_is_one_ranking(feed):
    """More than 90 days is asked for in pieces. Each piece comes back sorted by itself; put
    end to end they would be several rankings."""
    pieces = {"2026-01-01": "Date,Ticker,Company,Market Cap\n2026-02-03 16:30,MID,Mid Corp,500\n2026-01-20 08:30,LOW,Low Corp,10\n",
              "2026-04-01": "Date,Ticker,Company,Market Cap\n2026-04-15 16:30,TOP,Top Corp,9000\n2026-05-01 08:30,NONE,No Cap Corp,\n",
              "2026-06-30": "Date,Ticker,Company,Market Cap\n2026-06-30 16:30,HIGH,High Corp,700\n"}
    Feed.answers["calendar/earnings"] = lambda params: pieces[params["dateFrom"]]
    ranked = endpoints.earnings(Feed(), "2026-01-01", "2026-06-30", "-marketCap")
    assert ranked.ticker.tolist() == ["TOP", "HIGH", "MID", "LOW", "NONE"]                  # one ranking, the unknown last
    assert [p["sort"] for _, p in Feed.calls] == ["-marketCap"] * 3
    assert endpoints.earnings(Feed(), "2026-01-01", "2026-06-30", "ticker").ticker.tolist() == ["HIGH", "LOW", "MID", "NONE", "TOP"]
    assert endpoints.earnings(Feed(), "2026-01-01", "2026-06-30", "earningsDate").ticker.tolist() == ["LOW", "MID", "TOP", "NONE", "HIGH"]
    assert endpoints.earnings(Feed(), "2026-01-01", "2026-06-30").ticker.tolist() == ["MID", "LOW", "TOP", "NONE", "HIGH"]      # no sort asked: as sent


def test_a_piece_whose_figures_are_all_still_to_come_does_not_break_the_ranking(feed):
    """The days ahead have estimates and no actuals: in that piece the column is all blank.
    Typed piece by piece it would be text there and numbers elsewhere, and could not be ranked."""
    header = "Date,Ticker,Company,Market Cap,EPS Estimate,EPS Actual,EPS Surprise\n"
    pieces = {"2026-08-14": header + "2026-08-20 16:30,BEAT,Beat Corp,500,1.00,1.50,50.00%\n2026-09-01 08:30,MISS,Miss Corp,900,2.00,0.50,-75.00%\n",
              "2026-11-12": header + "2026-11-12 16:30,SOON,Soon Corp,700,3.00,,\n2026-11-12 08:30,LATER,Later Corp,,,,\n"}
    Feed.answers["calendar/earnings"] = lambda params: pieces[params["dateFrom"]]
    best = endpoints.earnings(Feed(), "2026-08-14", "2026-11-12", "-epsActual")
    assert best.ticker.tolist() == ["BEAT", "MISS", "SOON", "LATER"]                       # the ones with no figure yet go last
    worst = endpoints.earnings(Feed(), "2026-08-14", "2026-11-12", "epsActual")
    assert worst.ticker.tolist() == ["MISS", "BEAT", "SOON", "LATER"]                      # ... in either direction
    assert best.eps_actual.tolist()[:2] == [1.5, 0.5] and best.eps_actual.isna().tolist() == [False, False, True, True]
    assert str(best.eps_actual.dtype) == "float64" and best.eps_surprise.tolist()[:2] == [50.0, -75.0]        # one type for the whole range
    assert endpoints.earnings(Feed(), "2026-08-14", "2026-11-12", "-marketCap").ticker.tolist() == ["MISS", "SOON", "BEAT", "LATER"]
    assert endpoints.earnings(Feed(), "2026-08-14", "2026-11-12", "-company").ticker.tolist() == ["SOON", "MISS", "LATER", "BEAT"]
    unsorted = endpoints.earnings(Feed(), "2026-08-14", "2026-11-12")
    assert unsorted.ticker.tolist() == ["BEAT", "MISS", "SOON", "LATER"] and str(unsorted.eps_actual.dtype) == "float64"
    pieces["2026-11-12"] = "Date,Ticker,Company\n2026-11-12 16:30,SOON,Soon Corp\n"        # a piece of another shape is not stitched on
    with pytest.raises(FinvizError, match="came back with other columns than the days before it"):
        endpoints.earnings(Feed(), "2026-08-14", "2026-11-12")
