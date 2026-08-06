"""
app.py — Community Engagement Monitor.

Reads the scraper's CSV (engagement_data_steam.csv) and presents it as a
monitoring board: compare games, drill into one, and surface threads that
need attention (unanswered, or aging).

Design: cool slate/ink on light surface; teal = volume, amber = needs attention.
Headers in Space Grotesk, body in IBM Plex Sans, numbers in IBM Plex Mono.

Sentiment is still a placeholder column, so that section stays dormant until
the column is filled in. Nothing here invents data.

EA vs Player posts: EA_ACCOUNTS below is a manually maintained list of known
official/community-manager Steam usernames. This can't be reliably inferred
from scraped HTML alone (no verified "developer" badge selector exists yet —
see steam_scraper.py's SELECTORS comment for that same fragility pattern),
so it's a plain, editable list rather than a guess. Add real handles here to
make the EA/Player split meaningful; it's empty by default.

Language: detected from post/reply text via `langdetect`, bucketed into
Chinese / Spanish / English / Other / Unknown. Detection on short slangy
text (e.g. "gg", "lol nice") is unreliable — tested directly, and anything
under LANGUAGE_MIN_CHARS is left as "Unknown" rather than guessed, since a
wrong guess is worse than an honest blank here.

Setup (once):
    pip3 install streamlit pandas langdetect

Run:
    python3 -m streamlit run app.py
"""

import datetime as dt

import pandas as pd
import streamlit as st
from langdetect import detect, DetectorFactory, LangDetectException

DetectorFactory.seed = 0  # deterministic results — langdetect is otherwise randomized per call

CSV_PATH = "engagement_data_steam.csv"
NON_AUTHORS = ["", "[unknown]", "[deleted]"]

# Manually maintained — add known EA / community-manager Steam usernames here.
# Anything not in this set is counted as a Player post. Empty by default since
# this can't be inferred from the scrape itself; it needs real input from your
# community team to be meaningful.
EA_ACCOUNTS = set()

LANGUAGE_MIN_CHARS = 20  # below this, detection is unreliable enough to just skip it

st.set_page_config(page_title="Community Engagement Monitor", layout="wide")


# --- Styling ----------------------------------------------------------------

def inject_css():
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap');

        :root {
            --ink:   #14212E;
            --bg:    #EDF1F4;
            --card:  #FFFFFF;
            --line:  #DAE1E8;
            --muted: #61707E;
            --teal:  #0E7C7B;
            --amber: #E8920C;
        }

        .stApp { background: var(--bg); }
        html, body, [class*="css"] { font-family: 'IBM Plex Sans', sans-serif; color: var(--ink); }

        /* Trim default chrome for a cleaner board */
        #MainMenu, footer, [data-testid="stToolbar"] { visibility: hidden; }
        .block-container { padding-top: 2.2rem; max-width: 1300px; }

        /* Hero */
        .hero { margin-bottom: 1.4rem; }
        .hero .eyebrow {
            font-family: 'IBM Plex Mono', monospace; font-size: .72rem; letter-spacing: .18em;
            text-transform: uppercase; color: var(--amber); font-weight: 600; margin-bottom: .35rem;
        }
        .hero h1 {
            font-family: 'Space Grotesk', sans-serif; font-weight: 700; font-size: 2.4rem;
            line-height: 1.05; margin: 0 0 .45rem 0; color: var(--ink);
        }
        .hero .thesis { color: var(--muted); font-size: 1.02rem; max-width: 60ch; margin-bottom: .6rem; }
        .hero .meta {
            font-family: 'IBM Plex Mono', monospace; font-size: .78rem; color: var(--muted);
            border-top: 1px solid var(--line); padding-top: .55rem;
        }
        .hero .meta b { color: var(--teal); font-weight: 600; }

        /* Section header */
        .section { margin: 1.8rem 0 .7rem 0; }
        .section .eyebrow {
            font-family: 'IBM Plex Mono', monospace; font-size: .7rem; letter-spacing: .16em;
            text-transform: uppercase; color: var(--muted); font-weight: 600;
        }
        .section h2 {
            font-family: 'Space Grotesk', sans-serif; font-weight: 600; font-size: 1.5rem;
            margin: .15rem 0 0 0; color: var(--ink);
            border-bottom: 2px solid var(--line); padding-bottom: .5rem;
        }
        .section p.desc { color: var(--muted); font-size: .9rem; margin: .5rem 0 0 0; }

        /* KPI cards (custom HTML) */
        .kpis { display: flex; gap: .9rem; flex-wrap: wrap; margin: .3rem 0 .2rem 0; }
        .kpi {
            flex: 1 1 0; min-width: 150px; background: var(--card); border: 1px solid var(--line);
            border-radius: 12px; border-top: 3px solid var(--teal); padding: 1rem 1.1rem;
        }
        .kpi.attention { border-top-color: var(--amber); }
        .kpi .label {
            font-family: 'IBM Plex Mono', monospace; font-size: .68rem; letter-spacing: .12em;
            text-transform: uppercase; color: var(--muted); font-weight: 600;
        }
        .kpi .value {
            font-family: 'IBM Plex Mono', monospace; font-size: 2rem; font-weight: 600;
            color: var(--ink); line-height: 1.1; margin-top: .3rem;
        }
        .kpi.attention .value { color: var(--amber); }

        /* Charts / tables sit on cards */
        [data-testid="stVerticalBlock"] > div:has(> .stDataFrame),
        div:has(> .stArrowVegaLiteChart) { border-radius: 12px; }

        /* Sidebar */
        [data-testid="stSidebar"] { background: #E4EAEF; border-right: 1px solid var(--line); }
        [data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2 {
            font-family: 'Space Grotesk', sans-serif;
        }

        .stDataFrame { font-family: 'IBM Plex Sans', sans-serif; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def hero(games, records, updated):
    st.markdown(
        f"""
        <div class="hero">
          <div class="eyebrow">Community Signal</div>
          <h1>Engagement Monitor</h1>
          <div class="thesis">Where player communities are talking, and where they're being left waiting.
          Compare games at a glance, then drill into the threads that need a response.</div>
          <div class="meta"><b>{games}</b> games &nbsp;·&nbsp; <b>{records:,}</b> records tracked &nbsp;·&nbsp; loaded {updated}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def section(eyebrow, title, desc=None):
    d = f'<p class="desc">{desc}</p>' if desc else ""
    st.markdown(
        f'<div class="section"><div class="eyebrow">{eyebrow}</div><h2>{title}</h2>{d}</div>',
        unsafe_allow_html=True,
    )


def kpi_row(items):
    """items: list of (label, value, is_attention)."""
    cards = "".join(
        f'<div class="kpi{" attention" if att else ""}">'
        f'<div class="label">{label}</div><div class="value">{value}</div></div>'
        for label, value, att in items
    )
    st.markdown(f'<div class="kpis">{cards}</div>', unsafe_allow_html=True)


# --- Language detection ------------------------------------------------------

def detect_language(text):
    """Best-effort bucket: Chinese / Spanish / English / Other / Unknown.
    Tested directly against short gaming-forum-style text: under ~20 chars,
    detection is unreliable enough ("gg" -> Tagalog, "lol nice" -> Spanish)
    that guessing does more harm than an honest "Unknown"."""
    if not isinstance(text, str) or len(text.strip()) < LANGUAGE_MIN_CHARS:
        return "Unknown"
    try:
        code = detect(text)
    except LangDetectException:
        return "Unknown"
    if code.startswith("zh"):
        return "Chinese"
    if code == "es":
        return "Spanish"
    if code == "en":
        return "English"
    return "Other"


# --- Data loading -----------------------------------------------------------

@st.cache_data
def load_data(path):
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    df["date"] = df["timestamp"].dt.date
    for col in ["sentiment", "topic", "author_id", "game", "source", "action_type", "text", "permalink", "locked"]:
        if col in df.columns:
            df[col] = df[col].fillna("").astype(str)
    if "locked" not in df.columns:
        df["locked"] = ""  # older CSV, pre-migration — treated as unknown/not locked below
    df["is_locked"] = df["locked"].str.lower().eq("true")
    df["language"] = df["text"].apply(detect_language)
    df["poster_type"] = df["author_id"].apply(lambda a: "EA" if a in EA_ACCOUNTS else "Player")
    return df


inject_css()

try:
    df = load_data(CSV_PATH)
    loaded_from = CSV_PATH
except FileNotFoundError:
    hero(0, 0, "—")
    st.warning(f"Couldn't find `{CSV_PATH}` in this folder. Run the scraper first, or upload a CSV below.")
    uploaded = st.file_uploader("Upload an engagement CSV", type="csv")
    if uploaded is None:
        st.stop()
    df = load_data(uploaded)
    loaded_from = uploaded.name


# --- Aggregation helpers ----------------------------------------------------

def game_summary(data):
    rows = []
    for game, g in data.groupby("game"):
        if game == "":
            continue
        posts_g = g[g["action_type"] == "post"]
        replies_g = g[g["action_type"] == "reply"]
        answered = set(replies_g["permalink"])
        unanswered_g = posts_g[~posts_g["permalink"].isin(answered)]
        n_posts = len(posts_g)
        rows.append({
            "Game": game,
            "Records": len(g),
            "Posts": n_posts,
            "EA posts": int((posts_g["poster_type"] == "EA").sum()),
            "Player posts": int((posts_g["poster_type"] == "Player").sum()),
            "Replies": len(replies_g),
            "Unique authors": g[~g["author_id"].isin(NON_AUTHORS)]["author_id"].nunique(),
            "Unanswered": len(unanswered_g),
            "% unanswered": round(100 * len(unanswered_g) / n_posts, 1) if n_posts else 0.0,
            "Replies / post": round(len(replies_g) / n_posts, 1) if n_posts else 0.0,
        })
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values("Records", ascending=False).reset_index(drop=True)
    return out


def previous_period_range(date_range):
    """Given the active (start, end) filter, return the immediately preceding
    window of the same length — e.g. filtering to the last 7 days compares
    against the 7 days before that. Returns None if there's no valid range."""
    if not date_range or not isinstance(date_range, (list, tuple)) or len(date_range) != 2:
        return None
    start, end = date_range
    length_days = (end - start).days + 1
    prev_end = start - dt.timedelta(days=1)
    prev_start = prev_end - dt.timedelta(days=length_days - 1)
    return prev_start, prev_end


def add_period_deltas(summary, data, sel_source, sel_language, date_range):
    """Adds 'Δ Records' and 'Δ % unanswered' columns comparing the current
    filtered window to the equivalent-length period immediately before it.
    Blank (NaN) means there's no prior-period data for that game to compare
    against yet — left blank rather than treated as a 0 baseline, since a
    brand-new game isn't 'down 100%', it just has no history."""
    prev_range = previous_period_range(date_range)
    if prev_range is None:
        return summary

    prev_start, prev_end = prev_range
    prev_data = data[data["source"].isin(sel_source)]
    prev_data = prev_data[prev_data["language"].isin(sel_language)]
    prev_data = prev_data[
        ((prev_data["date"] >= prev_start) & (prev_data["date"] <= prev_end)) | prev_data["date"].isna()
    ]
    prev_summary = game_summary(prev_data)

    if prev_summary.empty:
        summary["Δ Records"] = pd.NA
        summary["Δ % unanswered"] = pd.NA
        return summary

    merged = summary.merge(
        prev_summary[["Game", "Records", "% unanswered"]].rename(
            columns={"Records": "_prev_records", "% unanswered": "_prev_unanswered"}
        ),
        on="Game", how="left",
    )
    merged["Δ Records"] = merged["Records"] - merged["_prev_records"]
    merged["Δ % unanswered"] = (merged["% unanswered"] - merged["_prev_unanswered"]).round(1)
    return merged.drop(columns=["_prev_records", "_prev_unanswered"])


def thread_table(data):
    """One row per thread: posted date, active span, reply count, link."""
    now = pd.Timestamp.now(tz="UTC")
    rows = []
    for perma, g in data.groupby("permalink"):
        if perma == "":
            continue
        posts_g = g[g["action_type"] == "post"]
        replies_g = g[g["action_type"] == "reply"]
        ts = g["timestamp"].dropna()
        if ts.empty:
            continue
        posted = posts_g["timestamp"].min() if not posts_g.empty else ts.min()
        last = ts.max()
        span_days = round((last - posted).total_seconds() / 86400, 1) if pd.notna(last) and pd.notna(posted) else None
        age_days = round((now - posted).total_seconds() / 86400, 1) if pd.notna(posted) else None
        title = posts_g.iloc[0]["text"][:90] if not posts_g.empty else "(post not captured)"
        author = posts_g.iloc[0]["author_id"] if not posts_g.empty else ""
        rows.append({
            "Game": g["game"].iloc[0],
            "Thread": title,
            "Author": author,
            "Posted": posted.date() if pd.notna(posted) else None,
            "Active span (days)": span_days,
            "Age (days)": age_days,
            "Replies": len(replies_g),
            "Link": perma,
        })
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values("Active span (days)", ascending=False, na_position="last").reset_index(drop=True)
    return out


# --- Sidebar filters --------------------------------------------------------

st.sidebar.header("Filters")

def multiselect_all(label, series):
    options = sorted(v for v in series.unique() if v != "")
    return st.sidebar.multiselect(label, options, default=options)

sel_source = multiselect_all("Source", df["source"])
sel_game = multiselect_all("Game", df["game"])
sel_language = multiselect_all("Language", df["language"])
sel_action = multiselect_all("Type (post / reply)", df["action_type"])

valid_dates = df["date"].dropna()
if not valid_dates.empty:
    min_d, max_d = valid_dates.min(), valid_dates.max()
    date_range = st.sidebar.date_input("Date range", value=(min_d, max_d), min_value=min_d, max_value=max_d)
else:
    date_range = None

author_query = st.sidebar.text_input("Author contains").strip().lower()
text_query = st.sidebar.text_input("Text contains (keyword)").strip().lower()

st.sidebar.caption(f"Loaded {loaded_from}")
st.sidebar.caption("Compare + Threads sections use Source + Date + Game + Language. The detail view uses every filter.")
if not EA_ACCOUNTS:
    st.sidebar.caption("⚠️ EA_ACCOUNTS list is empty — all posts currently count as Player. Add known EA/community-manager usernames in app.py to enable the EA vs Player split.")


# --- Filtered frames --------------------------------------------------------

def apply_source_date(data):
    out = data[data["source"].isin(sel_source)]
    out = out[out["language"].isin(sel_language)]
    if date_range and isinstance(date_range, (list, tuple)) and len(date_range) == 2:
        start, end = date_range
        out = out[((out["date"] >= start) & (out["date"] <= end)) | out["date"].isna()]
    return out

f_compare = apply_source_date(df)                    # source+date+language (all games)
f_scope = f_compare[f_compare["game"].isin(sel_game)]  # + game (for thread-level views)

f = f_scope.copy()                                    # + row-level filters (detail view)
f = f[f["action_type"].isin(sel_action)]
if author_query:
    f = f[f["author_id"].str.lower().str.contains(author_query, na=False)]
if text_query:
    f = f[f["text"].str.lower().str.contains(text_query, na=False)]


# --- Hero -------------------------------------------------------------------

hero(
    games=f_compare[f_compare["game"] != ""]["game"].nunique(),
    records=len(f_compare),
    updated=dt.date.today().isoformat(),
)


# --- Compare games ----------------------------------------------------------

section("01 · Overview", "Compare games",
        "All games side by side. The % unanswered and replies-per-post columns are size-independent, so they compare fairly even when one forum is far busier than another. Δ columns compare the current date range to the equivalent-length period right before it.")

summary = game_summary(f_compare)
if summary.empty:
    st.info("No game data in the current Source/Date/Language range.")
else:
    summary = add_period_deltas(summary, df, sel_source, sel_language, date_range)
    st.dataframe(summary, use_container_width=True, hide_index=True)
    c1, c2 = st.columns(2)
    with c1:
        st.caption("Total records by game")
        st.bar_chart(summary.set_index("Game")["Records"])
    with c2:
        st.caption("% of threads with no reply — higher means more players left waiting")
        st.bar_chart(summary.set_index("Game")["% unanswered"])

    st.caption("Records by language")
    lang_counts = f_compare[f_compare["language"] != ""]["language"].value_counts()
    if not lang_counts.empty:
        st.bar_chart(lang_counts)


# --- Threads (active span) --------------------------------------------------

section("02 · Threads", "Thread lifespan",
        "One row per discussion. Active span is first post to latest reply captured; sort any column. Note: span reflects replies within the scraper's per-thread cap, so very long threads may read slightly short.")

threads = thread_table(f_scope)
if threads.empty:
    st.info("No threads in scope.")
else:
    st.dataframe(
        threads,
        use_container_width=True, hide_index=True,
        column_config={
            "Link": st.column_config.LinkColumn("Link", display_text="open"),
            "Active span (days)": st.column_config.NumberColumn(format="%.1f"),
            "Age (days)": st.column_config.NumberColumn(format="%.1f"),
        },
    )


# --- Needs attention: unanswered --------------------------------------------

section("03 · Needs attention", "Unanswered threads",
        "Posts with zero replies, oldest first — the ones sitting open longest without anyone responding. Locked threads are excluded: a moderator closing a thread isn't the same as players being left waiting.")

posts_scope = f_scope[f_scope["action_type"] == "post"]
replies_scope = f_scope[f_scope["action_type"] == "reply"]

locked_zero_reply = posts_scope[posts_scope["is_locked"] & ~posts_scope["permalink"].isin(set(replies_scope["permalink"]))]
open_posts = posts_scope[~posts_scope["is_locked"]]
unanswered = open_posts[~open_posts["permalink"].isin(set(replies_scope["permalink"]))].copy()

now = pd.Timestamp.now(tz="UTC")
if not unanswered.empty:
    unanswered["Age (days)"] = ((now - unanswered["timestamp"]).dt.total_seconds() / 86400).round(1)
    unanswered = unanswered.sort_values("Age (days)", ascending=False, na_position="last")

kpi_row([("Unanswered threads", f"{len(unanswered)}", True)])
if len(locked_zero_reply) > 0:
    st.caption(f"({len(locked_zero_reply)} locked thread(s) with no replies excluded from this list — closed, not waiting.)")

if not unanswered.empty:
    view = unanswered.rename(columns={"game": "Game", "author_id": "Author", "text": "Thread", "permalink": "Link"})
    view["Posted"] = unanswered["timestamp"].dt.date
    view["Thread"] = view["Thread"].str.slice(0, 90)
    st.dataframe(
        view[["Game", "Posted", "Age (days)", "Author", "Thread", "Link"]],
        use_container_width=True, hide_index=True,
        column_config={
            "Link": st.column_config.LinkColumn("Link", display_text="open"),
            "Age (days)": st.column_config.NumberColumn(format="%.1f"),
        },
    )


# --- Detail view ------------------------------------------------------------

section("04 · Detail", "Filtered detail",
        "Reflects the full filter set on the left, including Type, Author, and keyword.")

posts = f[f["action_type"] == "post"]
replies = f[f["action_type"] == "reply"]
real_authors = f[~f["author_id"].isin(NON_AUTHORS)]["author_id"]

kpi_row([
    ("Records", f"{len(f):,}", False),
    ("Posts", f"{len(posts):,}", False),
    ("Replies", f"{len(replies):,}", False),
    ("Unique authors", f"{real_authors.nunique():,}", False),
])

if len(f) == 0:
    st.info("No records match the current filters. Loosen them on the left.")
    st.stop()

st.caption("Activity over time")
by_day = f.dropna(subset=["date"]).groupby("date").size().rename("records")
if not by_day.empty:
    st.line_chart(by_day)

c1, c2 = st.columns(2)
with c1:
    st.caption("Posts vs replies")
    st.bar_chart(f["action_type"].value_counts())
with c2:
    st.caption("Records per game (filtered)")
    st.bar_chart(f["game"].value_counts())

st.caption("Most active authors")
top_authors = f[~f["author_id"].isin(NON_AUTHORS)]["author_id"].value_counts().head(10)
if not top_authors.empty:
    st.bar_chart(top_authors)


# --- Sentiment (dormant) ----------------------------------------------------

section("05 · Sentiment", "Sentiment", "Activates automatically once the sentiment column is scored.")
if (f["sentiment"].str.strip() != "").any():
    st.bar_chart(f[f["sentiment"] != ""]["sentiment"].value_counts())
else:
    st.caption("Not scored yet — this section fills in once the `sentiment` column has values.")


# --- Download ---------------------------------------------------------------

section("06 · Export", "Download", "Save the current filtered slice as its own CSV.")
st.download_button(
    "Download filtered view (CSV)",
    data=f.to_csv(index=False).encode("utf-8"),
    file_name="engagement_filtered.csv",
    mime="text/csv",
)