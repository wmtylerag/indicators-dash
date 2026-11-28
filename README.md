South Korea Macro Dashboard (Streamlit)

An interactive dashboard that refreshes itself weekly. It shows six economic time series for the Republic of Korea. It goes back to January 1990 wherever the source has data that far. Every series comes from a free, public API with no key needed (IMF, OECD, World Bank).

#	Metric	Frequency	Units	Coverage*	Source
1	Real exchange rate vs USD (KRW/USD)	Monthly	Index 2020 = 100 (default) or level	1990-01 →	Calculated from IMF ER + IMF CPI
2	Real GDP growth	Quarterly	% y/y	1990-Q1 →	OECD Quarterly National Accounts
3	Population growth	Annual	%	1990 →	World Bank WDI
4	Headline inflation	Monthly	% y/y	1990-01 →	Calculated from IMF CPI
5	Real interest rate (ex post)	Monthly	% p.a.	1991-01 →	Calculated from OECD 3-month rate + IMF CPI
6	Unemployment rate	Monthly	% of labour force, s.a.	1990-01 →	OECD Infra-annual Labour Statistics

*Coverage checked against the live APIs in November 2026. OECD's 3-month interbank series for Korea starts in January 1991, so the real interest rate starts there too.

Quick start
bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py

The app opens at http://localhost:8501. It needs outbound HTTPS access to api.imf.org, sdmx.oecd.org and api.worldbank.org.

Streamlit Community Cloud: push app.py and requirements.txt to a GitHub repo, then choose New app → app.py. No secrets are needed.

Features
Requirement	How it is met
Choose a start and end month	Sidebar Start month / End month pickers (1990-01 to the latest month available)
CSV of all observations and calculations in the period	Sidebar ⬇️ All observations (long CSV): one row per metric and period, with value, units, frequency, source and method. ⬇️ Raw inputs + calculations (wide monthly CSV): one row per month with every raw input and every calculated column
CSV per chart	A ⬇️ CSV for this chart button under each chart
Refreshable from free public APIs	Sidebar 🔄 Refresh data now downloads everything again straight away
Self-refresh weekly	The app downloads fresh data automatically every 7 days. See Automatic weekly refresh
Units, frequency, source, latest observation on each chart	Shown above and below each chart. "Latest observation" is the newest data point the source has; "in selection" is the newest one inside your chosen period
All sources, including for calculated metrics	Each chart lists every input series with its provider page and exact API query, plus the formula. Calculated metrics are marked (calculated metric)
Timestamped log of refreshes	🕒 Refresh log expander: one row per download, with UTC timestamp, trigger (user refresh, scheduled weekly refresh, or first download), result, number of sources that loaded, next scheduled refresh, latest period per series, and any errors. It is also written to refresh_log.csv next to app.py and can be downloaded
Graceful failure	If one API is down during a refresh, that series keeps its previously downloaded data and a note says so. If every API is down, the previous data are kept and the failure is logged. 🔌 Source status shows the result for each series
Automatic weekly refresh

The app refreshes itself in three ways that work together:

Background scheduler. When the Streamlit server starts, one background thread is created for the whole server (not one per visitor). It checks every hour whether the saved data are 7 days old or more, and downloads fresh data if so. Nobody has to have the page open.
Catch-up at page load. Some hosts suspend the server when nobody visits; Streamlit Community Cloud, for example, puts idle apps to sleep. If the data are already overdue when someone opens the page, the page refreshes them before drawing the charts.
Open tabs update themselves. Every open browser tab checks every 15 minutes whether newer data have been saved and redraws if so. A dashboard left open on a screen therefore picks up the weekly update without a reload.

Each download is saved to data_snapshot.json next to app.py. All visitors share that copy, and it survives server restarts, so the app does not download again on every visit or restart. The 7-day clock runs from the last successful download, so pressing Refresh data now also restarts the clock. The sidebar shows when the data were last refreshed, what triggered the refresh, and when the next automatic refresh is due.

Hosting note. A scheduled refresh only runs while the server process is running. On a host that sleeps or restarts, step 2 makes the first visitor after the due date trigger the refresh. If you need a refresh exactly on time with no visitors, keep the app running continuously, for example on your own server with systemd, Docker restart: always, or a similar always-on setup.

Calculations

All formulas are applied in build_monthly_panel() in app.py.

1. Real exchange rate (RER)
RER(t) = S(t) × P1(t) / P2(t)
S(t) is KRW per 1 USD, monthly period average (IMF ER, XDC_USD, PA_RT).
P1(t), P2(t) are the monthly all-items CPIs (IMF CPI, CPI._T.IX).
Default (as specified): the CPIs are taken in the same order as the currencies in the definition of S (South Korea, then the US). So P1 = Korea CPI and P2 = US CPI, which gives RER = S × P_KOR / P_USA.
Alternative: a sidebar toggle switches to RER = S × P_USA / P_KOR. This is the textbook "real KRW per USD", where a rise means the won has depreciated in real terms. Both versions are always in the wide CSV.
Scale: the IMF publishes each CPI on its own reference year (Korea 2020 = 100, US 2010 = 100 at the time of writing). The raw level therefore depends on those base years. The default chart rebases the RER so that its 2020 average = 100, which removes that dependence. The raw level is still available in the chart (toggle) and in the CSV.
4. Headline inflation
π(t) = [P_KOR(t) / P_KOR(t−12) − 1] × 100

Uses the Korean CPI from the IMF. CPI is downloaded from January 1989 so that inflation exists from January 1990.

5. Real interest rate (ex post)
r(t) = [(1 + i(t)) / (1 + π(t)) − 1] × 100        (exact Fisher; charted)
r(t) ≈ i(t) − π(t)                                (approximation; in CSV)
i(t) is the 3-month interbank rate, % p.a. (OECD Monthly Financial Statistics, IR3TIB; for Korea this is the 91-day CD rate).
π(t) is headline CPI inflation, % y/y (from step 4).
This is an ex post (realised) real rate: it uses observed inflation over the past 12 months, not inflation expectations.
Non-calculated series
Real GDP growth: the OECD's published y/y growth of GDP at chain-linked volumes, seasonally adjusted.
Unemployment rate: the OECD's published rate, persons aged 15+, seasonally adjusted.
Population growth: the World Bank's published annual % growth of midyear total population.
Dating convention for mixed frequencies

Each observation is dated to the last month of its period: a quarter falls in March, June, September or December, and a year falls in December. A quarter or year is included in your selected range when its last month is inside the range. In the wide CSV, quarterly GDP growth sits in the quarter's last month and annual population growth sits in December.

Data sources and exact API queries

All queries below are what app.py sends (start period = 1989-01). Every endpoint was checked against the live API in November 2026.

IMF – SDMX 3.0 API (api.imf.org)

Provider page: https://data.imf.org/en/Resource-Pages/IMF-API · Terms: https://www.imf.org/external/terms.htm

Series	Dataset	Query
Korea CPI, all items, index	IMF.STA:CPI	https://api.imf.org/external/sdmx/3.0/data/dataflow/IMF.STA/CPI/+/KOR.CPI._T.IX.M?c[TIME_PERIOD]=ge:1989-01
US CPI, all items, index	IMF.STA:CPI	https://api.imf.org/external/sdmx/3.0/data/dataflow/IMF.STA/CPI/+/USA.CPI._T.IX.M?c[TIME_PERIOD]=ge:1989-01
KRW per USD, period average	IMF.STA:ER	https://api.imf.org/external/sdmx/3.0/data/dataflow/IMF.STA/ER/+/KOR.XDC_USD.PA_RT.M?c[TIME_PERIOD]=ge:1989-01

Requests send Accept: application/vnd.sdmx.data+csv;version=2.0.0, so the response is SDMX-CSV.

Note: the IMF retired its old dataservices.imf.org JSON API and the IFS database. The CPI and ER datasets above replace the IFS CPI and exchange-rate lines.

OECD – SDMX REST API (sdmx.oecd.org)

Data Explorer (dataset pages, API query builder and terms): https://data-explorer.oecd.org/

Series	Dataflow	Query
Real GDP growth, % y/y	DSD_NAMAIN1@DF_QNA_EXPENDITURE_GROWTH_OECD	https://sdmx.oecd.org/public/rest/data/OECD.SDD.NAD,DSD_NAMAIN1@DF_QNA_EXPENDITURE_GROWTH_OECD,/Q.Y.KOR.S1.S1.B1GQ._Z._Z._Z.PC.L.GY.T0102?startPeriod=1989-01
3-month interbank rate	DSD_STES@DF_FINMARK	https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_FINMARK,/KOR.M.IR3TIB.PA.....?startPeriod=1989-01
Unemployment rate, 15+, s.a.	DSD_LFS@DF_IALFS_UNE_M	https://sdmx.oecd.org/public/rest/data/OECD.SDD.TPS,DSD_LFS@DF_IALFS_UNE_M,1.0/KOR.UNE_LF_M.PT_LF_SUB._Z.Y._T.Y_GE15._Z.M?startPeriod=1989-01

Requests send Accept: application/vnd.sdmx.data+csv; charset=utf-8. The OECD rate-limits anonymous API use, so avoid pressing Refresh many times in a row. Weekly automatic refreshes are well within the limit.

World Bank – Indicators API v2 (api.worldbank.org)

Indicator page: https://data.worldbank.org/indicator/SP.POP.GROW?locations=KR · Licence: CC BY 4.0

Series	Indicator	Query
Population growth, annual %	SP.POP.GROW	https://api.worldbank.org/v2/country/KOR/indicator/SP.POP.GROW?format=json&per_page=20000&date=1989:<current year>
Calculated metrics: sources
Metric	Inputs
Real exchange rate	IMF ER (KRW/USD, period average) + IMF CPI (Korea) + IMF CPI (US)
Headline inflation	IMF CPI (Korea)
Real interest rate	OECD 3-month interbank rate + headline inflation (IMF CPI, Korea)

Please cite the original providers (IMF, OECD, World Bank) when you use this data, and check each provider's terms before redistributing it.

Output files
File	Contents
korea_macro_observations_<start>_to_<end>.csv	Long format: metric, period, date, value, units, frequency, calculated, source, method
korea_macro_wide_<start>_to_<end>.csv	One row per month: S_krw_per_usd, P_kor_cpi, P_usa_cpi, i_3m_interbank_pct, unemployment_rate_pct, RER_* (both formulas, level and 2020 = 100 index), inflation_yoy_pct, real_rate_fisher_exact_pct, real_rate_approx_i_minus_pi_pct, real_gdp_growth_yoy_pct_quarterly, population_growth_pct_annual
korea_<metric>_<start>_to_<end>.csv	The series shown in one chart
refresh_log.csv	Timestamped log of every download (manual, scheduled or first run)
data_snapshot.json	Saved copy of the latest download, created by the app. Delete it to force a full download on the next page load
Customising
Refresh interval: weekly by default. Set the environment variable REFRESH_INTERVAL_DAYS to change it, e.g. REFRESH_INTERVAL_DAYS=1 streamlit run app.py for daily, or edit REFRESH_INTERVAL in app.py. SCHEDULER_CHECK_SECONDS (hourly) and SESSION_POLL (15 minutes) control how often the app checks.
Earliest month: change FETCH_START (download start) and DEFAULT_START (first month you can select).
Different series: edit the RAW_SOURCES dictionary. Each entry holds the exact API URL, so you can swap in, for example, OECD's long-term bond yield (IRLT) in place of IR3TIB.
Troubleshooting
"Some sources could not be loaded": one API timed out or rate-limited the request. Wait a minute, then press Refresh. The error text is shown in Source status and in the refresh log.
Corporate proxy: requests respects the HTTPS_PROXY and REQUESTS_CA_BUNDLE environment variables.
Weekly refresh did not happen on time: the server was probably asleep or stopped. The refresh runs as soon as someone opens the page; see the hosting note under Automatic weekly refresh.
Read-only file system: the app still works. The saved data and the log are then kept in memory only, until the server restarts.
Latest month differs between charts: each source has its own release schedule. GDP is quarterly and population is annual, so their latest observation is earlier than the monthly series.
