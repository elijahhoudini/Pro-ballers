import os, sys, re, json, glob, asyncio, math, time
from datetime import datetime
from typing import Any, Dict, List, Optional

# ---------- Third-party deps ----------
# pip install:
# python-telegram-bot, SQLAlchemy, psycopg[binary], pandas, numpy, statsmodels, scikit-learn,
# feedparser, beautifulsoup4, cloudscraper, tenacity, python-dateutil, robotexclusionrulesparser, uvloop (non-windows)
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
import numpy as np

# Telegram
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram import Update

# Collectors deps
import feedparser
import cloudscraper
from bs4 import BeautifulSoup
from dateutil import parser as dtp

# ---------- Settings ----------
class Settings(BaseModel):
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    database_url: str = os.getenv("DATABASE_URL", "")  # may be empty
    app_env: str = os.getenv("APP_ENV", "dev")

settings = Settings()
# Single shared session to bypass Cloudflare-protected sites when gathering data.
scraper = cloudscraper.create_scraper()

# Default to a local SQLite database if none provided
default_sqlite_url = "sqlite+pysqlite:///data.db"
db_url = settings.database_url or default_sqlite_url
if db_url.startswith("sqlite"):
    print(f"Using local SQLite database at {db_url.split('///', 1)[1]}", file=sys.stderr)

# ---------- DB ----------
engine = create_engine(db_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)

# ---------- SQL Migrations (embedded) ----------
MIGRATIONS: Dict[str, str] = {
"0001_init.sql": r"""
CREATE TABLE IF NOT EXISTS players (
  player_id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  team TEXT,
  position TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS games_2025 (
  game_id SERIAL PRIMARY KEY,
  week INT NOT NULL,
  season_type TEXT CHECK (season_type IN ('preseason','regular')) NOT NULL,
  home TEXT NOT NULL,
  away TEXT NOT NULL,
  kickoff_utc TIMESTAMPTZ NOT NULL,
  weather JSONB,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS player_game_stats_2025 (
  player_id INT REFERENCES players(player_id),
  game_id INT REFERENCES games_2025(game_id),
  stats JSONB NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (player_id, game_id)
);

CREATE TABLE IF NOT EXISTS injuries_2025 (
  id BIGSERIAL PRIMARY KEY,
  player_id INT REFERENCES players(player_id),
  team TEXT,
  status TEXT,
  report_time TIMESTAMPTZ,
  source TEXT,
  url TEXT
);

CREATE TABLE IF NOT EXISTS props_2025 (
  id BIGSERIAL PRIMARY KEY,
  player_id INT REFERENCES players(player_id),
  game_id INT REFERENCES games_2025(game_id),
  market TEXT,
  line NUMERIC,
  book TEXT,
  price INT,
  fetched_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS features_2025 (
  player_id INT,
  game_id INT,
  features JSONB NOT NULL,
  generated_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (player_id, game_id)
);

CREATE TABLE IF NOT EXISTS predictions_2025 (
  id BIGSERIAL PRIMARY KEY,
  player_id INT,
  game_id INT,
  market TEXT,
  mean NUMERIC,
  sd NUMERIC,
  over_p NUMERIC,
  under_p NUMERIC,
  ev_over NUMERIC,
  ev_under NUMERIC,
  model_version TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS recommendations_2025 (
  id BIGSERIAL PRIMARY KEY,
  prediction_id BIGINT REFERENCES predictions_2025(id),
  pick TEXT CHECK (pick IN ('OVER','UNDER')),
  edge_bp NUMERIC,
  stake_units NUMERIC,
  rationale TEXT
);
""",
"0002_indexes.sql": r"""
CREATE INDEX IF NOT EXISTS idx_players_name ON players(LOWER(name));
CREATE INDEX IF NOT EXISTS idx_games_kickoff ON games_2025(kickoff_utc);
CREATE INDEX IF NOT EXISTS idx_props_player_game ON props_2025(player_id, game_id);
CREATE INDEX IF NOT EXISTS idx_preds_player_game ON predictions_2025(player_id, game_id);
CREATE INDEX IF NOT EXISTS idx_injuries_time ON injuries_2025(report_time);
""",
"0003_news.sql": r"""
CREATE TABLE IF NOT EXISTS news_2025 (
  id BIGSERIAL PRIMARY KEY,
  published_at TIMESTAMPTZ,
  title TEXT,
  url TEXT,
  source TEXT,
  tags TEXT[]
);
CREATE INDEX IF NOT EXISTS idx_news_time ON news_2025(published_at);
"""
}

def run_migrations():
    with engine.begin() as conn:
        for name in sorted(MIGRATIONS.keys()):
            sql = MIGRATIONS[name]
            if not sql.strip():
                continue
            if engine.dialect.name == "sqlite":
                sql = (
                    sql.replace("SERIAL", "INTEGER")
                       .replace("BIGSERIAL", "INTEGER")
                       .replace("TIMESTAMPTZ", "TIMESTAMP")
                       .replace("JSONB", "TEXT")
                       .replace("TEXT[]", "TEXT")
                       .replace("ARRAY[]::TEXT[]", "''")
                       .replace("now()", "CURRENT_TIMESTAMP")
                       .replace("NOW()", "CURRENT_TIMESTAMP")
                )
            print(f"Applying migration: {name}")
            for stmt in filter(None, (s.strip() for s in sql.split(";"))):
                conn.exec_driver_sql(stmt)
    print("All migrations applied.")

# ---------- Helpers: odds math ----------
def implied_prob(american:int) -> float:
    return (abs(american)/(abs(american)+100)) if american<0 else (100/(american+100))

def ev_win_prob(p:float, american:int) -> float:
    win = (100/abs(american)) if american<0 else (american/100)
    return p*win - (1-p)*1

def kelly_fraction(p:float, american:int, cap=0.5) -> float:
    q = 1-p
    b = (100/abs(american)) if american<0 else (american/100)
    f = (b*p - q) / b
    return max(0.0, min(cap, f))

# ---------- Collectors ----------
# A) RSS (team press releases / beat writers) — add your feeds in FEEDS below
FEEDS = {
    "rss": [],           # e.g., "https://www.colts.com/rss/somefeed.xml"
    "beat_writers": []   # e.g., "https://example.com/writer.xml"
}

def collectors_ingest_rss():
    urls = (FEEDS.get("rss", []) or []) + (FEEDS.get("beat_writers", []) or [])
    for url in urls:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries:
                when = None
                for key in ("published", "updated", "created"):
                    if key in e:
                        try:
                            when = dtp.parse(e[key])
                        except Exception:
                            when = None
                        break
                with SessionLocal() as s:
                    s.execute(text("""
                        INSERT INTO injuries_2025(player_id, team, status, report_time, source, url)
                        VALUES (NULL, NULL, NULL, :t, :src, :url)
                    """), {"t": when, "src": url, "url": getattr(e, "link", url)})
                    s.commit()
        except Exception:
            continue

# B) NFL.com injuries / news (defensive parsing; will skip quietly if layout changes)
NFL_INJURIES_URL = "https://www.nfl.com/injuries/"
NFL_NEWS_INDEX   = "https://www.nfl.com/news/"

def parse_nfl_injuries():
    try:
        resp = scraper.get(NFL_INJURIES_URL, timeout=20)
        resp.raise_for_status()
    except Exception:
        return 0
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("table tbody tr") or soup.select("section div table tbody tr")
    inserted = 0
    with SessionLocal() as s:
        for tr in rows:
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(tds) < 5:
                continue
            team = tds[0]
            status = tds[4] if len(tds) > 4 else None
            now_expr = "NOW()" if engine.dialect.name != "sqlite" else "CURRENT_TIMESTAMP"
            s.execute(text(f"""
                INSERT INTO injuries_2025(player_id, team, status, report_time, source, url)
                VALUES (NULL, :team, :status, {now_expr}, 'nfl.com', :url)
            """), {"team": team, "status": status, "url": NFL_INJURIES_URL})
            inserted += 1
        s.commit()
    return inserted

def parse_nfl_news():
    try:
        r = scraper.get(NFL_NEWS_INDEX, timeout=20)
        r.raise_for_status()
    except Exception:
        return 0
    soup = BeautifulSoup(r.text, "html.parser")
    cards = soup.select("a.d3-o-media-object") or soup.select("a[href*='/news/']")
    inserted = 0
    with SessionLocal() as s:
        for a in cards:
            href = a.get("href", "")
            url = href if href.startswith("http") else f"https://www.nfl.com{href}"
            title = a.get_text(strip=True)[:300]
            time_el = a.find("time")
            when = None
            if time_el and (time_el.get("datetime") or (time_el.text or "").strip()):
                try:
                    when = dtp.parse(time_el.get("datetime") or time_el.text.strip())
                except Exception:
                    when = None
            tags_expr = "ARRAY[]::TEXT[]" if engine.dialect.name != "sqlite" else "''"
            s.execute(text(f"""
                INSERT INTO news_2025(published_at, title, url, source, tags)
                VALUES (:t,:title,:url,'nfl.com',{tags_expr})
                ON CONFLICT DO NOTHING
            """), {"t": when, "title": title, "url": url})
            inserted += 1
        s.commit()
    return inserted

def collectors_ingest_sites():
    # Dispatcher: add more legal sources here as needed.
    total = 0
    try:
        total += parse_nfl_injuries()
    except Exception:
        pass
    try:
        total += parse_nfl_news()
    except Exception:
        pass
    return total

# ---------- Features (2025-only rolling) ----------
def build_features():
    # Minimal feature pipeline: copy raw stats JSON to features JSON until you add richer transforms.
    with SessionLocal() as s:
        rows = s.execute(text("""
            SELECT pgs.player_id, pgs.game_id, pgs.stats
            FROM player_game_stats_2025 pgs
        """)).fetchall()
        for pid, gid, stats in rows:
            s.execute(text("""
                INSERT INTO features_2025(player_id, game_id, features)
                VALUES (:pid,:gid,:f)
                ON CONFLICT (player_id, game_id) DO UPDATE SET features = EXCLUDED.features
            """), {"pid": pid, "gid": gid, "f": stats})
        s.commit()

# ---------- Model + Predictions ----------
def train_and_predict():
    # For each prop (line you text in), compute mu/sd from prior 2025 games for that market
    # and use a normal approximation to derive hit probabilities. If insufficient history,
    # skip (no fakery).
    with SessionLocal() as s:
        targets = s.execute(text("""
            SELECT pr.player_id, pr.game_id, pr.market, pr.line, pr.price, pr.id
            FROM props_2025 pr
        """)).fetchall()

        for pid, gid, market, line, price, prop_id in targets:
            hist = s.execute(text("""
                SELECT f.features
                FROM features_2025 f
                JOIN games_2025 g ON g.game_id = f.game_id
                WHERE f.player_id=:pid AND f.game_id < :gid
                ORDER BY g.week
            """), {"pid": pid, "gid": gid}).fetchall()
            vals = []
            for (feat,) in hist:
                if isinstance(feat, dict) and market in feat:
                    try:
                        vals.append(float(feat[market]))
                    except Exception:
                        pass
            if len(vals) >= 3:
                mu = float(np.mean(vals))
                sd = float(np.std(vals, ddof=1) or 0.0)
                sd = max(1e-6, sd)
            else:
                # Not enough 2025-only prior data to responsibly predict
                continue

            cdf = 0.5 * (1 + math.erf((float(line) - mu) / (sd * math.sqrt(2))))
            over_p = 1.0 - cdf
            under_p = cdf
            ev_o = ev_win_prob(over_p, int(price))
            ev_u = ev_win_prob(under_p, int(price))

            s.execute(text("""
                INSERT INTO predictions_2025(player_id, game_id, market, mean, sd, over_p, under_p, ev_over, ev_under, model_version)
                VALUES (:pid,:gid,:m,:mu,:sd,:po,:pu,:evo,:evu,'baseline-2025-v1')
            """), {"pid": pid, "gid": gid, "m": market, "mu": mu, "sd": sd, "po": over_p, "pu": under_p, "evo": ev_o, "evu": ev_u})
        s.commit()

# ---------- Recommendations ----------
def make_recommendations() -> int:
    made = 0
    with SessionLocal() as s:
        rows = s.execute(text("""
            SELECT p.id as prop_id, p.player_id, p.game_id, p.market, p.line, p.price,
                   pr.id as pred_id, pr.over_p, pr.under_p, pr.mean, pr.sd
            FROM props_2025 p
            JOIN predictions_2025 pr
              ON pr.player_id=p.player_id AND pr.game_id=p.game_id AND pr.market=p.market
        """)).fetchall()
        for prop_id, pid, gid, market, line, price, pred_id, over_p, under_p, mu, sd in rows:
            p_imp = implied_prob(int(price))
            ev_o = ev_win_prob(float(over_p), int(price))
            ev_u = ev_win_prob(float(under_p), int(price))
            if ev_o <= 0 and ev_u <= 0:
                continue
            if ev_o >= ev_u:
                pick = "OVER"; p_hit = float(over_p)
            else:
                pick = "UNDER"; p_hit = float(under_p)
            edge = p_hit - p_imp
            stake = kelly_fraction(p_hit, int(price), cap=0.5)
            rationale = f"Model μ={float(mu):.1f}, σ={float(sd):.1f}, line={float(line):.1f}; p_hit={p_hit:.3f}; edge={edge:.3f}"
            s.execute(text("""
                INSERT INTO recommendations_2025(prediction_id, pick, edge_bp, stake_units, rationale)
                VALUES (:pred,:pick,:edge,:stake,:why)
            """), {"pred": pred_id, "pick": pick, "edge": edge, "stake": stake, "why": rationale})
            made += 1
        s.commit()
    return made

# ---------- End-to-end refresh ----------
def refresh_all() -> int:
    collectors_ingest_rss()
    collectors_ingest_sites()
    build_features()
    train_and_predict()
    return make_recommendations()

# ---------- Telegram Bot ----------
HELP = (
    "🟢 Ready. Commands:\n"
    "/picks — top recommended props\n"
    "/player Name [, market , 7+] — lookup or probability\n"
    "/explain <recommendation_id> — rationale\n"
    "/preseason, /regular — set scope\n"
    "/addgame <week> <season_type> <YYYY-MM-DDTHH:MMZ> <home> <away>\n"
    "/addplayer <Name> | <TEAM> | <POS>\n"
    "/line <player> | <market> | <line> | <price> | <book> | <game_id>\n"
    "/refresh — run collectors → features → model → picks once"
)

async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)

async def picks(update: Update, _: ContextTypes.DEFAULT_TYPE):
    with SessionLocal() as s:
        rows = s.execute(text("""
          SELECT r.id, p.market, p.over_p, p.under_p, r.pick, r.edge_bp, r.stake_units
          FROM recommendations_2025 r
          JOIN predictions_2025 p ON p.id = r.prediction_id
          ORDER BY r.edge_bp DESC LIMIT 10
        """)).fetchall()
    if not rows:
        await update.message.reply_text("No recommendations yet — add lines with /line and run /refresh.")
        return
    lines = []
    for r in rows:
        lines.append(f"#{r.id} {r.market} → {r.pick} | edge {float(r.edge_bp):.3f} | stake {float(r.stake_units):.2f}u")
    await update.message.reply_text("\n".join(lines))

async def player_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """
    Usage:
      /player First Last
      /player First Last , <market> , <threshold>+
    Markets (short): rec, rec_yds, rush, rush_yds, pass_yds, pass_td
    Examples:
      /player Tyreek Hill , rec , 7+
      /player Tyreek Hill , rec_yds , 90+
    """
    text = (update.message.text or "")
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text(
            "Usage:\n/player First Last\n/player First Last , rec|rec_yds|rush|rush_yds|pass_yds|pass_td , 7+"
        )
        return

    query = parts[1].strip()

    if "," in query:
        try:
            name_raw, market_raw, thresh_raw = [p.strip() for p in query.split(",")]
        except Exception:
            await update.message.reply_text("Parse error. Example: /player Tyreek Hill , rec , 7+")
            return

        market_map = {
            "rec": "receptions",
            "rec_yds": "rec_yds",
            "rush": "rush_att",
            "rush_yds": "rush_yds",
            "pass_yds": "pass_yds",
            "pass_td": "pass_td",
        }
        market_key = market_map.get(market_raw.lower())
        if not market_key:
            await update.message.reply_text(
                "Unknown market. Use one of: rec, rec_yds, rush, rush_yds, pass_yds, pass_td"
            )
            return

        m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*\+\s*$", thresh_raw)
        if not m:
            await update.message.reply_text("Threshold must look like 7+ or 90+")
            return
        k = float(m.group(1))

        with SessionLocal() as s:
            row = s.execute(
                text("SELECT player_id, name FROM players WHERE LOWER(name)=LOWER(:n)"),
                {"n": name_raw},
            ).fetchone()
            if not row:
                await update.message.reply_text(
                    "Player not found. Use /addplayer Name | TEAM | POS first."
                )
                return
            pid = row[0]
            pred = s.execute(
                text(
                    """
                SELECT g.week, g.season_type, pr.mean, pr.sd
                FROM predictions_2025 pr
                JOIN games_2025 g ON g.game_id = pr.game_id
                WHERE pr.player_id = :pid AND pr.market = :m
                ORDER BY g.week DESC
                LIMIT 1
                """
                ),
                {"pid": pid, "m": market_key},
            ).fetchone()

        if not pred:
            await update.message.reply_text(
                "No 2025 prediction found for this player/market yet. Add lines with /line and /refresh."
            )
            return

        week, stype, mu, sd = pred
        mu = float(mu)
        sd = max(1e-6, float(sd))
        z = (k - 0.5 - mu) / sd
        from math import erf, sqrt
        cdf = 0.5 * (1 + erf(z / sqrt(2)))
        p_ge = 1.0 - cdf

        await update.message.reply_text(
            f"📈 {name_raw} — W{week} {stype[:3].upper()} {market_key}\n"
            f"μ={mu:.2f}, σ={sd:.2f}\n"
            f"P(≥ {k:g}) ≈ {p_ge:.3f}"
        )
        return

    name = query
    with SessionLocal() as s:
        row = s.execute(
            text("SELECT player_id, name FROM players WHERE LOWER(name)=LOWER(:n)"),
            {"n": name},
        ).fetchone()
        if not row:
            await update.message.reply_text(
                "Player not found. Make sure you /addplayer and collectors synced stats."
            )
            return
        pid = row[0]
        data = s.execute(
            text(
                """
            SELECT g.week, g.season_type, pr.market, pr.mean, pr.sd, pr.over_p
            FROM predictions_2025 pr
            JOIN games_2025 g ON g.game_id = pr.game_id
            WHERE pr.player_id = :pid
            ORDER BY g.week DESC
            LIMIT 8
            """
            ),
            {"pid": pid},
        ).fetchall()

    if not data:
        await update.message.reply_text("No 2025 projections yet for this player.")
        return

    msg = [f"📊 {name} — recent projections (2025):"]
    for w, stype, market, mu, sd, o in data:
        msg.append(
            f"W{w} {stype[:3].upper()} {market}: μ={float(mu):.1f}, σ={float(sd):.1f}, P(>line)={float(o):.3f}"
        )
    await update.message.reply_text("\n".join(msg))

async def explain(update: Update, _: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text("Usage: /explain <recommendation_id>")
        return
    rec_id = int(parts[1])
    with SessionLocal() as s:
        row = s.execute(text("""
          SELECT r.id, r.rationale FROM recommendations_2025 r WHERE r.id=:id
        """), {"id": rec_id}).fetchone()
    if not row:
        await update.message.reply_text("Recommendation not found.")
        return
    await update.message.reply_text(f"🔎 Explanation for #{rec_id}:\n{row[1] or 'N/A'}")

async def season_pre(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Season scope set to *preseason*. (Your collectors/models must respect this.)")

async def season_reg(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Season scope set to *regular*. (Your collectors/models must respect this.)")

async def addgame(update: Update, _: ContextTypes.DEFAULT_TYPE):
    # /addgame 1 preseason 2025-08-10T17:00Z KC NO
    parts = (update.message.text or "").split(maxsplit=5)
    if len(parts) != 6:
        await update.message.reply_text("Usage: /addgame <week> <season_type> <YYYY-MM-DDTHH:MMZ> <home> <away>")
        return
    _, week, season_type, iso, home, away = parts
    try:
        dt = datetime.fromisoformat(iso.replace("Z","+00:00"))
    except Exception:
        await update.message.reply_text("Bad datetime. Use ISO like 2025-08-10T17:00Z")
        return
    with SessionLocal() as s:
        s.execute(text("""
            INSERT INTO games_2025(week, season_type, home, away, kickoff_utc)
            VALUES (:w,:st,:h,:a,:ko)
        """), {"w": int(week), "st": season_type, "h": home, "a": away, "ko": dt})
        s.commit()
    await update.message.reply_text("Game added.")

async def addplayer(update: Update, _: ContextTypes.DEFAULT_TYPE):
    # /addplayer Name | TEAM | POS
    q = (update.message.text or "").split(maxsplit=1)
    if len(q) < 2:
        await update.message.reply_text("Usage: /addplayer Name | TEAM | POS")
        return
    try:
        name, team, pos = [x.strip() for x in q[1].split("|")]
    except Exception:
        await update.message.reply_text("Usage: /addplayer Name | TEAM | POS")
        return
    with SessionLocal() as s:
        s.execute(text("""
            INSERT INTO players(name, team, position) VALUES (:n,:t,:p)
        """), {"n": name, "t": team, "p": pos})
        s.commit()
    await update.message.reply_text("Player added.")

async def line_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE):
    # /line Name | market | line | price | book | game_id
    q = (update.message.text or "").split(maxsplit=1)
    if len(q) < 2:
        await update.message.reply_text("Usage: /line Name | market | line | price | book | game_id")
        return
    try:
        name, market, line, price, book, game_id = [x.strip() for x in q[1].split("|")]
        line = float(line); price = int(price); game_id = int(game_id)
    except Exception:
        await update.message.reply_text("Parse error. Example: /line Tyreek Hill | rec_yds | 88.5 | -115 | user | 123")
        return
    with SessionLocal() as s:
        row = s.execute(text("SELECT player_id FROM players WHERE LOWER(name)=LOWER(:n)"), {"n": name}).fetchone()
        if not row:
            await update.message.reply_text("Unknown player. Use /addplayer first.")
            return
        pid = row[0]
        s.execute(text("""
            INSERT INTO props_2025(player_id, game_id, market, line, book, price)
            VALUES (:pid,:gid,:m,:ln,:bk,:pr)
        """), {"pid": pid, "gid": game_id, "m": market, "ln": line, "bk": book, "pr": price})
        s.commit()
    await update.message.reply_text("Line saved. Run /refresh to update picks.")

async def refresh_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Starting refresh…")
    try:
        n = refresh_all()
        await update.message.reply_text(f"Refresh complete. {n} recommendations updated.")
    except Exception as e:
        await update.message.reply_text(f"Refresh failed: {e}")

async def player_message(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """Handle plain text messages with a player name and return recent stats."""
    name = (update.message.text or "").strip()
    if not name:
        return
    with SessionLocal() as s:
        row = s.execute(
            text("SELECT player_id, name FROM players WHERE LOWER(name)=LOWER(:n)"),
            {"n": name},
        ).fetchone()
        if not row:
            await update.message.reply_text(
                "Player not found. Use /addplayer Name | TEAM | POS first."
            )
            return
        pid = row[0]
        stat = s.execute(
            text(
                """
                SELECT g.week, g.season_type, s.stats
                FROM player_game_stats_2025 s
                JOIN games_2025 g ON g.game_id = s.game_id
                WHERE s.player_id = :pid
                ORDER BY g.week DESC
                LIMIT 1
                """
            ),
            {"pid": pid},
        ).fetchone()
    if not stat:
        await update.message.reply_text(f"No stats found for {name}.")
        return
    week, stype, stats_json = stat
    if isinstance(stats_json, str):
        try:
            stats_json = json.loads(stats_json)
        except Exception:
            pass
    stats_text = json.dumps(stats_json, indent=2, sort_keys=True)
    await update.message.reply_text(
        f"📊 {name} — W{week} {stype[:3].upper()} stats:\n{stats_text}"
    )

async def build_app(token: str):
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("picks", picks))
    app.add_handler(CommandHandler("player", player_cmd))
    app.add_handler(CommandHandler("explain", explain))
    app.add_handler(CommandHandler("preseason", season_pre))
    app.add_handler(CommandHandler("regular", season_reg))
    app.add_handler(CommandHandler("addgame", addgame))
    app.add_handler(CommandHandler("addplayer", addplayer))
    app.add_handler(CommandHandler("line", line_cmd))
    app.add_handler(CommandHandler("refresh", refresh_cmd))
    # Allow plain-text messages with just a player name
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), player_message))
    return app

# ---------- Entrypoint ----------
def main():
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "bot"

    if cmd == "migrate":
        run_migrations()
        return

    if cmd == "refresh":
        run_migrations()  # ensure tables exist
        n = refresh_all()
        print(f"Refresh complete. {n} recommendations updated.")
        return

    if cmd == "bot":
        # Auto-migrate every start (safe for CREATE IF NOT EXISTS)
        run_migrations()
        if not settings.telegram_token:
            raise RuntimeError("Set TELEGRAM_BOT_TOKEN before running the bot.")
        async def runner():
            app = await build_app(settings.telegram_token)
            await app.initialize()
            await app.start()
            await app.updater.start_polling()
            await app.idle()
        if os.name != "nt":
            try:
                import uvloop; uvloop.install()
            except Exception:
                pass
        asyncio.run(runner())
        return

    print(f"Unknown command: {cmd}")
    sys.exit(1)

if __name__ == "__main__":
    main()
