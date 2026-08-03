#!/usr/bin/env python3
# NQGEX collector — computation engine + SQLite logger + levels.json / levels.txt
# + self-contained dashboard renderer.
#
# Methodology: NAIVE GEX BLOCK v2 (live-verified 2026-07-31).
# Formulas are frozen; do not change without explicit reason.
# Assumption: dealers long calls / short puts (textbook). NOT dealer-calibrated.
# All translated levels are zones (±~20 NQ pts), never ticks.
#
# Usage:
#   py collector.py                     one snapshot (cron-friendly), chain TTL 45 min
#   py collector.py --loop 5            re-run every 5 min; chain TTL tracks the
#                                       interval, so every tick pulls a fresh chain
#   py collector.py --loop 5 --rth-only
#       gate to 07:00-16:05 ET: sleeps until open if early, exits after close
#       (Brisbane owner: schedule Mon-Fri 20:55 AEST; the ET gate absorbs US DST)

import argparse
import json
import math
import os
import re
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET   = ZoneInfo("America/New_York")
R    = 0.04     # short-rate approx; gamma is insensitive at short DTE
BAND = 0.05     # wall/ladder band: +/-5% of spot
GRID = 0.07     # flip search: +/-7% of spot
STEP = 0.0025   # grid step 0.25%
NLAD = 6        # ladder rows: cap on named levels beyond the walls
NOTABLE = 0.25  # secondary wall must carry >=25% of its own side's primary wall
LADMIN = 0.15   # ladder level must carry >=15% of the band's biggest bar
LADSEP = 2      # ladder spacing, in median strike gaps (see the ladder block)
OCC  = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")
UA   = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

BASE        = Path(__file__).resolve().parent
CACHE_DIR   = BASE / "cache"
DB_PATH     = BASE / "nqgex.db"
LEVELS_JSON = BASE / "levels.json"
LEVELS_TXT  = BASE / "levels.txt"
TV_LEVELS   = BASE / "tv_levels.txt"     # one-line paste string for the Pine indicator
TEMPLATE    = BASE / "dashboard_template.html"
DASH_HTML   = BASE / "dashboard.html"
RUNS_LOG    = BASE / "runs.log"

SYMBOLS = (("QQQ", "NQ"),)   # (chain symbol, futures label); _NDX/_SPX are roadmap

# Session window (ET): premarket from 07:00 — overnight OCC settlement lands
# ~09:00 so the morning runs catch fresh OI before the 09:30 open; last snapshots
# capture the 16:00 close marks. (09:25 -> 08:30 owner 2026-08-01; -> 07:00
# owner 2026-08-03. The dashboard drift window WIN0 tracks this — keep them
# in step or early snapshots vanish from the chart.)
RTH_OPEN  = (7, 0)
RTH_CLOSE = (16, 5)


# ---------------------------------------------------------------- data fetch

def chain_stamp(doc):
    """Cboe's own top-level "timestamp" — the moment CDN-side generated the
    payload, in UTC. Verified 2026-08-03 against the response's Last-Modified
    header (they agree within 2 s), so this is the honest "as of" for the map,
    not our download time and not our compute time. The quotes *inside* it are
    a further ~15 min delayed at source; that is a separate, additive lag.
    Returns an aware UTC datetime, or None if the field is missing/unparseable."""
    s = doc.get("timestamp") if isinstance(doc, dict) else None
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def load_chain(sym, ttl_min):
    """Cboe delayed chain with a local file cache. Returns (data, meta).
    meta surfaces exactly what happened: fresh fetch, warm cache, stale-cache
    fallback after a failed fetch, or hard failure (data=None) — plus Cboe's own
    payload timestamp so the UI can report the age of the DATA, not of the run."""
    CACHE_DIR.mkdir(exist_ok=True)
    f = CACHE_DIR / f"cboe_{sym}.json"
    meta = {"ok": False, "source": None, "age_min": None, "error": None,
            "stamp_utc": None, "data_age_min": None, "bytes": None}

    def _stamp(doc, now=None):
        st = chain_stamp(doc)
        if st:
            meta["stamp_utc"] = st.isoformat()
            meta["data_age_min"] = round(
                ((now or datetime.now(timezone.utc)) - st).total_seconds() / 60, 1)

    etag_f = CACHE_DIR / f"cboe_{sym}.etag"
    have_cache = f.exists()
    age_min = (time.time() - f.stat().st_mtime) / 60 if have_cache else None
    if not (have_cache and age_min < ttl_min):
        url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
        headers = dict(UA)
        # Conditional GET: Cboe's CDN honours If-None-Match and answers 304 with
        # a zero-byte body (verified 2026-08-03), and the chain regenerates on an
        # irregular schedule — 30 min between two observed premarket stamps, then
        # 10. So polling often is cheap and picking a fixed interval is guesswork:
        # ask every tick, pay the 5.5 MB only when the chain has actually moved.
        if have_cache and etag_f.exists():
            try:
                tag = etag_f.read_text(encoding="utf-8").strip()
                if tag:
                    headers["If-None-Match"] = tag
            except OSError:
                pass
        try:
            resp = urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=90)
            data = resp.read()
            # validate BEFORE committing to cache — a 200-with-HTML body must
            # not poison a good stale cache (that fallback is the whole point)
            doc = json.loads(data)
            parsed = doc["data"]
            tmp = f.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, f)
            tag = resp.headers.get("ETag")
            if tag:
                try:
                    etag_f.write_text(tag, encoding="utf-8")
                except OSError:
                    pass                      # revalidation is an optimisation only
            meta.update(ok=True, source="fresh", age_min=0.0, bytes=len(data))
            _stamp(doc)
            return parsed, meta
        except urllib.error.HTTPError as e:
            if e.code == 304 and have_cache:
                # Cboe has published nothing newer. Deliberately do NOT touch the
                # cache mtime: leaving it stale is what makes the next tick ask
                # again instead of sitting inside its own TTL.
                meta.update(ok=True, source="unchanged", age_min=round(age_min, 1),
                            bytes=0)
            else:
                meta["error"] = f"HTTP {e.code}"
                if have_cache:
                    meta.update(ok=True, source="stale_cache", age_min=round(age_min, 1))
                else:
                    return None, meta
        except Exception as e:
            meta["error"] = f"{type(e).__name__}: {e}"
            if have_cache:  # degrade gracefully, loudly
                meta.update(ok=True, source="stale_cache", age_min=round(age_min, 1))
            else:
                return None, meta
    else:
        meta.update(ok=True, source="cache", age_min=round(age_min, 1))
    try:
        with open(f, encoding="utf-8") as fh:
            doc = json.load(fh)
        _stamp(doc)
        return doc["data"], meta
    except Exception as e:
        meta.update(ok=False, error=f"cache parse: {type(e).__name__}: {e}")
        return None, meta


def futures_last():
    """One keyless CNBC quote call for @ND.1 (NOT @NQ.1). Returns (last, error)."""
    try:
        url = ("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
               "?symbols=%40ND.1&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1&output=json")
        q = json.loads(urllib.request.urlopen(
            urllib.request.Request(url, headers=UA), timeout=30).read())
        q = q["FormattedQuoteResult"]["FormattedQuote"]
        q = [q] if isinstance(q, dict) else q
        d = {r["symbol"]: float(str(r["last"]).replace(",", "")) for r in q if r.get("last")}
        last = d.get("@ND.1")
        return (last, None) if last else (None, "no @ND.1 last in response")
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- engine math

def bs_gamma(S, K, T, v):
    if T <= 0 or v <= 0:
        return 0.0
    d1 = (math.log(S / K) + (R + 0.5 * v * v) * T) / (v * math.sqrt(T))
    return math.exp(-0.5 * d1 * d1) / (math.sqrt(2 * math.pi) * S * v * math.sqrt(T))


def third_friday(d):
    return d.weekday() == 4 and 15 <= d.day <= 21


def compute(sym, chain, fut, fx):
    """The verified engine, returning a structured snapshot instead of printing."""
    spot = chain.get("current_price") or chain.get("close")
    if not spot:
        raise ValueError("chain has no current_price/close")
    now = datetime.now(ET)
    rows = []
    for o in chain["options"]:
        m = OCC.match(o.get("option", ""))
        if not m:
            continue
        _, ymd, cp, k8 = m.groups()
        K = int(k8) / 1000.0
        exp = datetime.strptime(ymd, "%y%m%d").replace(hour=16, tzinfo=ET)
        T = (exp - now).total_seconds() / (365 * 24 * 3600)
        oi = o.get("open_interest") or 0
        if T <= 0 or oi <= 0:
            continue
        iv = o.get("iv") or 0.0
        if iv > 3:
            iv /= 100.0
        rows.append(dict(K=K, cp=cp, exp=exp.date(), T=T, oi=oi, iv=iv,
                         g=o.get("gamma") or 0.0, de=o.get("delta") or 0.0))
    if not rows:
        raise ValueError("no usable contracts after parse")

    exps = sorted({r["exp"] for r in rows})
    front = exps[:2]
    monthly = next((e for e in exps if third_friday(e)), None)
    sel = set(front) | ({monthly} if monthly else set())
    allrows = rows                                   # full chain, pre-selection (IV30)
    rows = [r for r in rows if r["exp"] in sel]
    sgn = lambda r: 1.0 if r["cp"] == "C" else -1.0

    mult = (fut / spot) if fut else None

    # headline net GEX ($ per 1% move): gamma * OI * 100sh * spot^2 * 1% == gamma*OI*spot^2
    net = sum(sgn(r) * r["g"] * r["oi"] for r in rows) * spot * spot
    sub = {}
    for r in rows:
        sub[r["exp"]] = sub.get(r["exp"], 0.0) + sgn(r) * r["g"] * r["oi"] * spot * spot

    # per-strike gamma-weighted map within +/-BAND (full map, not just the ladder)
    lo, hi = spot * (1 - BAND), spot * (1 + BAND)
    per = {}
    for r in rows:
        if lo <= r["K"] <= hi:
            c, p = per.get(r["K"], (0.0, 0.0))
            v = r["g"] * r["oi"] * spot * spot
            per[r["K"]] = (c + v, p) if r["cp"] == "C" else (c, p + v)
    netK = {k: per[k][0] - per[k][1] for k in per}
    above = {k: v for k, v in per.items() if k >= spot} or per
    below = {k: v for k, v in per.items() if k <= spot} or per
    call_wall = max(above, key=lambda k: above[k][0]) if per else None
    put_wall  = max(below, key=lambda k: below[k][1]) if per else None

    # naive flip: BS gamma re-priced across a spot grid, zero crossing nearest spot
    liv = [r for r in rows if r["iv"] > 0]

    def net_at(S):
        return sum(sgn(r) * bs_gamma(S, r["K"], r["T"], r["iv"]) * r["oi"] for r in liv) * S * S

    n = int(GRID / STEP)
    pts = [(S, net_at(S)) for S in (spot * (1 + i * STEP) for i in range(-n, n + 1))]
    cross = []
    for (s1, g1), (s2, g2) in zip(pts, pts[1:]):
        if g1 == 0:
            cross.append(s1)
        elif g1 * g2 < 0:
            cross.append(s1 + (s2 - s1) * g1 / (g1 - g2))
    flip = min(cross, key=lambda s: abs(s - spot)) if cross else None
    bs_at_spot = net_at(spot)

    # secondary walls — same-side concentrations sitting on the "wrong" side of
    # the naive zero-gamma (flip) level: put mass ABOVE zero gamma, call mass
    # BELOW it. These are the exception to the usual shape, so each must carry
    # at least NOTABLE of its own side's primary wall to earn a name; below that
    # it is ordinary ladder structure and stays unnamed. Falls back to spot as
    # the reference when no flip exists inside the grid.
    zg = flip if flip is not None else spot

    # A secondary wall must also be its own level, not a relabelling of one the
    # chart already carries: it has to clear the median strike gap from the flip
    # and from both primary walls. For QQQ that gap is ~1 pt ~= 41 NQ pts, i.e.
    # a full zone diameter under the ±~20 pt doctrine — self-calibrating if a
    # wider-strike chain (NDX/SPX) is ever added.
    ks = sorted(per)
    gaps = sorted(b - a for a, b in zip(ks, ks[1:]))
    sep = gaps[len(gaps) // 2] if gaps else 0.0
    drawn = [x for x in (flip, call_wall, put_wall) if x is not None]
    # a one-strike band leaves sep at 0.0, which would let the separation test
    # pass at zero distance — exclude the primaries by identity as well
    wall_ks = {x for x in (call_wall, put_wall) if x is not None}

    def secondary(idx, keep, primary):
        # Separation is a DRAWING rule, so it vetoes the winner — it must never
        # re-run the max over a filtered field. `flip` drifts continuously while
        # strikes are discrete, so filtering first lets a sub-strike drift in the
        # flip knock the heaviest strike out of contention and crown a far weaker
        # distant one: verified on real snapshots, PWA hopped 692 -> 695 (174 NQ
        # pts) onto 43% less put gamma with a bit-identical gamma map. Withhold
        # the level instead — None is a supported state everywhere downstream.
        cand = [k for k in per if keep(k) and per[k][idx] > 0 and k not in wall_ks]
        if not cand:
            return None
        k = max(cand, key=lambda x: per[x][idx])
        if any(abs(k - d) < sep for d in drawn):
            return None                      # merges into a level already drawn
        floor = NOTABLE * per[primary][idx] if primary is not None else 0.0
        return k if per[k][idx] >= floor else None

    put_wall_hi  = secondary(1, lambda k: k > zg, put_wall)   # puts above zero gamma
    call_wall_lo = secondary(0, lambda k: k < zg, call_wall)  # calls below zero gamma

    put_oi  = sum(r["oi"] for r in rows if r["cp"] == "P")
    call_oi = sum(r["oi"] for r in rows if r["cp"] == "C")

    # naive DEX: OI-weighted delta notional, natural signs (puts negative)
    dex = sum(r["de"] * r["oi"] for r in rows) * 100 * spot

    # naive expected move: front-expiry ATM IV * spot * sqrt(T)
    em = atm_iv = front_T = None
    front_exp = min(sel) if sel else None
    fr = [r for r in rows if r["exp"] == front_exp and r["iv"] > 0]
    if fr:
        atmK = min({r["K"] for r in fr}, key=lambda k: abs(k - spot))
        ivs = [r["iv"] for r in fr if r["K"] == atmK]
        Tf  = next(r["T"] for r in fr if r["K"] == atmK)
        em  = spot * (sum(ivs) / len(ivs)) * math.sqrt(Tf)
        atm_iv, front_T = sum(ivs) / len(ivs), Tf

    # IV30 — 30-day ATM IV via total-variance interpolation between the two
    # expiries bracketing 30 days, from the full chain. CONTEXT METRIC ONLY:
    # it's the number comparable to composite IVs on sites like Barchart
    # (which blend expiries/strikes and read above short-dated ATM). The
    # expected move stays on front-expiry ATM IV — do not substitute.
    iv30 = None
    T30 = 30.0 / 365.0
    exp_iv = []
    for e in sorted({r["exp"] for r in allrows}):
        er = [r for r in allrows if r["exp"] == e and r["iv"] > 0]
        if not er:
            continue
        k = min({r["K"] for r in er}, key=lambda x: abs(x - spot))
        ivs_e = [r["iv"] for r in er if r["K"] == k]
        T_e = next(r["T"] for r in er if r["K"] == k)
        exp_iv.append((T_e, sum(ivs_e) / len(ivs_e)))
    below = [x for x in exp_iv if x[0] <= T30]
    above = [x for x in exp_iv if x[0] > T30]
    if below and above:                              # bracketed only — no extrapolation
        (t1, v1), (t2, v2) = below[-1], above[0]
        var30 = (v1*v1*t1*(t2-T30) + v2*v2*t2*(T30-t1)) / (T30 * (t2 - t1))
        if var30 > 0:
            iv30 = math.sqrt(var30)

    # γ-ladder — the strikes that visibly STAND OUT, ranked by TOTAL gamma
    # (call + put), not by |net GEX|.
    #
    # |net| was the original rule and it demonstrably selects noise. Measured
    # 2026-08-03: it named 696 / 697 / 698 (total 0.166 / 0.135 / 0.146B — three
    # of the smallest bars in the band, and all within three strikes of each
    # other) while skipping 685 (0.257B), 710 (0.212B) and 693 (0.202B). Cause:
    # far above spot the put side is ~0.01B, so net ≈ call and a small one-sided
    # strike outranks a big balanced one. Since |net| <= total by construction,
    # ranking on total can never drop a high-|net| strike that isn't also
    # high-total — prominence strictly dominates the rule it replaces.
    #
    # Three gates, all needed: a floor (LADMIN of the band's biggest bar) kills
    # the small stuff; local-peak stops a level landing on the shoulder of a
    # bigger bar; and a spacing rule against every level already placed — walls,
    # flip and earlier ladder entries — stops the adjacent-strike clusters.
    #
    # Ladder spacing is LADSEP × the median gap, not one gap. Near the money QQQ
    # lists 1-pt strikes, so the median gap IS 1 and a bare one-gap rule lets 696
    # sit against 695 — two lines ~41 NQ pts apart, which is one zone diameter
    # under the ±~20 pt doctrine, i.e. visually the same level. Walls keep the
    # tighter one-gap rule on purpose: a wall is worth drawing even when close.
    tot = {k: per[k][0] + per[k][1] for k in per}
    maxtot = max(tot.values()) if tot else 0.0
    ksorted = sorted(per)
    ladder = []
    placed = list(drawn) + [x for x in (put_wall_hi, call_wall_lo) if x is not None]
    for k in sorted(per, key=lambda x: (-tot[x], x)):
        if len(ladder) >= NLAD or maxtot <= 0:
            break
        if tot[k] < LADMIN * maxtot or k in wall_ks:
            continue
        i = ksorted.index(k)
        lo = tot[ksorted[i - 1]] if i else 0.0
        hi = tot[ksorted[i + 1]] if i < len(ksorted) - 1 else 0.0
        if not (tot[k] > lo and tot[k] > hi):
            continue                         # a shoulder, not a peak
        if any(abs(k - d) < LADSEP * sep for d in placed):
            continue
        ladder.append(k)
        placed.append(k)

    return {
        "sym": sym, "fx": fx, "spot": spot, "fut": fut, "mult": mult,
        "net_gex": net, "regime": "POS" if net > 0 else "NEG",
        "warn": (bs_at_spot > 0) != (net > 0),
        "sub": {e.isoformat(): v for e, v in sorted(sub.items())},
        # side = which side's gamma dominates the strike (net = call - put), so
        # every level the app names or draws can say whether it is call- or
        # put-driven instead of arriving as an anonymous price
        "strikes": [{"k": k, "call": per[k][0], "put": per[k][1], "net": netK[k],
                     "side": "C" if netK[k] >= 0 else "P"}
                    for k in sorted(per)],
        "curve": [{"s": s, "net": g} for s, g in pts],
        "flip": flip, "call_wall": call_wall, "put_wall": put_wall,
        "put_wall_hi": put_wall_hi, "call_wall_lo": call_wall_lo,
        "zg_ref": zg, "zg_is_flip": flip is not None,
        "em": em, "front_exp": front_exp.isoformat() if front_exp else None,
        "atm_iv": atm_iv, "front_T": front_T, "iv30": iv30,
        "dex": dex, "pc_oi": (put_oi / call_oi) if call_oi else None,
        "ladder": ladder,
    }


# ------------------------------------------------------------- text renderer
# Mirrors the verified console block so regressions stay visible at a glance.

def render_text(s):
    B = lambda x: f"{x/1e9:+.2f}B"
    mult, fx, sym = s["mult"], s["fx"], s["sym"]
    tr = lambda x: f"{x*mult:,.0f}" if mult else "—"
    warn = "  ⚠ BS-at-spot sign differs from chain-greek net" if s["warn"] else ""
    hdr = (f" → {fx} {s['fut']:,.2f} (×{mult:.3f} live)" if mult
           else f"  ({fx} translation unavailable — {sym}-space only)")
    subs = " · ".join(f"{datetime.fromisoformat(e):%b%d} {B(v)}" for e, v in s["sub"].items())
    flip = s["flip"]
    fs = (f"~{flip:.1f} → {fx} ~{tr(flip)} ({(flip/s['spot']-1)*100:+.1f}%)" if flip
          else f"none in ±{GRID*100:.0f}%")
    out = [
        f"{sym}  spot {s['spot']}{hdr} | net naive GEX {B(s['net_gex'])}/1% "
        f"({s['regime']} gamma) | P/C OI {s['pc_oi']:.2f}{warn}",
        f"     [{subs}]",
        f"     naive flip {fs} | call wall {s['call_wall']:g} → {tr(s['call_wall'])} "
        f"| put wall {s['put_wall']:g} → {tr(s['put_wall'])}",
    ]
    zg_lab = "zero γ" if s.get("zg_is_flip") else "spot (no flip)"
    sec = []
    if s.get("put_wall_hi") is not None:
        sec.append(f"put wall above {zg_lab} {s['put_wall_hi']:g} → {tr(s['put_wall_hi'])}")
    if s.get("call_wall_lo") is not None:
        sec.append(f"call wall below {zg_lab} {s['call_wall_lo']:g} → {tr(s['call_wall_lo'])}")
    if sec:
        out.append("     " + " | ".join(sec))
    out.append(f"     γ-ladder ($B/1%):  strike   {fx+'≈':>8}     call     put      net  side")

    per = {r["k"]: r for r in s["strikes"]}
    marks = [("call wall", s["call_wall"]), ("put wall", s["put_wall"]),
             ("put wall > zero γ", s.get("put_wall_hi")),
             ("call wall < zero γ", s.get("call_wall_lo"))]
    # display set = the prominence-ranked ladder PLUS every named wall (a wall
    # is excluded from the ladder itself, so it has to be added back here)
    show = sorted({k for k in list(s["ladder"]) + [m[1] for m in marks]
                   if k is not None and k in per}, reverse=True)
    for k in show:
        r = per[k]
        hits = [name for name, kk in marks if kk == k]
        tag = ("   ← " + " · ".join(hits)) if hits else ""
        side = "CALL" if r["side"] == "C" else "PUT "
        out.append(f"                       {k:>6g}   {tr(k):>8}   {r['call']/1e9:+6.2f}"
                   f"  {-r['put']/1e9:+6.2f}   {r['net']/1e9:+6.2f}  {side}{tag}")
    if s["em"]:
        fe = datetime.fromisoformat(s["front_exp"])
        emtxt = (f"exp move ({fe:%b%d} ATM-IV) ±{s['em']:.1f} ({s['em']/s['spot']*100:.1f}%)"
                 + (f" → {fx} ±{s['em']*mult:,.0f} pts" if mult else ""))
    else:
        emtxt = "exp move n/a"
    out.append(f"     {emtxt} | naive DEX {s['dex']/1e9:+.1f}B (natural-sign delta notional)")
    if mult:
        out.append(f"     (1 {sym} strike ≈ {mult:,.0f} {fx} pts — translated levels are zones, not ticks)")
    return "\n".join(out)


# ------------------------------------------------------------------- storage

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL, ts_et TEXT NOT NULL, sym TEXT NOT NULL,
            spot REAL, fut REAL, mult REAL, net_gex REAL, flip REAL,
            call_wall REAL, put_wall REAL, em REAL, dex REAL, pc_oi REAL,
            regime TEXT, warn INTEGER, sub_json TEXT, status_json TEXT);
        CREATE TABLE IF NOT EXISTS strikes(
            snap_id INTEGER NOT NULL, K REAL, call_gex REAL, put_gex REAL, net REAL);
        CREATE TABLE IF NOT EXISTS curve(
            snap_id INTEGER NOT NULL, S REAL, net REAL);
        CREATE INDEX IF NOT EXISTS idx_snap_ts  ON snapshots(sym, ts_et);
        CREATE INDEX IF NOT EXISTS idx_strikes  ON strikes(snap_id);
        CREATE INDEX IF NOT EXISTS idx_curve    ON curve(snap_id);
    """)
    # migrations for older databases (call_wall_lo/put_wall_hi added 2026-08-03)
    for col in ("atm_iv", "iv30", "call_wall_lo", "put_wall_hi"):
        try:
            conn.execute(f"ALTER TABLE snapshots ADD COLUMN {col} REAL")
        except sqlite3.OperationalError:
            pass
    return conn


def persist(conn, s, status, ts_utc, ts_et):
    cur = conn.execute(
        """INSERT INTO snapshots(ts_utc, ts_et, sym, spot, fut, mult, net_gex, flip,
           call_wall, put_wall, em, dex, pc_oi, regime, warn, sub_json, status_json,
           atm_iv, iv30, call_wall_lo, put_wall_hi)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ts_utc.isoformat(), ts_et.isoformat(), s["sym"], s["spot"], s["fut"],
         s["mult"], s["net_gex"], s["flip"], s["call_wall"], s["put_wall"],
         s["em"], s["dex"], s["pc_oi"], s["regime"], int(s["warn"]),
         json.dumps(s["sub"]), json.dumps(status), s["atm_iv"], s["iv30"],
         s["call_wall_lo"], s["put_wall_hi"]))
    snap_id = cur.lastrowid
    conn.executemany("INSERT INTO strikes(snap_id, K, call_gex, put_gex, net) VALUES(?,?,?,?,?)",
                     [(snap_id, r["k"], r["call"], r["put"], r["net"]) for r in s["strikes"]])
    conn.executemany("INSERT INTO curve(snap_id, S, net) VALUES(?,?,?)",
                     [(snap_id, p["s"], p["net"]) for p in s["curve"]])
    conn.commit()
    return snap_id


def display_session_date(conn, sym, ts_et):
    """Which ET session the drift view shows: today from RTH_OPEN ET on a weekday,
    otherwise the most recent prior WEEKDAY that has snapshots — so the last NY
    session stays on screen through the evening/weekend until RTH_OPEN next day."""
    if ts_et.weekday() < 5 and (ts_et.hour, ts_et.minute) >= RTH_OPEN:
        return ts_et.date()
    row = conn.execute(
        """SELECT MAX(substr(ts_et,1,10)) FROM snapshots
           WHERE sym=? AND substr(ts_et,1,10) < ?
           AND CAST(strftime('%w', substr(ts_et,1,10)) AS INTEGER) BETWEEN 1 AND 5""",
        (sym, ts_et.date().isoformat())).fetchone()
    if row and row[0]:
        return date.fromisoformat(row[0])
    return ts_et.date()


def session_history(conn, sym, et_date):
    """All of today's (ET) snapshots — the dashboard drift series.
    substr(), not date(): SQLite's date() normalizes offset-bearing timestamps
    to UTC, which would misfile evening-ET runs into the next day's session."""
    rows = conn.execute(
        """SELECT ts_et, spot, fut, mult, net_gex, flip, call_wall, put_wall, em, regime,
                  call_wall_lo, put_wall_hi
           FROM snapshots WHERE sym=? AND substr(ts_et,1,10)=? ORDER BY ts_utc""",
        (sym, et_date.isoformat())).fetchall()
    keys = ("t", "spot", "fut", "mult", "net", "flip", "cw", "pw", "em", "regime",
            "cwlo", "pwhi")
    return [dict(zip(keys, r)) for r in rows]


# ------------------------------------------------------------- file outputs

def atomic_write(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(4):   # a reader (NT8, browser, editor) can hold the target
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(0.25)


def named_walls(s):
    """The named wall levels as (levels.txt key, TV key, strike), skipping any
    secondary wall the engine judged not notable. Shared by both writers so the
    NinjaScript file and the TradingView string can never drift apart."""
    return [(txt, tv, k) for txt, tv, k in (
        ("CALL_WALL",          "CW",  s["call_wall"]),
        ("PUT_WALL",           "PW",  s["put_wall"]),
        ("PUT_WALL_ABOVE_ZG",  "PWA", s.get("put_wall_hi")),
        ("CALL_WALL_BELOW_ZG", "CWB", s.get("call_wall_lo")),
    ) if k is not None]


def ladder_rows(s):
    """Ladder strikes not already emitted as a named wall, each keyed with the
    side whose gamma dominates that strike: L1C = call-driven, L1P = put-driven.
    Numbering follows the engine's importance order (top |net GEX| first), then
    the γ-spikes by total gamma.

    Spikes ride the same L-numbering deliberately (owner, 2026-08-03): they are
    found by a different rule, but on the chart they are just another level and
    get no badge of their own. The engine still records `spikes` separately so
    the tables and the archive can say WHY a level is on the list."""
    side = {r["k"]: r["side"] for r in s["strikes"]}
    named = {k for _, _, k in named_walls(s)}
    out = []
    for k in s["ladder"]:
        if k in named:
            continue
        out.append((f"L{len(out) + 1}{side.get(k, 'C')}", k))
    return out


def write_levels_txt(s, ts_et):
    """NQ-points CSV for the (future) NinjaScript reader. Only called when the
    futures quote succeeded — a stale-but-correct file beats a fresh QQQ-space one."""
    mult, fut = s["mult"], s["fut"]
    nq = lambda x: round(x * mult)
    lines = [f"# NQGEX {ts_et:%Y-%m-%d %H:%M} ET regime={s['regime']}",
             "# *_ZG walls sit on the far side of the zero-gamma (flip) level",
             "# ladder side suffix: L1C = call-driven strike, L1P = put-driven",
             f"SPOT_NQ,{round(fut)}"]
    if s["flip"] is not None:
        lines.append(f"FLIP,{nq(s['flip'])}")
    for key, _, k in named_walls(s):
        lines.append(f"{key},{nq(k)}")
    if s["em"] is not None:
        lines.append(f"EM_HI,{round(fut + s['em'] * mult)}")
        lines.append(f"EM_LO,{round(fut - s['em'] * mult)}")
    for key, k in ladder_rows(s):
        lines.append(f"{key},{nq(k)}")
    atomic_write(LEVELS_TXT, "\n".join(lines) + "\n")


def tv_levels_string(s):
    """One-line KEY=VALUE;… levels string (NQ points) for the GEXYGEN
    TradingView indicator. Pine can't fetch external data, so paste-string is
    the standard TV delivery (SpotGamma-style)."""
    mult, fut = s["mult"], s["fut"]
    nq = lambda x: round(x * mult)
    parts = []
    if s["flip"] is not None:
        parts.append(f"FLIP={nq(s['flip'])}")
    for _, key, k in named_walls(s):
        parts.append(f"{key}={nq(k)}")
    if s["em"] is not None:
        parts.append(f"EMH={round(fut + s['em'] * mult)}")
        parts.append(f"EML={round(fut - s['em'] * mult)}")
    for key, k in ladder_rows(s):
        parts.append(f"{key}={nq(k)}")
    return ";".join(parts)


def build_payload(s, status, history, ts_utc, ts_et):
    ch = status.get("chain") or {}
    stamp = ch.get("stamp_utc")
    return {
        "meta": {
            "app": "GEXYGEN",
            "generated_utc": ts_utc.isoformat(),
            "generated_et": ts_et.isoformat(),
            # Cboe's own payload timestamp — the age the dashboard headlines,
            # because "when we computed" says nothing about how old the map is
            "chain_stamp_utc": stamp,
            "chain_stamp_et": (datetime.fromisoformat(stamp).astimezone(ET).isoformat()
                               if stamp else None),
            "source_delay_min": 15,   # Cboe's own quote delay, on top of the stamp
            "refresh_sec": 120,
            "note": ("naive: textbook dealer assumption (long calls / short puts), "
                     "NOT dealer-calibrated. Levels are zones (±~20 NQ pts), not ticks. "
                     "Context, not trade signals."),
        },
        "status": status,
        "snap": s,
        "history": history,
    }


def write_levels_json(payload):
    atomic_write(LEVELS_JSON, json.dumps(payload, indent=1))


def render_dashboard(payload):
    if not TEMPLATE.exists():
        return f"dashboard template missing ({TEMPLATE.name}) — skipped"
    html = TEMPLATE.read_text(encoding="utf-8")
    blob = json.dumps(payload).replace("</", "<\\/")
    if "__NQGEX_PAYLOAD__" not in html:
        return "template has no __NQGEX_PAYLOAD__ token — skipped"
    atomic_write(DASH_HTML, html.replace("__NQGEX_PAYLOAD__", blob))
    return None


# ------------------------------------------------------------------ RTH gate

def et_now():
    return datetime.now(ET)


def rth_bounds(t):
    o = t.replace(hour=RTH_OPEN[0], minute=RTH_OPEN[1], second=0, microsecond=0)
    c = t.replace(hour=RTH_CLOSE[0], minute=RTH_CLOSE[1], second=0, microsecond=0)
    return o, c


# ------------------------------------------------------------------ one run

def run_once(ttl_min):
    ts_utc = datetime.now(timezone.utc)
    ts_et  = ts_utc.astimezone(ET)
    status = {"run_et": ts_et.isoformat(),
              "chain": None, "quote": None, "levels_txt_written": False, "notes": []}

    fut, qerr = futures_last()
    status["quote"] = {"ok": fut is not None, "error": qerr}
    if fut is None:
        status["notes"].append("futures quote failed — QQQ-space only; levels.txt NOT rewritten")

    wrote_any = False
    for sym, fx in SYMBOLS:
        chain, cmeta = load_chain(sym, ttl_min)
        status["chain"] = cmeta
        if cmeta.get("source") == "stale_cache":
            status["notes"].append(
                f"chain fetch failed ({cmeta['error']}) — using stale cache "
                f"({cmeta['age_min']:.0f} min old)")
        if chain is None:
            status["notes"].append(f"{sym} chain unavailable ({cmeta['error']}) — run skipped")
            break
        try:
            s = compute(sym, chain, fut, fx)
        except Exception as e:
            status["notes"].append(f"{sym} compute failed ({type(e).__name__}: {e}) — run skipped")
            break

        # persistence and file outputs come first — a print failure (encoding,
        # closed pipe) must never cost a persisted snapshot
        tv_string = None
        if s["mult"]:
            try:
                write_levels_txt(s, ts_et)
                tv_string = tv_levels_string(s)
                atomic_write(TV_LEVELS, tv_string + "\n")
                status["levels_txt_written"] = True
            except OSError as e:
                status["notes"].append(f"levels.txt write failed ({type(e).__name__}: {e})")

        conn = db_connect()
        try:
            snap_id = persist(conn, s, status, ts_utc, ts_et)
            hist_date = display_session_date(conn, sym, ts_et)
            history = session_history(conn, sym, hist_date)
            payload = build_payload(s, status, history, ts_utc, ts_et)
            payload["history_date"] = hist_date.isoformat()
            payload["tv_string"] = tv_string
            notes_before = len(status["notes"])
            try:
                write_levels_json(payload)
            except OSError as e:
                status["notes"].append(f"levels.json write failed ({type(e).__name__}: {e})")
            try:
                derr = render_dashboard(payload)
            except OSError as e:
                derr = f"dashboard write failed ({type(e).__name__}: {e})"
            if derr:
                status["notes"].append(derr)
            if len(status["notes"]) != notes_before:   # keep the DB ledger truthful
                conn.execute("UPDATE snapshots SET status_json=? WHERE id=?",
                             (json.dumps(status), snap_id))
                conn.commit()
        finally:
            conn.close()

        print(f"── NAIVE GEX  {ts_et:%Y-%m-%d %H:%M ET}  "
              f"(cached Cboe chains · textbook dealer assumption — not dealer-calibrated)")
        print(f"── {sym}→{fx} via live futures/ETF ratio · NQ = MNQ price — micros differ only in $/point")
        print(render_text(s))
        wrote_any = True

        mult = s["mult"]
        tr = lambda x: f"{round(x*mult)}" if (mult and x is not None) else "—"
        dage = cmeta.get("data_age_min")
        ledger = (f"{ts_et:%Y-%m-%d %H:%M} ET | chain={cmeta['source']}"
                  f"({cmeta['age_min']:.0f}m)"
                  + (f" data={dage:.0f}m" if dage is not None else "")
                  + f" | quote={'ok' if fut else 'FAIL'}"
                  f" | net={s['net_gex']/1e9:+.2f}B {s['regime']}"
                  f" | flip={tr(s['flip'])} cw={tr(s['call_wall'])} pw={tr(s['put_wall'])}"
                  + (f" pwa={tr(s['put_wall_hi'])}" if s['put_wall_hi'] is not None else "")
                  + (f" cwb={tr(s['call_wall_lo'])}" if s['call_wall_lo'] is not None else "")
                  + f" | txt={'yes' if status['levels_txt_written'] else 'no'}"
                  + (" | " + "; ".join(status["notes"]) if status["notes"] else ""))
    if not wrote_any:
        ledger = f"{ts_et:%Y-%m-%d %H:%M} ET | RUN FAILED | " + "; ".join(status["notes"])
    print(f"STATUS  {ledger}")
    try:
        with open(RUNS_LOG, "a", encoding="utf-8") as fh:
            fh.write(ledger + "\n")
    except OSError:
        pass
    return wrote_any


# ---------------------------------------------------------------------- main

def main():
    # Windows redirects stdout as cp1252, which cannot encode γ/⚠/── — a
    # scheduled `>> log` run would crash on the first print without this
    for _s in (sys.stdout, sys.stderr):
        try:
            if _s is not None and hasattr(_s, "reconfigure"):
                _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="NQGEX collector — naive gamma levels for NQ/MNQ from free public data")
    ap.add_argument("--once", action="store_true",
                    help="single snapshot then exit (this is already the default)")
    ap.add_argument("--loop", type=int, metavar="MIN",
                    help="re-run every MIN minutes (chain cache TTL drops to 10 min)")
    ap.add_argument("--rth-only", action="store_true",
                    help="gate to the 07:00-16:05 ET session window; sleep until it "
                         "opens, exit after it closes")
    ap.add_argument("--log", nargs="?", const="runs_console.log", metavar="FILE",
                    help="also append all console output to FILE (default "
                         "runs_console.log) — replaces shell redirection so the "
                         "same command works on any host")
    args = ap.parse_args()

    if args.log:
        class _Tee:
            def __init__(self, *streams): self.streams = streams
            def write(self, s):
                for st in self.streams:
                    try: st.write(s)
                    except Exception: pass
            def flush(self):
                for st in self.streams:
                    try: st.flush()
                    except Exception: pass
        path = Path(args.log)
        if not path.is_absolute():
            path = BASE / path
        fh = open(path, "a", encoding="utf-8", buffering=1)
        sys.stdout = _Tee(sys.stdout, fh)
        sys.stderr = _Tee(sys.stderr, fh)

    # TTL must track the loop interval, not sit at a fixed 10: with a hardcoded
    # TTL a --loop shorter than it would serve cache on alternate ticks and
    # silently halve the real sampling rate. The 1-minute margin absorbs tick
    # jitter so a tick can never land a hair inside the window and skip a fetch.
    ttl = max(0, args.loop - 1) if args.loop else 45

    if args.loop:
        # single-instance guard: shortcut + scheduled task + manual launches
        # must never run two session collectors at once (duplicate fetches,
        # DB contention). Loopback bind = cheap cross-process mutex.
        try:
            _lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            _lock.bind(("127.0.0.1", 47613))
        except OSError:
            print("STATUS  another GEXYGEN collector is already running — exiting")
            return

    while True:
        t = et_now()
        if args.rth_only:
            if t.weekday() >= 5:
                print(f"STATUS  {t:%Y-%m-%d %H:%M} ET is a weekend — exiting")
                return
            o, c = rth_bounds(t)
            if t > c:
                print(f"STATUS  {t:%H:%M} ET is past the {c:%H:%M} close — exiting")
                return
            if t < o:
                wait = (o - t).total_seconds()
                print(f"STATUS  {t:%H:%M} ET — sleeping {wait/60:.0f} min until {o:%H:%M} ET open")
                time.sleep(wait)

        started = time.monotonic()
        try:
            run_once(ttl)
        except Exception as e:
            # never let one bad run kill a session loop; surface it instead
            msg = f"{et_now():%Y-%m-%d %H:%M} ET | RUN CRASHED | {type(e).__name__}: {e}"
            print(f"STATUS  {msg}", file=sys.stderr)
            try:
                with open(RUNS_LOG, "a", encoding="utf-8") as fh:
                    fh.write(msg + "\n")
            except OSError:
                pass
            if not args.loop:
                raise

        if not args.loop:
            return
        if args.rth_only and et_now() + timedelta(minutes=args.loop) > rth_bounds(et_now())[1]:
            print(f"STATUS  next tick would pass the close — exiting")
            return
        time.sleep(max(0, args.loop * 60 - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
