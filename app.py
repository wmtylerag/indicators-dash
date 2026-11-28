from __future__ import annotations

import io
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------
APP_TITLE = "South Korea Macro Dashboard"
FETCH_START = "1989-01"  # one extra year so y/y changes exist from Jan 1990
DEFAULT_START = "1990-01"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(APP_DIR, "refresh_log.csv")
DATA_FILE = os.path.join(APP_DIR, "data_snapshot.json")  # last successful download, reused across restarts
# Automatic refresh interval (default: weekly). Override with env var, e.g. REFRESH_INTERVAL_DAYS=1
REFRESH_INTERVAL = timedelta(days=float(os.environ.get("REFRESH_INTERVAL_DAYS", "7")))
SCHEDULER_CHECK_SECONDS = 60 * 60  # background thread checks hourly whether a refresh is due
SESSION_POLL = "15m"  # open browser tabs check this often for newer data and redraw
HTTP_TIMEOUT = 60
USER_AGENT = "korea-macro-dashboard/1.0 (streamlit; python-requests)"

IMF_BASE = "https://api.imf.org/external/sdmx/3.0/data/dataflow"
IMF_CSV = "application/vnd.sdmx.data+csv;version=2.0.0"
OECD_BASE = "https://sdmx.oecd.org/public/rest/data"
OECD_CSV = "application/vnd.sdmx.data+csv; charset=utf-8"
WB_BASE = "https://api.worldbank.org/v2/country/KOR/indicator"

# Every raw series the app downloads. URLs are built from these so the exact API
# query is shown in the dashboard and in the README.
RAW_SOURCES = {
    "cpi_kor": {
        "label": "Korea CPI, all items (index)",
        "provider": "IMF – Consumer Price Index (CPI) dataset, IMF.STA:CPI",
        "url": f"{IMF_BASE}/IMF.STA/CPI/+/KOR.CPI._T.IX.M",
        "page": "https://data.imf.org/en/datasets/IMF.STA:CPI",
        "kind": "imf",
    },
    "cpi_usa": {
        "label": "United States CPI, all items (index)",
        "provider": "IMF – Consumer Price Index (CPI) dataset, IMF.STA:CPI",
        "url": f"{IMF_BASE}/IMF.STA/CPI/+/USA.CPI._T.IX.M",
        "page": "https://data.imf.org/en/datasets/IMF.STA:CPI",
        "kind": "imf",
    },
    "krw_usd": {
        "label": "KRW per USD, period average",
        "provider": "IMF – Exchange Rates (ER) dataset, IMF.STA:ER (XDC_USD, PA_RT)",
        "url": f"{IMF_BASE}/IMF.STA/ER/+/KOR.XDC_USD.PA_RT.M",
        "page": "https://data.imf.org/en/datasets/IMF.STA:ER",
        "kind": "imf",
    },
    "gdp_yoy": {
        "label": "Real GDP growth, % y/y (chain-linked volumes, s.a.)",
        "provider": "OECD – Quarterly National Accounts, GDP growth (DF_QNA_EXPENDITURE_GROWTH_OECD)",
        "url": f"{OECD_BASE}/OECD.SDD.NAD,DSD_NAMAIN1@DF_QNA_EXPENDITURE_GROWTH_OECD,/"
               "Q.Y.KOR.S1.S1.B1GQ._Z._Z._Z.PC.L.GY.T0102",
        "page": "https://data-explorer.oecd.org/",
        "kind": "oecd",
    },
    "ir3m": {
        "label": "3-month interbank rate (CD 91-day), % p.a.",
        "provider": "OECD – Monthly Financial Statistics (DF_FINMARK, IR3TIB)",
        "url": f"{OECD_BASE}/OECD.SDD.STES,DSD_STES@DF_FINMARK,/KOR.M.IR3TIB.PA.....",
        "page": "https://data-explorer.oecd.org/",
        "kind": "oecd",
    },
    "unemp": {
        "label": "Unemployment rate, 15+, % of labour force, s.a.",
        "provider": "OECD – Infra-annual Labour Statistics (DF_IALFS_UNE_M)",
        "url": f"{OECD_BASE}/OECD.SDD.TPS,DSD_LFS@DF_IALFS_UNE_M,1.0/"
               "KOR.UNE_LF_M.PT_LF_SUB._Z.Y._T.Y_GE15._Z.M",
        "page": "https://data-explorer.oecd.org/",
        "kind": "oecd",
    },
    "pop_growth": {
        "label": "Population growth, annual %",
        "provider": "World Bank – World Development Indicators (SP.POP.GROW)",
        "url": f"{WB_BASE}/SP.POP.GROW",
        "page": "https://data.worldbank.org/indicator/SP.POP.GROW?locations=KR",
        "kind": "wb",
    },
}


# --------------------------------------------------------------------------------------
# Fetchers
# --------------------------------------------------------------------------------------
def _get(url: str, params: dict | None = None, accept: str | None = None) -> requests.Response:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    r = requests.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r


def _period_to_timestamp(period: str) -> pd.Timestamp:
    """Map an SDMX/World Bank period label to the FIRST day of its LAST month.

    '1990-M01' / '1990-01' -> 1990-01-01 ; '1990-Q1' -> 1990-03-01 ; '1990' -> 1990-12-01.
    Using the period's final month means a quarter/year is shown once it is complete
    and is included in a month-range filter when its last month is inside the range.
    """
    p = str(period).strip()
    if "-Q" in p:
        y, q = p.split("-Q")
        return pd.Timestamp(int(y), int(q) * 3, 1)
    if "-M" in p:
        y, m = p.split("-M")
        return pd.Timestamp(int(y), int(m), 1)
    if len(p) == 7 and p[4] == "-":
        return pd.Timestamp(int(p[:4]), int(p[5:7]), 1)
    if len(p) == 4 and p.isdigit():
        return pd.Timestamp(int(p), 12, 1)
    raise ValueError(f"Unrecognised period label: {period!r}")


def _tidy(df: pd.DataFrame, period_col: str, value_col: str) -> pd.DataFrame:
    out = pd.DataFrame(
        {
            "period": df[period_col].astype(str),
            "value": pd.to_numeric(df[value_col], errors="coerce"),
        }
    ).dropna(subset=["value"])
    out["date"] = out["period"].map(_period_to_timestamp)
    out["period"] = out["period"].str.replace("-M", "-", regex=False)  # IMF '1990-M01' -> '1990-01'
    return out.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)


def fetch_imf(url: str) -> pd.DataFrame:
    """IMF SDMX 3.0 data query, returned as SDMX-CSV 2.0."""
    params = {"c[TIME_PERIOD]": f"ge:{FETCH_START}"}
    r = _get(url, params=params, accept=IMF_CSV)
    df = pd.read_csv(io.StringIO(r.text), dtype=str)
    return _tidy(df, "TIME_PERIOD", "OBS_VALUE")


def fetch_oecd(url: str) -> pd.DataFrame:
    """OECD SDMX 2.1 REST data query, returned as SDMX-CSV."""
    r = _get(url, params={"startPeriod": FETCH_START}, accept=OECD_CSV)
    df = pd.read_csv(io.StringIO(r.text), dtype=str)
    return _tidy(df, "TIME_PERIOD", "OBS_VALUE")


def fetch_wb(url: str) -> pd.DataFrame:
    """World Bank API v2 (JSON)."""
    params = {"format": "json", "per_page": 20000, "date": f"{FETCH_START[:4]}:{datetime.now().year}"}
    r = _get(url, params=params)
    payload = r.json()
    if not isinstance(payload, list) or len(payload) < 2 or payload[1] is None:
        raise ValueError(f"Unexpected World Bank response: {str(payload)[:200]}")
    df = pd.DataFrame([{"date": row["date"], "value": row["value"]} for row in payload[1]])
    return _tidy(df, "date", "value")


FETCHERS = {"imf": fetch_imf, "oecd": fetch_oecd, "wb": fetch_wb}


def download_all() -> tuple[dict, dict]:
    """Download every raw series. Returns (series dict, status dict)."""
    data, status = {}, {}
    for key, meta in RAW_SOURCES.items():
        try:
            df = FETCHERS[meta["kind"]](meta["url"])
            if df.empty:
                raise ValueError("no observations returned")
            data[key] = df
            status[key] = {
                "ok": True,
                "rows": len(df),
                "first": df["period"].iloc[0],
                "latest": df["period"].iloc[-1],
                "error": "",
            }
        except Exception as exc:  # noqa: BLE001 - surface any failure in the UI/log
            data[key] = _empty_series()
            status[key] = {"ok": False, "rows": 0, "first": "", "latest": "", "error": str(exc)[:300]}
    return data, status


def _empty_series() -> pd.DataFrame:
    return pd.DataFrame({"period": pd.Series(dtype=str), "value": pd.Series(dtype=float),
                         "date": pd.Series(dtype="datetime64[ns]")})


# ---- Persistent snapshot + weekly scheduler ------------------------------------------
# The latest download is saved to DATA_FILE so that every visitor, every browser tab and
# every server restart share the same data, and so the app knows when it last refreshed.
_REFRESH_LOCK = threading.Lock()
_MEMO: dict = {}  # in-process copy of the snapshot (also used if the disk is read-only)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S UTC")


def _save_snapshot(snap: dict) -> None:
    _MEMO["snap"] = snap
    payload = {
        "fetched_at": snap["fetched_at"].isoformat(),
        "trigger": snap["trigger"],
        "status": snap["status"],
        "data": {k: {"period": df["period"].tolist(), "value": df["value"].tolist()}
                 for k, df in snap["data"].items()},
    }
    try:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, DATA_FILE)  # atomic swap so readers never see a half-written file
    except OSError:
        pass


def _load_snapshot_from_disk() -> dict | None:
    try:
        with open(DATA_FILE, encoding="utf-8") as fh:
            payload = json.load(fh)
        data = {}
        for k, d in payload["data"].items():
            df = pd.DataFrame({"period": d["period"], "value": d["value"]})
            df["date"] = df["period"].map(_period_to_timestamp) if len(df) else pd.Series(dtype="datetime64[ns]")
            data[k] = df if len(df) else _empty_series()
        for k in RAW_SOURCES:  # a series added to RAW_SOURCES after the snapshot was written
            data.setdefault(k, _empty_series())
        return {"fetched_at": datetime.fromisoformat(payload["fetched_at"]), "trigger": payload["trigger"],
                "status": payload["status"], "data": data}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def current_snapshot() -> dict | None:
    memo = _MEMO.get("snap")
    disk = None
    if os.path.exists(DATA_FILE):
        mtime = os.path.getmtime(DATA_FILE)
        if memo is None or _MEMO.get("mtime") != mtime:
            disk = _load_snapshot_from_disk()
            if disk is not None:
                _MEMO["mtime"] = mtime
                _MEMO["snap"] = disk
    return disk or memo


def refresh_now(trigger: str, only_if_due: bool = False) -> dict:
    """Download everything, save the snapshot and write a log entry.

    With only_if_due=True, does nothing unless the saved data are older than REFRESH_INTERVAL
    (re-checked under the lock so concurrent callers never download twice)."""
    with _REFRESH_LOCK:
        snap = current_snapshot()
        if only_if_due and snap is not None and _utcnow() - snap["fetched_at"] < REFRESH_INTERVAL:
            return snap
        data, status = download_all()
        n_ok = sum(1 for v in status.values() if v["ok"])
        now = _utcnow()
        if n_ok == 0 and snap is not None:
            # Total outage: keep the previous data, log the failure, retry at the next check.
            new = snap
        else:
            if snap is not None:  # keep the previous copy of any series that failed this time
                for k, v in status.items():
                    if not v["ok"] and snap["status"].get(k, {}).get("ok"):
                        data[k] = snap["data"][k]
                        status[k] = {**snap["status"][k], "error": f"refresh failed, showing data from "
                                     f"{_fmt(snap['fetched_at'])}: {v['error']}"}
            new = {"fetched_at": now, "trigger": trigger, "status": status, "data": data}
            _save_snapshot(new)
        append_log({
            "logged_at_utc": _fmt(now),
            "trigger": trigger,
            "result": "updated" if new is not snap else "failed - previous data kept",
            "sources_ok": f"{n_ok}/{len(status)}",
            "next_scheduled_utc": _fmt(new["fetched_at"] + REFRESH_INTERVAL) if new is not snap
                                  else _fmt(now + timedelta(seconds=SCHEDULER_CHECK_SECONDS)),
            "latest_by_series": json.dumps({k: v["latest"] for k, v in status.items()}),
            "errors": "; ".join(f"{k}: {v['error']}" for k, v in status.items() if v["error"]),
        })
        return new


def _scheduler_loop() -> None:
    while True:
        try:
            refresh_now("Scheduled weekly refresh (background)", only_if_due=True)
        except Exception:  # noqa: BLE001 - never let the scheduler thread die
            pass
        time.sleep(SCHEDULER_CHECK_SECONDS)


@st.cache_resource(show_spinner=False)
def start_scheduler() -> threading.Thread:
    """Start one background scheduler thread per server process (not per visitor)."""
    t = threading.Thread(target=_scheduler_loop, name="weekly-refresh", daemon=True)
    t.start()
    return t


# --------------------------------------------------------------------------------------
# Calculations
# --------------------------------------------------------------------------------------
def _monthly(df: pd.DataFrame, name: str) -> pd.Series:
    return df.set_index("date")["value"].rename(name)


def build_monthly_panel(raw: dict) -> pd.DataFrame:
    """Monthly panel of raw inputs + calculated series."""
    m = pd.concat(
        [
            _monthly(raw["krw_usd"], "S_krw_per_usd"),
            _monthly(raw["cpi_kor"], "P_kor_cpi"),
            _monthly(raw["cpi_usa"], "P_usa_cpi"),
            _monthly(raw["ir3m"], "i_3m_interbank_pct"),
            _monthly(raw["unemp"], "unemployment_rate_pct"),
        ],
        axis=1,
    ).sort_index()
    m.index.name = "date"
    if not m.empty:
        full = pd.date_range(m.index.min(), m.index.max(), freq="MS")
        m = m.reindex(full)
        m.index.name = "date"

    # 1) Real exchange rate — exactly as specified: RER = S x P1 / P2,
    #    P1 = Korea CPI, P2 = US CPI (CPIs in the order the currencies are named).
    m["RER_S_x_Pkor_over_Pusa"] = m["S_krw_per_usd"] * m["P_kor_cpi"] / m["P_usa_cpi"]
    #    Alternative ordering (the textbook "KRW per USD in real terms"): S x P_USA / P_KOR.
    m["RER_S_x_Pusa_over_Pkor"] = m["S_krw_per_usd"] * m["P_usa_cpi"] / m["P_kor_cpi"]
    #    The two CPIs have different reference years in the IMF dataset, so also provide
    #    each RER rebased to its 2020 average = 100 (base-year independent).
    for col in ["RER_S_x_Pkor_over_Pusa", "RER_S_x_Pusa_over_Pkor"]:
        base = m.loc["2020-01-01":"2020-12-01", col].mean()
        m[f"{col}_index_2020eq100"] = m[col] / base * 100 if pd.notna(base) and base else pd.NA

    # 4) Headline inflation: y/y % change of Korea CPI.
    m["inflation_yoy_pct"] = (m["P_kor_cpi"] / m["P_kor_cpi"].shift(12) - 1) * 100

    # 5) Real interest rate (ex post), exact Fisher: (1+i)/(1+pi) - 1.
    i = m["i_3m_interbank_pct"] / 100
    pi = m["inflation_yoy_pct"] / 100
    m["real_rate_fisher_exact_pct"] = ((1 + i) / (1 + pi) - 1) * 100
    m["real_rate_approx_i_minus_pi_pct"] = m["i_3m_interbank_pct"] - m["inflation_yoy_pct"]
    return m


# Chart / metric metadata: what is plotted, units, frequency, sources.
def metric_specs(rer_choice: str, rer_scale: str) -> list[dict]:
    rer_col = "RER_S_x_Pkor_over_Pusa" if rer_choice.startswith("S × P_KOR") else "RER_S_x_Pusa_over_Pkor"
    if rer_scale.startswith("Index"):
        rer_col += "_index_2020eq100"
        rer_units = "Index, 2020 average = 100"
    else:
        rer_units = "KRW per USD × CPI ratio (CPI levels as published by IMF)"
    formula = (
        "RER(t) = S(t) × P_KOR(t) / P_USA(t)" if "P_kor_over" in rer_col
        else "RER(t) = S(t) × P_USA(t) / P_KOR(t)"
    )
    return [
        {
            "key": "rer", "title": "1. Real exchange rate vs USD (KRW/USD)",
            "panel": "monthly", "col": rer_col, "units": rer_units, "freq": "Monthly",
            "raw_keys": ["krw_usd", "cpi_kor", "cpi_usa"], "calculated": True,
            "method": f"{formula}; S = KRW per USD, period average; P = all-items CPI. "
                      "Calculated in this app from IMF data.",
        },
        {
            "key": "gdp", "title": "2. Real GDP growth",
            "panel": "gdp", "col": "value", "units": "% change, same quarter of previous year",
            "freq": "Quarterly", "raw_keys": ["gdp_yoy"], "calculated": False,
            "method": "GDP at chain-linked volumes (real), seasonally adjusted, y/y growth as published by OECD.",
        },
        {
            "key": "pop", "title": "3. Population growth",
            "panel": "pop", "col": "value", "units": "% change, annual", "freq": "Annual",
            "raw_keys": ["pop_growth"], "calculated": False,
            "method": "Midyear total population growth as published by the World Bank (WDI).",
        },
        {
            "key": "infl", "title": "4. Headline inflation (CPI)",
            "panel": "monthly", "col": "inflation_yoy_pct", "units": "% change, same month of previous year",
            "freq": "Monthly", "raw_keys": ["cpi_kor"], "calculated": True,
            "method": "π(t) = [P_KOR(t) / P_KOR(t−12) − 1] × 100. Calculated in this app from IMF CPI.",
        },
        {
            "key": "rir", "title": "5. Real interest rate (ex post)",
            "panel": "monthly", "col": "real_rate_fisher_exact_pct", "units": "% per annum",
            "freq": "Monthly", "raw_keys": ["ir3m", "cpi_kor"], "calculated": True,
            "method": "r(t) = [(1 + i(t)) / (1 + π(t)) − 1] × 100, i = 3-month interbank rate (OECD), "
                      "π = headline CPI inflation y/y (IMF). Calculated in this app; the simple "
                      "i − π approximation is also in the CSV.",
        },
        {
            "key": "unemp", "title": "6. Unemployment rate",
            "panel": "monthly", "col": "unemployment_rate_pct", "units": "% of labour force, seasonally adjusted",
            "freq": "Monthly", "raw_keys": ["unemp"], "calculated": False,
            "method": "Persons aged 15+, as published by OECD.",
        },
    ]


# --------------------------------------------------------------------------------------
# Refresh log
# --------------------------------------------------------------------------------------
_LOG_MEMO: list[dict] = []


def append_log(entry: dict) -> None:
    """Record a refresh. Safe to call from the background thread (no Streamlit calls)."""
    _LOG_MEMO.insert(0, entry)
    try:  # persist to disk (best effort; read-only filesystems fall back to memory)
        if os.path.exists(LOG_FILE):  # a log from an older app version with other columns
            with open(LOG_FILE, encoding="utf-8") as fh:
                if fh.readline().strip() != ",".join(entry.keys()):
                    os.replace(LOG_FILE, LOG_FILE.replace(".csv", f"_archived_{int(time.time())}.csv"))
        pd.DataFrame([entry]).to_csv(LOG_FILE, mode="a", index=False, header=not os.path.exists(LOG_FILE))
    except OSError:
        pass


def read_log() -> pd.DataFrame:
    if os.path.exists(LOG_FILE):
        try:
            return pd.read_csv(LOG_FILE).iloc[::-1].reset_index(drop=True)
        except Exception:  # noqa: BLE001 - e.g. a log written by an older app version
            pass
    return pd.DataFrame(_LOG_MEMO)


# --------------------------------------------------------------------------------------
# Helpers for UI
# --------------------------------------------------------------------------------------
def month_label(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m")


def series_for_spec(spec: dict, monthly: pd.DataFrame, raw: dict) -> pd.DataFrame:
    if spec["panel"] == "monthly":
        s = monthly[[spec["col"]]].rename(columns={spec["col"]: "value"}).dropna()
        s = s.reset_index()
        s["period"] = s["date"].dt.strftime("%Y-%m")
        return s[["date", "period", "value"]]
    key = {"gdp": "gdp_yoy", "pop": "pop_growth"}[spec["panel"]]
    return raw[key][["date", "period", "value"]].copy()


def make_chart(df: pd.DataFrame, spec: dict) -> go.Figure:
    fig = go.Figure()
    if spec["freq"] == "Annual":
        fig.add_bar(x=df["date"], y=df["value"], name=spec["title"],
                    customdata=df["period"], hovertemplate="%{customdata}: %{y:.2f}<extra></extra>")
    else:
        fig.add_scatter(x=df["date"], y=df["value"], mode="lines", name=spec["title"],
                        customdata=df["period"], hovertemplate="%{customdata}: %{y:,.2f}<extra></extra>")
    if spec["key"] in {"gdp", "pop", "infl", "rir"}:
        fig.add_hline(y=0, line_width=1, line_color="gray", opacity=0.5)
    fig.update_layout(
        height=330, margin=dict(l=10, r=10, t=10, b=10), showlegend=False,
        yaxis_title=spec["units"], xaxis_title=None, hovermode="x unified",
    )
    return fig


# --------------------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------------------
@st.fragment(run_every=SESSION_POLL)
def _watch_for_new_data() -> None:
    """Runs periodically in each open browser tab; redraws the page when the scheduler
    has saved newer data, so a dashboard left open on a screen stays current."""
    snap = current_snapshot()
    shown = st.session_state.get("shown_fetched_at")
    if snap is not None and shown is not None and snap["fetched_at"] > shown:
        st.rerun(scope="app")


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon="📈", layout="wide")
    st.title("🇰🇷 " + APP_TITLE)
    st.caption("Monthly, quarterly and annual indicators for the Republic of Korea from free public APIs "
               "(IMF, OECD, World Bank). No API keys required.")

    # ---- Sidebar: refresh + options ------------------------------------------------
    with st.sidebar:
        st.header("Data refresh")
        refresh = st.button("🔄 Refresh data now", width="stretch", type="primary")

    if refresh:
        with st.spinner("Downloading data from IMF, OECD and World Bank…"):
            snap = refresh_now("User refresh")
        st.toast(f"Refresh finished at {_fmt(_utcnow())} — {read_log().iloc[0]['result']}")
    else:
        snap = current_snapshot()
        if snap is None:
            with st.spinner("First run: downloading data from IMF, OECD and World Bank…"):
                snap = refresh_now("Initial download (no saved data)", only_if_due=True)
        elif _utcnow() - snap["fetched_at"] >= REFRESH_INTERVAL:
            # Catches up if the host was asleep when the background refresh was due.
            with st.spinner("Data are more than a week old – refreshing…"):
                snap = refresh_now("Scheduled weekly refresh (overdue at page load)", only_if_due=True)

    start_scheduler()  # started after the first download so the two never overlap
    raw, status = snap["data"], snap["status"]
    fetched_at = _fmt(snap["fetched_at"])
    next_due = snap["fetched_at"] + REFRESH_INTERVAL
    st.session_state["shown_fetched_at"] = snap["fetched_at"]

    with st.sidebar:
        interval_txt = ("weekly" if REFRESH_INTERVAL == timedelta(days=7)
                        else f"every {REFRESH_INTERVAL.total_seconds() / 86400:g} days")
        st.caption(f"**Auto-refresh:** {interval_txt}  \n"
                   f"**Last refreshed:** {fetched_at} ({snap['trigger'].lower()})  \n"
                   f"**Next automatic refresh:** {_fmt(max(next_due, _utcnow()))}")
        _watch_for_new_data()

    stale = {k: v for k, v in status.items() if v["ok"] and v["error"]}
    if stale:
        st.info("Latest refresh failed for some series; showing their previously downloaded data: " +
                ", ".join(f"**{RAW_SOURCES[k]['label']}**" for k in stale))
    failed = {k: v for k, v in status.items() if not v["ok"]}
    if failed:
        st.warning("Some sources could not be loaded: " +
                   "; ".join(f"**{RAW_SOURCES[k]['label']}** ({v['error']})" for k, v in failed.items()))

    monthly = build_monthly_panel(raw)

    # ---- Month range selector -------------------------------------------------------
    latest_candidates = [df["date"].max() for df in raw.values() if not df.empty]
    if not latest_candidates:
        st.error("No data could be downloaded. Check your internet connection and press Refresh.")
        st.stop()
    max_month = max(latest_candidates)
    min_month = pd.Timestamp(DEFAULT_START)
    months = pd.date_range(min_month, max_month, freq="MS")
    labels = [month_label(d) for d in months]

    with st.sidebar:
        st.header("Time period")
        start_lbl = st.selectbox("Start month", labels, index=0)
        end_lbl = st.selectbox("End month", labels, index=len(labels) - 1)
        st.header("Real exchange rate options")
        rer_choice = st.radio(
            "Formula", ["S × P_KOR / P_USA (as specified: P1 = Korea, P2 = US)",
                        "S × P_USA / P_KOR (P1 = US, P2 = Korea)"],
            index=0,
            help="S = KRW per USD. The specification RER = S × P1/P2 lists the CPIs in the same order as "
                 "the currencies (Korea, then US). Choose the second option for the textbook convention "
                 "in which a rise means a real depreciation of the won. Both are in the CSV.",
        )
        rer_scale = st.radio("Scale", ["Index (2020 = 100)", "Level (published CPI bases)"], index=0)

    start, end = pd.Timestamp(start_lbl + "-01"), pd.Timestamp(end_lbl + "-01")
    if start > end:
        st.error("Start month must be on or before end month.")
        st.stop()

    specs = metric_specs(rer_choice, rer_scale)

    # ---- Downloads (all observations & calculations in range) ----------------------
    long_frames = []
    for spec in specs:
        s = series_for_spec(spec, monthly, raw)
        s = s[(s["date"] >= start) & (s["date"] <= end)]
        long_frames.append(s.assign(
            metric=spec["title"].split(". ", 1)[1], units=spec["units"], frequency=spec["freq"],
            calculated="yes" if spec["calculated"] else "no",
            source="; ".join(RAW_SOURCES[k]["provider"] for k in spec["raw_keys"]),
            method=spec["method"],
        ))
    long_df = pd.concat(long_frames, ignore_index=True)
    long_df["date"] = pd.to_datetime(long_df["date"]).dt.strftime("%Y-%m-%d")
    long_df = long_df[["metric", "period", "date", "value", "units", "frequency", "calculated", "source", "method"]]

    wide = monthly.loc[start:end].copy()
    gdp_m = raw["gdp_yoy"].set_index("date")["value"].rename("real_gdp_growth_yoy_pct_quarterly")
    pop_m = raw["pop_growth"].set_index("date")["value"].rename("population_growth_pct_annual")
    wide = wide.join(gdp_m, how="left").join(pop_m, how="left")
    wide.index = wide.index.strftime("%Y-%m")
    wide.index.name = "month"

    with st.sidebar:
        st.header("Download")
        tag = f"{start_lbl}_to_{end_lbl}"
        st.download_button(
            "⬇️ All observations (long CSV)", long_df.to_csv(index=False).encode("utf-8"),
            file_name=f"korea_macro_observations_{tag}.csv", mime="text/csv", width="stretch",
            help="One row per metric and period: value, units, frequency, source and method.",
        )
        st.download_button(
            "⬇️ Raw inputs + calculations (wide monthly CSV)", wide.to_csv().encode("utf-8"),
            file_name=f"korea_macro_wide_{tag}.csv", mime="text/csv", width="stretch",
            help="One row per month with every raw input (S, both CPIs, interest rate, unemployment) and every "
                 "calculated column. Quarterly GDP growth sits in the quarter's last month; annual "
                 "population growth sits in December.",
        )

    # ---- Headline KPIs ----------------------------------------------------------------
    cols = st.columns(len(specs))
    for c, spec in zip(cols, specs):
        s = series_for_spec(spec, monthly, raw)
        if s.empty:
            c.metric(spec["title"].split(". ", 1)[1], "n/a")
            continue
        last = s.iloc[-1]
        prev = s.iloc[-2]["value"] if len(s) > 1 else None
        c.metric(spec["title"].split(". ", 1)[1], f"{last['value']:,.2f}",
                 None if prev is None else f"{last['value'] - prev:+.2f}",
                 delta_color="off", help=f"Latest observation: {last['period']} ({spec['units']})")

    st.caption(f"Showing **{start_lbl} → {end_lbl}**. Data last downloaded: **{fetched_at}**.")

    # ---- Charts -----------------------------------------------------------------------
    for row in range(0, len(specs), 2):
        c1, c2 = st.columns(2)
        for col, spec in zip((c1, c2), specs[row:row + 2]):
            with col:
                st.subheader(spec["title"])
                full = series_for_spec(spec, monthly, raw)
                sel = full[(full["date"] >= start) & (full["date"] <= end)]
                latest = full["period"].iloc[-1] if not full.empty else "n/a"
                st.markdown(
                    f"**Units:** {spec['units']}  \n"
                    f"**Frequency:** {spec['freq']}  \n"
                    f"**Latest observation:** {latest}"
                    + ("" if sel.empty else f"  ·  **in selection:** {sel['period'].iloc[-1]}")
                )
                if sel.empty:
                    st.info("No observations in the selected period.")
                else:
                    st.plotly_chart(make_chart(sel, spec), width="stretch", key=f"chart_{spec['key']}")
                src_lines = []
                for k in spec["raw_keys"]:
                    meta = RAW_SOURCES[k]
                    src_lines.append(f"- {meta['label']}: [{meta['provider']}]({meta['page']}) · "
                                     f"API: `{meta['url']}`")
                st.markdown(
                    f"**Source{'s' if len(src_lines) > 1 else ''}:**" +
                    (" *(calculated metric)*" if spec["calculated"] else "") + "\n" + "\n".join(src_lines) +
                    f"\n\n**Method:** {spec['method']}"
                )
                st.download_button(
                    "⬇️ CSV for this chart", sel.assign(date=sel["date"].dt.strftime("%Y-%m-%d"))
                    .to_csv(index=False).encode("utf-8"),
                    file_name=f"korea_{spec['key']}_{start_lbl}_to_{end_lbl}.csv", mime="text/csv",
                    key=f"dl_{spec['key']}",
                )
        st.divider()

    # ---- Data table, source status, log ------------------------------------------------
    with st.expander("📋 Data table (wide monthly, selected period)"):
        st.dataframe(wide, width="stretch")

    with st.expander("🔌 Source status (latest download)"):
        st.dataframe(pd.DataFrame([
            {"series": RAW_SOURCES[k]["label"], "provider": RAW_SOURCES[k]["provider"],
             "ok": v["ok"], "observations": v["rows"], "first": v["first"], "latest": v["latest"],
             "api_url": RAW_SOURCES[k]["url"], "error": v["error"]}
            for k, v in status.items()
        ]), width="stretch", hide_index=True)

    with st.expander("🕒 Refresh log", expanded=bool(refresh)):
        log_df = read_log()
        st.dataframe(log_df, width="stretch", hide_index=True)
        if not log_df.empty:
            st.download_button("⬇️ Download refresh log", log_df.to_csv(index=False).encode("utf-8"),
                               file_name="refresh_log.csv", mime="text/csv", key="dl_log")

    st.caption(
        "Data: IMF (CPI, ER datasets) via api.imf.org SDMX 3.0 · OECD Data Explorer via sdmx.oecd.org · "
        "World Bank WDI via api.worldbank.org. Real exchange rate, CPI inflation and real interest rate are "
        "calculated in this app from those inputs. Check each provider's terms of use before redistributing."
    )


if __name__ == "__main__":
    main()
