# Finviz screener columns

Generated with `finviz columns --markdown` from `finviz_cli/columns.py`. Ids and headers are Finviz's; keys are this CLI's names.

| id | key | Finviz header | kind | ETF only | unit |
|---|---|---|---|---|---|
| 0 | `no` | No. | number |  |  |
| 1 | `ticker` | Ticker | text |  |  |
| 2 | `company` | Company | text |  |  |
| 3 | `sector` | Sector | text |  |  |
| 4 | `industry` | Industry | text |  |  |
| 5 | `country` | Country | text |  |  |
| 6 | `mcap_m` | Market Cap | number |  | millions of dollars |
| 7 | `p_e` | P/E | number |  |  |
| 8 | `forward_p_e` | Forward P/E | number |  |  |
| 9 | `peg` | PEG | number |  |  |
| 10 | `p_s` | P/S | number |  |  |
| 11 | `p_b` | P/B | number |  |  |
| 12 | `p_cash` | P/Cash | number |  |  |
| 13 | `p_free_cash_flow` | P/Free Cash Flow | number |  |  |
| 14 | `dividend_yield` | Dividend Yield | number |  |  |
| 15 | `payout_ratio` | Payout Ratio | number |  |  |
| 16 | `eps_ttm` | EPS (ttm) | number |  |  |
| 17 | `eps_growth_this_year` | EPS Growth This Year | number |  |  |
| 18 | `eps_growth_next_year` | EPS Growth Next Year | number |  |  |
| 19 | `eps_growth_past_5_years` | EPS Growth Past 5 Years | number |  |  |
| 20 | `eps_growth_next_5_years` | EPS Growth Next 5 Years | number |  |  |
| 21 | `sales_growth_past_5_years` | Sales Growth Past 5 Years | number |  |  |
| 22 | `eps_growth_quarter_over_quarter` | EPS Growth Quarter Over Quarter | number |  |  |
| 23 | `sales_growth_quarter_over_quarter` | Sales Growth Quarter Over Quarter | number |  |  |
| 24 | `shares_out_m` | Shares Outstanding | number |  | millions of shares |
| 25 | `float_m` | Shares Float | number |  | millions of shares |
| 26 | `insider_ownership` | Insider Ownership | number |  |  |
| 27 | `insider_transactions` | Insider Transactions | number |  |  |
| 28 | `institutional_ownership` | Institutional Ownership | number |  |  |
| 29 | `institutional_transactions` | Institutional Transactions | number |  |  |
| 30 | `short_float` | Short Float | number |  | percent of float |
| 31 | `short_ratio` | Short Ratio | number |  |  |
| 32 | `return_on_assets` | Return on Assets | number |  |  |
| 33 | `return_on_equity` | Return on Equity | number |  |  |
| 34 | `return_on_invested_capital` | Return on Invested Capital | number |  |  |
| 35 | `current_ratio` | Current Ratio | number |  |  |
| 36 | `quick_ratio` | Quick Ratio | number |  |  |
| 37 | `lt_debt_equity` | LT Debt/Equity | number |  |  |
| 38 | `total_debt_equity` | Total Debt/Equity | number |  |  |
| 39 | `gross_margin` | Gross Margin | number |  |  |
| 40 | `operating_margin` | Operating Margin | number |  |  |
| 41 | `profit_margin` | Profit Margin | number |  |  |
| 42 | `perf_week` | Performance (Week) | number |  |  |
| 43 | `perf_month` | Performance (Month) | number |  |  |
| 44 | `perf_quarter` | Performance (Quarter) | number |  |  |
| 45 | `perf_half` | Performance (Half Year) | number |  |  |
| 46 | `perf_year` | Performance (Year) | number |  |  |
| 47 | `perf_ytd` | Performance (YTD) | number |  |  |
| 48 | `beta` | Beta | number |  |  |
| 49 | `atr` | Average True Range | number |  | dollars |
| 50 | `volatility_week` | Volatility (Week) | number |  |  |
| 51 | `volatility_month` | Volatility (Month) | number |  |  |
| 52 | `sma20` | 20-Day Simple Moving Average | number |  |  |
| 53 | `sma50` | 50-Day Simple Moving Average | number |  |  |
| 54 | `sma200` | 200-Day Simple Moving Average | number |  |  |
| 55 | `high_50d` | 50-Day High | number |  |  |
| 56 | `low_50d` | 50-Day Low | number |  |  |
| 57 | `high_52w` | 52-Week High | number |  |  |
| 58 | `low_52w` | 52-Week Low | number |  |  |
| 59 | `rsi` | Relative Strength Index (14) | number |  |  |
| 60 | `open_chg` | Change from Open | number |  | percent vs today's open |
| 61 | `gap` | Gap | number |  | percent, open vs previous close |
| 62 | `analyst_recom` | Analyst Recom | number |  |  |
| 63 | `avgvol` | Average Volume | number |  | shares (Finviz sends thousands; converted on load) |
| 64 | `relvol` | Relative Volume | number |  | today's volume / average volume |
| 65 | `price` | Price | number |  |  |
| 66 | `chg` | Change | number |  | percent vs previous close |
| 67 | `vol` | Volume | number |  | shares |
| 68 | `earnings` | Earnings Date | text |  |  |
| 69 | `target_price` | Target Price | number |  |  |
| 70 | `ipo_date` | IPO Date | text |  |  |
| 71 | `ah_close` | After-Hours Close | number |  |  |
| 72 | `ah_chg` | After-Hours Change | number |  |  |
| 73 | `book_sh` | Book/sh | number |  |  |
| 74 | `cash_sh` | Cash/sh | number |  |  |
| 75 | `dividend` | Dividend | number |  |  |
| 76 | `employees` | Employees | number |  |  |
| 77 | `eps_next_q` | EPS Next Q | number |  |  |
| 78 | `income_m` | Income | number |  | millions of dollars |
| 79 | `index` | Index | text |  |  |
| 80 | `optionable` | Optionable | text |  |  |
| 81 | `prev_close` | Prev Close | number |  |  |
| 82 | `sales_m` | Sales | number |  | millions of dollars |
| 83 | `shortable` | Shortable | text |  |  |
| 84 | `short_interest_m` | Short Interest | number |  | millions of shares |
| 85 | `float_pct` | Float % | number |  | percent of shares outstanding |
| 86 | `open` | Open | number |  |  |
| 87 | `high` | High | number |  |  |
| 88 | `low` | Low | number |  |  |
| 89 | `trades` | Trades | number |  | number of trades today |
| 90 | `perf_1m` | Performance (1 Minute) | number |  |  |
| 91 | `perf_2m` | Performance (2 Minutes) | number |  |  |
| 92 | `perf_3m` | Performance (3 Minutes) | number |  |  |
| 93 | `perf_5m` | Performance (5 Minutes) | number |  |  |
| 94 | `perf_10m` | Performance (10 Minutes) | number |  |  |
| 95 | `perf_15m` | Performance (15 Minutes) | number |  |  |
| 96 | `perf_30m` | Performance (30 Minutes) | number |  |  |
| 97 | `perf_1h` | Performance (1 Hour) | number |  |  |
| 98 | `perf_2h` | Performance (2 Hours) | number |  |  |
| 99 | `perf_4h` | Performance (4 Hours) | number |  |  |
| 100 | `asset_type` | Asset Type | text | yes |  |
| 101 | `etf_type` | ETF Type | text | yes |  |
| 102 | `region` | Region | text | yes |  |
| 103 | `single_category` | Single Category | text | yes |  |
| 104 | `sector_theme` | Sector/Theme | text | yes |  |
| 105 | `tags` | Tags | text | yes |  |
| 106 | `active_passive` | Active/Passive | text | yes |  |
| 107 | `net_expense_ratio` | Net Expense Ratio | number | yes |  |
| 108 | `total_holdings` | Total Holdings | number | yes |  |
| 109 | `assets_under_management` | Assets Under Management | number | yes |  |
| 110 | `net_asset_value` | Net Asset Value | number | yes |  |
| 111 | `net_asset_value_pct` | Net Asset Value % | number | yes |  |
| 112 | `net_flows_1_month` | Net Flows (1 Month) | number | yes |  |
| 113 | `net_flows_pct_1_month` | Net Flows % (1 Month) | number | yes |  |
| 114 | `net_flows_3_month` | Net Flows (3 Month) | number | yes |  |
| 115 | `net_flows_pct_3_month` | Net Flows % (3 Month) | number | yes |  |
| 116 | `net_flows_ytd` | Net Flows (YTD) | number | yes |  |
| 117 | `net_flows_pct_ytd` | Net Flows % (YTD) | number | yes |  |
| 118 | `net_flows_1_year` | Net Flows (1 Year) | number | yes |  |
| 119 | `net_flows_pct_1_year` | Net Flows % (1 Year) | number | yes |  |
| 120 | `return_1_year` | Return 1 Year | number | yes |  |
| 121 | `return_3_year` | Return 3 Year | number | yes |  |
| 122 | `return_5_year` | Return 5 Year | number | yes |  |
| 123 | `return_10_year` | Return 10 Year | number | yes |  |
| 124 | `return_since_inception` | Return Since Inception | number | yes |  |
| 125 | `ath_dist` | All-Time High | number |  | percent below the all-time high |
| 126 | `atl_dist` | All-Time Low | number |  | percent above the all-time low |
| 127 | `eps_surprise` | EPS Surprise | number |  |  |
| 128 | `rev_surprise` | Revenue Surprise | number |  |  |
| 129 | `exchange` | Exchange | text |  |  |
| 130 | `dividend_ttm` | Dividend TTM | number |  |  |
| 131 | `dividend_ex_date` | Dividend Ex Date | text |  |  |
| 132 | `eps_year_over_year_ttm` | EPS Year Over Year TTM | number |  |  |
| 133 | `sales_year_over_year_ttm` | Sales Year Over Year TTM | number |  |  |
| 134 | `range_52w` | 52-Week Range | text |  |  |
| 135 | `news_time` | News Time | text |  |  |
| 136 | `news_url` | News URL | text |  |  |
| 137 | `news` | News Title | text |  |  |
| 138 | `performance_3_years` | Performance (3 Years) | number |  |  |
| 139 | `performance_5_years` | Performance (5 Years) | number |  |  |
| 140 | `performance_10_years` | Performance (10 Years) | number |  |  |
| 141 | `ah_vol` | After-Hours Volume | number |  | shares |
| 142 | `eps_growth_past_3_years` | EPS Growth Past 3 Years | number |  |  |
| 143 | `sales_growth_past_3_years` | Sales Growth Past 3 Years | number |  |  |
| 144 | `enterprise_value` | Enterprise Value | number |  |  |
| 145 | `ev_ebitda` | EV/EBITDA | number |  |  |
| 146 | `ev_sales` | EV/Sales | number |  |  |
| 147 | `dividend_growth_1_year` | Dividend Growth 1 Year | number |  |  |
| 148 | `dividend_growth_3_years` | Dividend Growth 3 Years | number |  |  |
| 149 | `dividend_growth_5_years` | Dividend Growth 5 Years | number |  |  |
| 150 | `daily_digest` | Daily Digest | text |  |  |
| 151 | `security_type` | Security Type | text | yes |  |
| 152 | `tracked_index` | Tracked Index | text | yes |  |
| 153 | `candlestick` | Candlestick | text |  |  |
| 154 | `dilution_risk` | Dilution Risk | number |  | Finviz score, higher is riskier |
| 155 | `perf_mtd` | Performance (MTD) | number |  |  |
