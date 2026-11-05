#!/usr/bin/env python3
"""
NCAA men's basketball handicapping model, with Barttorvik (T-Rank) benchmark.

Daily workflow (run each morning):
    python cbb_model.py daily            # pull Torvik ratings, grade yesterday, predict today
Other commands:
    python cbb_model.py fetch 20261102   # pull one date's games/scores/lines from ESPN
    python cbb_model.py grade 20261102   # update ratings from that date's finals
    python cbb_model.py predict 20261105 # predict a date and flag edges vs Vegas
    python cbb_model.py torvik [file]    # pull Torvik ratings (or read a downloaded CSV)
    python cbb_model.py backfill 20261102 # load and grade every day from a date to yesterday
    python cbb_model.py backfill 20261102 reset  # same, but wipe ratings first and rebuild
    python cbb_model.py report           # season accuracy + record by edge tier
    python cbb_model.py lines lines.csv  # override lines (columns: date,home,away,spread,total)

Team IDs (the crosswalk):
    Every team has one permanent ID of ours (e.g. 100194) in team_master. team_alias
    maps each source's name or code for that team to the ID. A name no source mapping
    knows goes to the unmatched list instead of being guessed.
    python cbb_model.py unmatched                    # names waiting for a mapping, with suggestions
    python cbb_model.py link torvik "Ohio St." 100194  # confirm a mapping (permanent)
    python cbb_model.py accept                       # confirm all suggestions at once (review first!)
    python cbb_model.py find ohio                    # look up team IDs by name
    python cbb_model.py crosswalk teams.csv          # export the whole crosswalk to a CSV

Database: if the DATABASE_URL environment variable is set (Supabase/Postgres), it is
used. Otherwise everything is saved in a local file, cbb.db.

Conventions: spread is from the HOME team's view (negative = home favored), the same
as ESPN and Vegas.
"""
import csv, io, json, os, re, sys, difflib, sqlite3, urllib.request
from datetime import date, timedelta

# ----------------------------- settings ---------------------------------
LEAGUE_AVG_PTS = 71.0        # starting average points per team per game
HOME_COURT = 3.0             # our model: points of home advantage (0 at neutral sites)
TORVIK_HOME_EDGE = 0.014     # Torvik-style: home offense +1.4%, away offense -1.4%
K_START, K_MIN = 0.40, 0.10  # learning rate: big early, shrinks as a team plays more
K_DECAY_GAMES = 8
NON_D1_RATING = -10.0        # points: teams missing from Torvik's file (non-Division I)
SEEDED_GAMES = 4             # a Torvik-seeded team starts as if it had played this many
                             # games, so early results don't wipe out the seed
# Edge tiers: |model - Vegas| in points, checked from biggest to smallest
SPREAD_TIERS = [(6.0, "STRONG"), (4.0, "MEDIUM"), (2.0, "LEAN")]
TOTAL_TIERS  = [(8.0, "STRONG"), (5.0, "MEDIUM"), (3.0, "LEAN")]

ESPN = ("https://site.api.espn.com/apis/site/v2/sports/basketball/"
        "mens-college-basketball/scoreboard?dates={d}&groups=50&limit=500")
TORVIK = "https://barttorvik.com/{y}_team_results.csv"
HEADERS = {"User-Agent": "Mozilla/5.0"}

# Known Barttorvik name -> ESPN name pairs that simple rules can't match. These are
# confirmed into the crosswalk automatically. Anything else is handled with "link".
NAME_FIXES = {
    "Miami FL": "Miami", "Miami OH": "Miami (OH)", "Connecticut": "UConn",
    "N.C. State": "NC State", "Mississippi": "Ole Miss", "Saint Mary's": "Saint Mary's",
    "UNC Wilmington": "UNC Wilmington", "Louisiana Monroe": "UL Monroe",
    "Texas A&M Corpus Chris": "Texas A&M-Corpus Christi", "Nebraska Omaha": "Omaha",
    "Illinois Chicago": "UIC", "Cal St. Bakersfield": "Cal State Bakersfield",
    "Cal St. Fullerton": "Cal State Fullerton", "Cal St. Northridge": "Cal State Northridge",
    "Southeast Missouri St.": "Southeast Missouri State", "Detroit Mercy": "Detroit Mercy",
    "Purdue Fort Wayne": "Purdue Fort Wayne", "IU Indy": "IU Indianapolis",
    "SIU Edwardsville": "SIU Edwardsville", "Arkansas Pine Bluff": "Arkansas-Pine Bluff",
    "Bethune Cookman": "Bethune-Cookman", "Gardner Webb": "Gardner-Webb",
    "Loyola MD": "Loyola Maryland", "St. Thomas": "St. Thomas-Minnesota",
}

def season_year(d=None):
    d = d or date.today()
    return d.year + 1 if d.month >= 7 else d.year   # Torvik labels by the year it ends

# ----------------------------- database ---------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS team_master(
    team_id TEXT PRIMARY KEY, name TEXT, espn_id TEXT UNIQUE, conf TEXT);
CREATE TABLE IF NOT EXISTS team_alias(
    source TEXT, alias TEXT, team_id TEXT, PRIMARY KEY(source, alias));
CREATE TABLE IF NOT EXISTS unmatched(
    source TEXT, alias TEXT, suggested_id TEXT, suggested_name TEXT, first_seen TEXT,
    PRIMARY KEY(source, alias));
CREATE TABLE IF NOT EXISTS teams(
    team TEXT PRIMARY KEY, off REAL DEFAULT 0, def REAL DEFAULT 0, games INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS torvik(
    team TEXT PRIMARY KEY, torvik_name TEXT, adjoe REAL, adjde REAL, adjt REAL,
    barthag REAL, updated TEXT);
CREATE TABLE IF NOT EXISTS games(
    game_id TEXT PRIMARY KEY, date TEXT, home TEXT, away TEXT, neutral INTEGER,
    home_pts INTEGER, away_pts INTEGER, final INTEGER DEFAULT 0,
    vegas_spread REAL, vegas_total REAL,
    pred_home REAL, pred_away REAL, pred_spread REAL, pred_total REAL,
    tv_home REAL, tv_away REAL, tv_spread REAL, tv_total REAL,
    spread_edge REAL, spread_tier TEXT, spread_pick TEXT,
    total_edge REAL, total_tier TEXT, total_pick TEXT,
    graded INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v REAL);
"""

class DB:
    """Same code works on SQLite (local file) and Postgres (Supabase)."""
    def __init__(self):
        url = os.environ.get("DATABASE_URL")
        self.pg = bool(url)
        if self.pg:
            import psycopg2
            self.con = psycopg2.connect(url)
        else:
            path = os.environ.get("CBB_DB", os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "cbb.db"))
            self.con = sqlite3.connect(path)
        for stmt in SCHEMA.split(";"):
            if stmt.strip(): self.execute(stmt)
        self.commit()

    def execute(self, sql, params=()):
        cur = self.con.cursor()
        cur.execute(sql.replace("?", "%s") if self.pg else sql, params)
        return cur

    def commit(self): self.con.commit()

def league_avg(con):
    r = con.execute("SELECT v FROM meta WHERE k='avg'").fetchone()
    return r[0] if r else LEAGUE_AVG_PTS

def set_meta(con, k, v):
    con.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

# ----------------------------- team crosswalk ---------------------------
FIRST_ID = 100001

def norm(s):
    s = s.replace("St.", "State").replace("&", "and")
    return re.sub(r"[^a-z0-9]", "", s.lower())

def espn_team_id(con, espn_id, name):
    """ESPN is the anchor: its numeric team IDs never change. A new ESPN team gets a
    new permanent ID of ours. Its ESPN name is also stored as an alias."""
    r = con.execute("SELECT team_id FROM team_master WHERE espn_id=?", (str(espn_id),)).fetchone()
    if r:
        tid = r[0]
    else:
        last = con.execute("SELECT MAX(CAST(team_id AS INTEGER)) FROM team_master").fetchone()[0]
        tid = str((last or FIRST_ID - 1) + 1)
        con.execute("INSERT INTO team_master(team_id,name,espn_id) VALUES(?,?,?)",
                    (tid, name, str(espn_id)))
    add_alias(con, "espn", str(espn_id), tid)
    add_alias(con, "espn_name", name, tid)
    return tid

def add_alias(con, source, alias, tid):
    con.execute("""INSERT INTO team_alias(source,alias,team_id) VALUES(?,?,?)
                   ON CONFLICT(source,alias) DO UPDATE SET team_id=excluded.team_id""",
                (source, alias, tid))
    con.execute("DELETE FROM unmatched WHERE source=? AND alias=?", (source, alias))

def resolve(con, source, alias):
    """Return our team ID for a source's name, or None (and log it) if unknown.
    Order: confirmed alias -> known fix -> exact match after cleanup -> unmatched."""
    r = con.execute("SELECT team_id FROM team_alias WHERE source=? AND alias=?",
                    (source, alias)).fetchone()
    if r: return r[0]
    names = con.execute("SELECT alias, team_id FROM team_alias WHERE source='espn_name'").fetchall()
    if source == "torvik" and alias in NAME_FIXES:
        hit = [t for a, t in names if a == NAME_FIXES[alias]]
        if hit: add_alias(con, source, alias, hit[0]); return hit[0]
    # Exact match once punctuation/"St." are cleaned up, against any source's alias
    every = con.execute("SELECT alias, team_id FROM team_alias WHERE source != 'espn'").fetchall()
    hits = {t for a, t in every if norm(a) == norm(alias)}
    if len(hits) == 1:
        tid = hits.pop(); add_alias(con, source, alias, tid); return tid
    # Unknown: log it with a best guess, but don't use it until it's confirmed
    by_norm = {norm(a): (t, a) for a, t in names}
    close = difflib.get_close_matches(norm(alias), list(by_norm), n=1, cutoff=0.6)
    sid, sname = by_norm[close[0]] if close else (None, None)
    con.execute("""INSERT INTO unmatched(source,alias,suggested_id,suggested_name,first_seen)
                   VALUES(?,?,?,?,?) ON CONFLICT(source,alias) DO NOTHING""",
                (source, alias, sid, sname, date.today().isoformat()))
    return None

def team_name(con, tid):
    r = con.execute("SELECT name FROM team_master WHERE team_id=?", (tid,)).fetchone()
    return r[0] if r else str(tid)

def unmatched(con):
    # Clear anything you've since mapped by hand (e.g. a row added in Supabase's table editor)
    con.execute("""DELETE FROM unmatched WHERE EXISTS (SELECT 1 FROM team_alias a
                   WHERE a.source=unmatched.source AND a.alias=unmatched.alias)""")
    return con.execute("""SELECT source, alias, suggested_id, suggested_name FROM unmatched
                          ORDER BY source, alias""").fetchall()

def export_crosswalk(con, path):
    rows = con.execute("""SELECT m.team_id, m.name, a.source, a.alias FROM team_master m
                          JOIN team_alias a ON a.team_id=m.team_id ORDER BY m.name, a.source""").fetchall()
    with open(path, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["team_id", "name", "source", "alias"]); w.writerows(rows)
    return len(rows)

def team(con, name):
    r = con.execute("SELECT off, def, games FROM teams WHERE team=?", (name,)).fetchone()
    if r: return r
    # New team: start from Barttorvik ratings if we have them, otherwise average
    tv = con.execute("SELECT adjoe, adjde, adjt FROM torvik WHERE team=?", (name,)).fetchone()
    avg = con.execute("SELECT v FROM meta WHERE k='tv_eff'").fetchone()
    if tv and avg:
        scale = tv[2] / 100                       # per-100-possession -> per-game points
        off, de, g = (tv[0] - avg[0]) * scale, (avg[0] - tv[1]) * scale, SEEDED_GAMES
    elif avg:                                 # Torvik loaded but no row: non-Division I school
        off, de, g = NON_D1_RATING, NON_D1_RATING, SEEDED_GAMES
    else:
        off, de, g = 0.0, 0.0, 0
    con.execute("INSERT INTO teams(team,off,def,games) VALUES(?,?,?,?)", (name, off, de, g))
    return (off, de, g)

# ----------------------------- data in ----------------------------------
def get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")

def fetch_espn(con, d):
    """Pull games, finals and betting lines for date d (YYYYMMDD) from ESPN."""
    data = json.loads(get(ESPN.format(d=d)))
    n = 0
    for ev in data.get("events", []):
        c = ev["competitions"][0]
        side = {t["homeAway"]: t for t in c["competitors"]}
        def name(s):
            t = side[s]["team"]
            return espn_team_id(con, t["id"], t.get("location") or t["displayName"])
        final = 1 if c["status"]["type"].get("completed") else 0
        hp = int(side["home"].get("score") or 0) if final else None
        ap = int(side["away"].get("score") or 0) if final else None
        spread = total = None
        if c.get("odds"):
            o = c["odds"][0]
            spread, total = o.get("spread"), o.get("overUnder")
        upsert_game(con, ev["id"], d, name("home"), name("away"),
                    int(c.get("neutralSite", False)), hp, ap, final, spread, total)
        n += 1
    con.commit()
    return n

def upsert_game(con, gid, d, home, away, neutral, hp, ap, final, spread, total):
    con.execute("""INSERT INTO games(game_id,date,home,away,neutral,home_pts,away_pts,final,
                   vegas_spread,vegas_total) VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(game_id) DO UPDATE SET home_pts=excluded.home_pts,
                   away_pts=excluded.away_pts, final=excluded.final, neutral=excluded.neutral,
                   vegas_spread=COALESCE(games.vegas_spread, excluded.vegas_spread),
                   vegas_total=COALESCE(games.vegas_total, excluded.vegas_total)""",
                (gid, d, home, away, neutral, hp, ap, final, spread, total))

def import_lines(con, path):
    """Team names in the CSV go through the crosswalk (source "lines")."""
    n = 0
    for row in csv.DictReader(open(path)):
        h, a = resolve(con, "lines", row["home"]), resolve(con, "lines", row["away"])
        if not (h and a): continue             # logged in unmatched; rerun after linking
        n += con.execute("""UPDATE games SET vegas_spread=?, vegas_total=?
                            WHERE date=? AND home=? AND away=?""",
                         (float(row["spread"]), float(row["total"]),
                          row["date"], h, a)).rowcount
    con.commit()
    return n

# ----------------------------- Barttorvik -------------------------------
def load_torvik(con, path=None):
    """Pull Torvik's team ratings (or read a CSV you downloaded by hand)."""
    local = path or f"torvik_{season_year()}.csv"
    if os.path.exists(local):
        text = open(local).read()             # a file you downloaded and uploaded to the repo
    else:
        try:
            text = get(TORVIK.format(y=season_year()))
        except Exception as e:
            print(f"Torvik download failed ({e}) and no {local} in the repo; skipping Torvik")
            return 0
    rows = [r for r in csv.DictReader(io.StringIO(text)) if r.get("team")]
    for r in rows:
        tid = resolve(con, "torvik", r["team"])
        if not tid: continue                   # logged in unmatched until you link it
        if r.get("conf"):
            con.execute("UPDATE team_master SET conf=? WHERE team_id=?", (r["conf"], tid))
        con.execute("""INSERT INTO torvik(team,torvik_name,adjoe,adjde,adjt,barthag,updated)
                       VALUES(?,?,?,?,?,?,?) ON CONFLICT(team) DO UPDATE SET
                       torvik_name=excluded.torvik_name, adjoe=excluded.adjoe, adjde=excluded.adjde,
                       adjt=excluded.adjt, barthag=excluded.barthag, updated=excluded.updated""",
                    (tid, r["team"], float(r["adjoe"]), float(r["adjde"]),
                     float(r["adjt"]), float(r["barthag"]), date.today().isoformat()))
    set_meta(con, "tv_eff", sum(float(r["adjoe"]) for r in rows) / len(rows))
    set_meta(con, "tv_tempo", sum(float(r["adjt"]) for r in rows) / len(rows))
    con.commit()
    return len(rows)

def torvik_predict(con, home, away, neutral):
    """Rebuild a T-Rank-style line from Torvik's ratings. None if a team is missing."""
    h = con.execute("SELECT adjoe,adjde,adjt FROM torvik WHERE team=?", (home,)).fetchone()
    a = con.execute("SELECT adjoe,adjde,adjt FROM torvik WHERE team=?", (away,)).fetchone()
    eff = con.execute("SELECT v FROM meta WHERE k='tv_eff'").fetchone()
    tmp = con.execute("SELECT v FROM meta WHERE k='tv_tempo'").fetchone()
    if not (h and a and eff and tmp): return None
    poss = h[2] * a[2] / tmp[0]
    edge = 0 if neutral else TORVIK_HOME_EDGE
    ph = h[0] * a[1] / eff[0] * poss / 100 * (1 + edge)
    pa = a[0] * h[1] / eff[0] * poss / 100 * (1 - edge)
    return ph, pa

# ----------------------------- the model --------------------------------
def predict_game(con, home, away, neutral):
    avg = league_avg(con)
    ho, hd, _ = team(con, home); ao, ad, _ = team(con, away)
    hca = 0 if neutral else HOME_COURT
    return avg + ho - ad + hca / 2, avg + ao - hd - hca / 2

def tier(edge, tiers):
    for lo, label in tiers:
        if abs(edge) >= lo: return label
    return None

def predict_date(con, d):
    rows = con.execute("""SELECT game_id,home,away,neutral,vegas_spread,vegas_total FROM games
                          WHERE date=? AND graded=0""", (d,)).fetchall()
    for gid, home, away, neu, vs, vt in rows:
        ph, pa = predict_game(con, home, away, neu)
        ps, pt = -(ph - pa), ph + pa
        tv = torvik_predict(con, home, away, neu)
        th, ta = tv if tv else (None, None)
        ts, tt_ = (-(th - ta), th + ta) if tv else (None, None)
        se = st = sp = te = tt = tp = None
        if vs is not None:
            se = vs - ps                       # >0: model likes home more than Vegas
            st = tier(se, SPREAD_TIERS)
            sp = (home if se > 0 else away) if st else None
        if vt is not None:
            te = pt - vt
            tt = tier(te, TOTAL_TIERS)
            tp = ("OVER" if te > 0 else "UNDER") if tt else None
        con.execute("""UPDATE games SET pred_home=?,pred_away=?,pred_spread=?,pred_total=?,
                       tv_home=?,tv_away=?,tv_spread=?,tv_total=?,
                       spread_edge=?,spread_tier=?,spread_pick=?,total_edge=?,total_tier=?,total_pick=?
                       WHERE game_id=?""",
                    (ph, pa, ps, pt, th, ta, ts, tt_, se, st, sp, te, tt, tp, gid))
    con.commit()
    return len(rows)

def grade_date(con, d):
    """Update team ratings from finals. Predictions are stored before tip-off."""
    if con.execute("SELECT COUNT(*) FROM games WHERE date=? AND final=1 AND pred_home IS NULL",
                   (d,)).fetchone()[0]:
        predict_date(con, d)                   # backfill: predict before learning from it
    rows = con.execute("""SELECT game_id,home,away,neutral,home_pts,away_pts FROM games
                          WHERE date=? AND final=1 AND graded=0""", (d,)).fetchall()
    for gid, home, away, neu, hp, ap in rows:
        ph, pa = predict_game(con, home, away, neu)
        eh, ea = hp - ph, ap - pa
        for name, err_for, err_against in ((home, eh, ea), (away, ea, eh)):
            o, de, g = team(con, name)
            k = max(K_MIN, K_START / (1 + g / K_DECAY_GAMES))
            con.execute("UPDATE teams SET off=?, def=?, games=games+1 WHERE team=?",
                        (o + k * err_for, de - k * err_against, name))
        avg = league_avg(con)
        set_meta(con, "avg", avg + 0.01 * ((hp + ap) / 2 - avg))
        con.execute("UPDATE games SET graded=1 WHERE game_id=?", (gid,))
    con.commit()
    return len(rows)

# ----------------------------- output -----------------------------------
def slate(con, d):
    rows = con.execute("""SELECT home,away,pred_home,pred_away,tv_spread,vegas_spread,pred_spread,
                          spread_tier,spread_pick,tv_total,vegas_total,pred_total,total_tier,total_pick
                          FROM games WHERE date=?
                          ORDER BY (spread_tier IS NULL AND total_tier IS NULL), home""",
                       (d,)).fetchall()
    f = lambda x: "-" if x is None else f"{x:+.1f}"
    g = lambda x: "-" if x is None else f"{x:.1f}"
    out = [f"=== Slate {d}: {len(rows)} games ===",
           f"{'Matchup':40} {'Model':>11} | {'Torvik':>6} {'Vegas':>6} {'Model':>6} {'Spread flag':>20} | "
           f"{'Torvik':>6} {'Vegas':>6} {'Model':>6} {'Total flag':>12}"]
    for h, a, ph, pa, tvs, vs, ps, st, sp, tvt, vt, pt, tt, tp in rows:
        h, a = team_name(con, h), team_name(con, a)
        sp = team_name(con, sp) if sp else sp
        out.append(f"{(a[:18]+' @ '+h[:19]):40} {pa:5.1f}-{ph:<5.1f} | {f(tvs):>6} {f(vs):>6} "
                   f"{f(ps):>6} {(st+' '+sp[:13]) if st else '':>20} | {g(tvt):>6} {g(vt):>6} "
                   f"{g(pt):>6} {(tt+' '+tp) if tt else '':>12}")
    return "\n".join(out)

def report(con):
    def miss(col_s, col_t):
        return con.execute(f"""SELECT COUNT(*), AVG(ABS((home_pts-away_pts)+{col_s})),
                               AVG(ABS((home_pts+away_pts)-{col_t})) FROM games
                               WHERE final=1 AND {col_s} IS NOT NULL AND {col_t} IS NOT NULL""").fetchone()
    out = ["=== Season accuracy: average miss in points (lower is better) ==="]
    for label, s, t in (("Our model", "pred_spread", "pred_total"),
                        ("Torvik", "tv_spread", "tv_total"),
                        ("Vegas", "vegas_spread", "vegas_total")):
        n, ms, mt = miss(s, t)
        out.append(f"{label:10} spread {ms or 0:5.2f}   total {mt or 0:5.2f}   ({n} games)")
    out.append("=== Record by edge tier vs Vegas (pushes excluded) ===")
    for kind, col, tiers in (("SPREAD", "spread", SPREAD_TIERS), ("TOTAL", "total", TOTAL_TIERS)):
        for _, label in tiers:
            rows = con.execute(f"""SELECT home,home_pts,away_pts,vegas_spread,vegas_total,{col}_pick
                                   FROM games WHERE final=1 AND {col}_tier=?""", (label,)).fetchall()
            w = l = 0
            for home, hp, ap, vs, vt, pick in rows:
                if kind == "SPREAD":
                    res = ((hp - ap) + vs) * (1 if pick == home else -1)
                else:
                    res = (hp + ap - vt) * (1 if pick == "OVER" else -1)
                w += res > 0; l += res < 0
            pct = f"{100*w/(w+l):.1f}%" if w + l else "-"
            out.append(f"{kind:6} {label:6}: {w}-{l}  ({pct})   breakeven at -110 is 52.4%")
    return "\n".join(out)

# ----------------------------- CLI --------------------------------------
def unmatched_text(con):
    rows = unmatched(con)
    if not rows: return "All team names are mapped."
    out = [f"{len(rows)} team names need a mapping. Confirm each with:",
           '  python cbb_model.py link SOURCE "NAME" TEAM_ID   (or "accept" to take all suggestions)']
    for src, alias, sid, sname in rows:
        guess = f"suggested {sid} ({sname})" if sid else "no suggestion; use 'find' to look it up"
        out.append(f"  {src:8} {alias!r:32} {guess}")
    return "\n".join(out)

def main():
    con = DB()
    args = sys.argv[1:] or ["daily"]
    cmd, arg = args[0], (args[1] if len(args) > 1 else None)
    if cmd == "fetch":       print(fetch_espn(con, arg), "games")
    elif cmd == "grade":     print(grade_date(con, arg), "games graded")
    elif cmd == "predict":   predict_date(con, arg); print(slate(con, arg))
    elif cmd == "torvik":    print(load_torvik(con, arg), "Torvik teams loaded"); print(unmatched_text(con))
    elif cmd == "lines":     print(import_lines(con, arg), "lines updated"); print(unmatched_text(con))
    elif cmd == "report":    print(report(con))
    elif cmd == "unmatched": print(unmatched_text(con))
    elif cmd == "link":
        src, alias, tid = args[1], args[2], args[3]
        if not con.execute("SELECT 1 FROM team_master WHERE team_id=?", (tid,)).fetchone():
            sys.exit(f"No team with ID {tid}. Use: python cbb_model.py find NAME")
        add_alias(con, src, alias, tid); con.commit()
        print(f"Linked {src} {alias!r} -> {tid} ({team_name(con, tid)})")
    elif cmd == "accept":
        rows = [r for r in unmatched(con) if r[2]]
        for src, alias, sid, sname in rows:
            add_alias(con, src, alias, sid); print(f"Linked {src} {alias!r} -> {sid} ({sname})")
        con.commit()
    elif cmd == "find":
        q = norm(" ".join(args[1:]))
        for tid, name, conf in con.execute("SELECT team_id,name,conf FROM team_master ORDER BY name").fetchall():
            if q in norm(name): print(tid, name, conf or "")
    elif cmd == "crosswalk": print(export_crosswalk(con, arg or "crosswalk.csv"), "rows written")
    elif cmd == "backfill":                    # e.g. backfill 20261102: load every day up to yesterday
        d = date(int(arg[:4]), int(arg[4:6]), int(arg[6:]))
        if "reset" in args:                    # start the season over: clear ratings and grades
            con.execute("DELETE FROM teams"); con.execute("DELETE FROM meta WHERE k='avg'")
            con.execute("""UPDATE games SET graded=0, pred_home=NULL, pred_away=NULL,
                           pred_spread=NULL, pred_total=NULL, tv_home=NULL, tv_away=NULL,
                           tv_spread=NULL, tv_total=NULL, spread_tier=NULL, spread_pick=NULL,
                           total_tier=NULL, total_pick=NULL""")
            print("Ratings reset; rebuilding from", arg)
        load_torvik(con)
        while d < date.today():
            ds = d.strftime("%Y%m%d"); n = fetch_espn(con, ds)
            if n and d == date.today() - timedelta(days=1): load_torvik(con)
            print(ds, n, "games,", grade_date(con, ds), "graded"); d += timedelta(days=1)
        print(report(con)); print(unmatched_text(con))
    elif cmd == "daily":
        today = date.today(); yday = today - timedelta(days=1)
        y, t = yday.strftime("%Y%m%d"), today.strftime("%Y%m%d")
        fetch_espn(con, y); fetch_espn(con, t)  # ESPN first: it creates the team IDs
        load_torvik(con)
        grade_date(con, y)                     # finals in, ratings updated
        predict_date(con, t)                   # today's slate + lines + flags
        text = slate(con, t) + "\n\n" + report(con)
        if unmatched(con): text += "\n\n" + unmatched_text(con)
        con.commit()
        print(text)
        open(f"report_{t}.txt", "w").write(text)
    else: print(__doc__)

if __name__ == "__main__":
    main()
