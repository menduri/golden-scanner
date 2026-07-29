"""
Option Chain Engine — Tradier Production API
Implements: Net Premium, GEX, Max Pain, Call Wall, Put Wall,
            LEAPS, Resistance/Support, Bull/Bear Score, Pine Script
All expirations included — no date filtering.
"""

import requests, time, math
from collections import defaultdict

TRADIER_TOKEN = "Vzj4eByTvakHHT4PLg4jl6AjtfZs"
TRADIER_BASE  = "https://api.tradier.com/v1"
HEADERS = {
    "Authorization": f"Bearer {TRADIER_TOKEN}",
    "Accept": "application/json"
}

# ── STEP 1: SPOT PRICE ───────────────────────────────────────────────────────
def get_spot(symbol):
    r = requests.get(f"{TRADIER_BASE}/markets/quotes",
                     params={"symbols": symbol}, headers=HEADERS, timeout=10)
    q = r.json()["quotes"]["quote"]
    price = q.get("last") or q.get("close")
    return float(price)

# ── STEP 2: ALL EXPIRATIONS ──────────────────────────────────────────────────
def get_expirations(symbol):
    r = requests.get(f"{TRADIER_BASE}/markets/options/expirations",
                     params={"symbol": symbol, "includeAllRoots": "true", "strikes": "false"},
                     headers=HEADERS, timeout=10)
    data = r.json()["expirations"]["date"]
    if isinstance(data, str):
        data = [data]
    return data  # ALL expirations — no date filter

# ── STEP 3: OPTION CHAIN PER EXPIRATION ─────────────────────────────────────
def get_chain(symbol, expiration):
    try:
        r = requests.get(f"{TRADIER_BASE}/markets/options/chains",
                         params={"symbol": symbol, "expiration": expiration, "greeks": "true"},
                         headers=HEADERS, timeout=15)
        data = r.json().get("options", {})
        if not data or not data.get("option"):
            return []
        opts = data["option"]
        if isinstance(opts, dict):
            opts = [opts]
        return opts
    except:
        return []

# ── STEP 4 & 5: BUILD STRIKE MAP ─────────────────────────────────────────────
def build_strike_map(symbol, progress_cb=None):
    spot = get_spot(symbol)
    expirations = get_expirations(symbol)

    strike_map = defaultdict(lambda: {
        "cOI":0,"pOI":0,"cVol":0,"pVol":0,
        "cDollar":0,"pDollar":0,
        "cGEX":0,"pGEX":0,
        "cDelta_sum":0,"pDelta_sum":0,
    })

    total_exp = len(expirations)
    for i, exp in enumerate(expirations):
        if progress_cb:
            progress_cb(i+1, total_exp, exp)

        chain = get_chain(symbol, exp)
        time.sleep(0.08)  # 80ms rate limit

        for opt in chain:
            try:
                strike = float(opt.get("strike", 0))
                if strike <= 0: continue

                # Strike range filter: 50% to 200% of spot
                if strike < spot * 0.50 or strike > spot * 2.00:
                    continue

                opt_type = opt.get("option_type", "").lower()
                greeks = opt.get("greeks") or {}

                delta = greeks.get("delta")
                gamma = greeks.get("gamma") or 0
                oi    = opt.get("open_interest") or 0
                ask   = opt.get("ask") or 0
                vol   = opt.get("volume") or 0

                # Data quality filter
                if delta is None or delta == 0.000: continue
                if oi == 0 or ask == 0: continue

                abs_delta = abs(float(delta))
                gamma     = abs(float(gamma))
                oi        = float(oi)
                ask       = float(ask)
                vol       = float(vol)

                dollar = oi * abs_delta * ask * 100

                if opt_type == "call":
                    strike_map[strike]["cOI"]     += oi
                    strike_map[strike]["cVol"]    += vol
                    strike_map[strike]["cDollar"] += dollar
                    strike_map[strike]["cGEX"]    += oi * gamma * 100 * spot
                    strike_map[strike]["cDelta_sum"] += abs_delta
                elif opt_type == "put":
                    strike_map[strike]["pOI"]     += oi
                    strike_map[strike]["pVol"]    += vol
                    strike_map[strike]["pDollar"] += dollar
                    strike_map[strike]["pGEX"]    += oi * gamma * 100 * spot * -1
                    strike_map[strike]["pDelta_sum"] += abs_delta
            except:
                continue

    # Build final records
    records = []
    for strike, d in strike_map.items():
        net_gex    = d["cGEX"] + d["pGEX"]
        total_dollar = d["cDollar"] + d["pDollar"]
        net_dollar = d["cDollar"] - d["pDollar"]
        records.append({
            "strike":      strike,
            "cOI":         d["cOI"],
            "pOI":         d["pOI"],
            "cVol":        d["cVol"],
            "pVol":        d["pVol"],
            "cDollar":     d["cDollar"],
            "pDollar":     d["pDollar"],
            "totalDollar": total_dollar,
            "netDollar":   net_dollar,
            "cGEX":        d["cGEX"],
            "pGEX":        d["pGEX"],
            "nGEX":        net_gex,
        })

    records.sort(key=lambda x: x["strike"])
    return spot, records

# ── CALCULATIONS ─────────────────────────────────────────────────────────────
def calc_gamma_flip(records, spot):
    filtered = [r for r in records if spot*0.6 <= r["strike"] <= spot*1.4]
    filtered.sort(key=lambda x: x["strike"])
    for i in range(len(filtered)-1):
        lo = filtered[i]; hi = filtered[i+1]
        if lo["nGEX"] < 0 and hi["nGEX"] > 0:
            w = abs(lo["nGEX"]) / (abs(lo["nGEX"]) + abs(hi["nGEX"]))
            return lo["strike"] + (hi["strike"] - lo["strike"]) * w
    return spot

def calc_max_pain(records, spot):
    candidates = [r for r in records
                  if (r["cOI"]+r["pOI"]) > 100
                  and spot*0.5 <= r["strike"] <= spot*1.5]
    if not candidates:
        return spot
    best_strike = spot
    best_loss = float("inf")
    for cand in candidates:
        cs = cand["strike"]
        loss = sum(
            r["cOI"] * max(0, r["strike"] - cs) +
            r["pOI"] * max(0, cs - r["strike"])
            for r in candidates
        )
        if loss < best_loss:
            best_loss = loss
            best_strike = cs
    return best_strike

def calc_walls(records, spot):
    above = [r for r in records if r["strike"] > spot and r["strike"] <= spot*2]
    below = [r for r in records if r["strike"] < spot and r["strike"] > spot*0.3]
    call_wall = max(above, key=lambda x: x["totalDollar"])["strike"] if above else spot*1.1
    put_wall  = max(below, key=lambda x: x["totalDollar"])["strike"] if below else spot*0.9
    return call_wall, put_wall

def calc_leaps(records, spot):
    threshold_pct = 0.01 if spot < 20 else 0.06
    above = [r for r in records if r["strike"] > spot*1.10]
    if not above: return []
    max_dollar = max(r["cDollar"] for r in above) if above else 1
    threshold = max_dollar * threshold_pct
    filtered = [r for r in above if r["cDollar"] >= threshold]
    filtered.sort(key=lambda x: x["cDollar"], reverse=True)
    return [r["strike"] for r in filtered[:8]]

def calc_resistance_support(records, spot):
    threshold_pct = 0.01 if spot < 20 else 0.08
    all_dollars = [r["totalDollar"] for r in records]
    max_dollar = max(all_dollars) if all_dollars else 1
    threshold = max_dollar * threshold_pct

    above = [r for r in records if r["strike"] > spot and r["totalDollar"] >= threshold]
    below = [r for r in records if r["strike"] < spot and r["totalDollar"] >= threshold]
    above.sort(key=lambda x: x["totalDollar"], reverse=True)
    below.sort(key=lambda x: x["totalDollar"], reverse=True)
    return [r["strike"] for r in above[:7]], [r["strike"] for r in below[:7]]

def calc_bull_bear_score(records, spot, gf, mp):
    total_gex = sum(r["nGEX"] for r in records)
    total_coi = sum(r["cOI"] for r in records)
    total_poi = sum(r["pOI"] for r in records)
    pc_ratio  = total_poi / total_coi if total_coi > 0 else 1.0

    atm = [r for r in records if spot*0.95 <= r["strike"] <= spot*1.05]
    atm_net = sum(r["netDollar"] for r in atm)

    score = 0
    breakdown = []

    if total_gex > 0:
        score += 2; breakdown.append("GEX Positive +2")
    else:
        breakdown.append("GEX Negative +0")

    if spot > gf:
        score += 2; breakdown.append("Above Gamma Flip +2")
    else:
        breakdown.append("Below Gamma Flip +0")

    if pc_ratio < 0.9:
        score += 2; breakdown.append(f"P/C {pc_ratio:.2f} Bullish +2")
    elif pc_ratio <= 1.1:
        score += 1; breakdown.append(f"P/C {pc_ratio:.2f} Neutral +1")
    else:
        breakdown.append(f"P/C {pc_ratio:.2f} Bearish +0")

    if mp > spot:
        score += 2; breakdown.append(f"Max Pain {mp:.2f} Above +2")
    elif mp >= spot * 0.95:
        score += 1; breakdown.append(f"Max Pain {mp:.2f} Near +1")
    else:
        breakdown.append(f"Max Pain {mp:.2f} Below +0")

    if atm_net > 0:
        score += 2; breakdown.append("ATM Call Dominant +2")
    else:
        breakdown.append("ATM Put Dominant +0")

    labels = {10:"FULL BULL",9:"BULL",8:"BULL",7:"NEUTRAL",6:"NEUTRAL",
              5:"WATCH",4:"WATCH",3:"BEAR",2:"BEAR",1:"BEAR",0:"BEAR"}
    label = labels.get(score, "NEUTRAL")
    return score, label, breakdown, round(pc_ratio, 3)

def get_zone_size(spot):
    if spot > 1000: return 8.0
    if spot > 800:  return 5.0
    if spot > 400:  return 2.0
    if spot > 150:  return 1.5
    if spot > 50:   return 0.80
    if spot > 20:   return 0.40
    if spot > 5:    return 0.20
    return 0.10

def fmt_dollar(v):
    v = abs(v)
    if v >= 1e9: return f"${v/1e9:.2f}B"
    if v >= 1e6: return f"${v/1e6:.2f}M"
    if v >= 1e3: return f"${v/1e3:.1f}K"
    return f"${v:.0f}"

# ── FULL ANALYSIS ─────────────────────────────────────────────────────────────
def full_analysis(symbol, progress_cb=None):
    spot, records = build_strike_map(symbol, progress_cb)
    if not records:
        return None

    gf = calc_gamma_flip(records, spot)
    mp = calc_max_pain(records, spot)
    cw, pw = calc_walls(records, spot)
    leaps = calc_leaps(records, spot)
    resistance, support = calc_resistance_support(records, spot)
    score, label, breakdown, pc = calc_bull_bear_score(records, spot, gf, mp)
    zone = get_zone_size(spot)

    total_cDollar = sum(r["cDollar"] for r in records)
    total_pDollar = sum(r["pDollar"] for r in records)
    net_total     = total_cDollar - total_pDollar

    # Net premium chart data (no binning — exact strikes)
    chart_data = [
        {"strike": r["strike"], "cDollar": r["cDollar"],
         "pDollar": r["pDollar"], "netDollar": r["netDollar"]}
        for r in records if r["cDollar"] > 0 or r["pDollar"] > 0
    ]

    return {
        "symbol":     symbol.upper(),
        "spot":       spot,
        "gf":         round(gf, 2),
        "mp":         round(mp, 2),
        "cw":         round(cw, 2),
        "pw":         round(pw, 2),
        "leaps":      [round(x,2) for x in leaps],
        "resistance": [round(x,2) for x in resistance],
        "support":    [round(x,2) for x in support],
        "score":      score,
        "label":      label,
        "breakdown":  breakdown,
        "pc_ratio":   pc,
        "zone":       zone,
        "total_cDollar": total_cDollar,
        "total_pDollar": total_pDollar,
        "net_total":     net_total,
        "net_total_fmt": fmt_dollar(net_total),
        "call_fmt":      fmt_dollar(total_cDollar),
        "put_fmt":       fmt_dollar(total_pDollar),
        "strike_count":  len(chart_data),
        "chart_data":    chart_data,
    }

# ── PINE SCRIPT GENERATOR ─────────────────────────────────────────────────────
def generate_pine_single(a):
    sym  = a["symbol"]
    zone = a["zone"]
    spot = a["spot"]

    def pct(level):
        diff = ((level - spot) / spot) * 100
        return f"+{diff:.1f}% above" if diff >= 0 else f"{diff:.1f}% below"

    gf_hi = round(a["gf"] + zone/2, 4); gf_lo = round(a["gf"] - zone/2, 4)
    mp_hi = round(a["mp"] + zone/2, 4); mp_lo = round(a["mp"] - zone/2, 4)
    cw_hi = round(a["cw"] + zone/2, 4); cw_lo = round(a["cw"] - zone/2, 4)
    pw_hi = round(a["pw"] + zone/2, 4); pw_lo = round(a["pw"] - zone/2, 4)

    gf_regime = "POS GAMMA" if spot > a["gf"] else "NEG GAMMA"
    gf_lbl = f"GAMMA FLIP ${a['gf']} -- {gf_regime} | {a['score']}/10 {a['label']}"
    mp_sign = "+" if a["mp"] >= spot else ""
    mp_lbl  = f"MAX PAIN ${a['mp']} -- {mp_sign}{((a['mp']-spot)/spot*100):.1f}%"
    cw_lbl  = f"CALL WALL ${a['cw']} -- {pct(a['cw'])}"
    pw_lbl  = f"PUT WALL ${a['pw']} -- {pct(a['pw'])}"

    leaps_block = ""
    for lv in a["leaps"]:
        leaps_block += f"""
    line.new(x1=bar_index, y1={lv}, x2=bar_index+1, y2={lv}, color=color.new(color.aqua,40), width=1, extend=extend.both)
    label.new(bar_index, {lv}, "LEAPS ${lv} -- {pct(lv)}", color=color.new(color.aqua,50), textcolor=color.white, size=size.small, style=label.style_label_right, xloc=xloc.bar_index)"""

    resist_block = ""
    for lv in a["resistance"]:
        resist_block += f"""
    line.new(x1=bar_index, y1={lv}, x2=bar_index+1, y2={lv}, color=color.new(color.orange,40), width=1, extend=extend.both)
    label.new(bar_index, {lv}, "Resistance ${lv} -- {pct(lv)}", color=color.new(color.orange,50), textcolor=color.white, size=size.small, style=label.style_label_right, xloc=xloc.bar_index)"""

    support_block = ""
    for lv in a["support"]:
        support_block += f"""
    line.new(x1=bar_index, y1={lv}, x2=bar_index+1, y2={lv}, color=color.new(color.red,40), width=2, extend=extend.both)
    label.new(bar_index, {lv}, "Support ${lv} -- {pct(lv)}", color=color.new(color.red,50), textcolor=color.white, size=size.small, style=label.style_label_right, xloc=xloc.bar_index)"""

    script = f"""//@version=6
indicator("{sym} — Option Levels | {a['score']}/10 {a['label']}", overlay=true, max_lines_count=500, max_boxes_count=50, max_labels_count=100)

// {sym} | Spot:{spot} | GF:{a['gf']} | MP:{a['mp']} | CW:{a['cw']} | PW:{a['pw']}
// Net:{a['net_total_fmt']} | P/C:{a['pc_ratio']} | Score:{a['score']}/10 {a['label']}

var bool drawn = false
if not drawn
    drawn := true
    // Gamma Flip
    box.new(bar_index, {gf_hi}, bar_index+1, {gf_lo}, bgcolor=color.new(color.aqua,82), border_color=color.new(color.aqua,50), extend=extend.both)
    label.new(bar_index, {a['gf']}, "{gf_lbl}", color=color.new(color.aqua,50), textcolor=color.white, size=size.small, style=label.style_label_right, xloc=xloc.bar_index)
    // Max Pain
    box.new(bar_index, {mp_hi}, bar_index+1, {mp_lo}, bgcolor=color.new(color.yellow,82), border_color=color.new(color.yellow,50), extend=extend.both)
    label.new(bar_index, {a['mp']}, "{mp_lbl}", color=color.new(color.yellow,50), textcolor=color.black, size=size.small, style=label.style_label_right, xloc=xloc.bar_index)
    // Call Wall
    box.new(bar_index, {cw_hi}, bar_index+1, {cw_lo}, bgcolor=color.new(color.green,82), border_color=color.new(color.green,50), extend=extend.both)
    label.new(bar_index, {a['cw']}, "{cw_lbl}", color=color.new(color.green,50), textcolor=color.white, size=size.small, style=label.style_label_right, xloc=xloc.bar_index)
    // Put Wall
    box.new(bar_index, {pw_hi}, bar_index+1, {pw_lo}, bgcolor=color.new(color.red,82), border_color=color.new(color.red,50), extend=extend.both)
    label.new(bar_index, {a['pw']}, "{pw_lbl}", color=color.new(color.red,50), textcolor=color.white, size=size.small, style=label.style_label_right, xloc=xloc.bar_index){leaps_block}{resist_block}{support_block}
"""
    return script


def generate_pine_universal(analyses):
    blocks = []
    for a in analyses:
        sym  = a["symbol"]
        zone = a["zone"]
        spot = a["spot"]

        def pct(level):
            diff = ((level - spot) / spot) * 100
            return f"+{diff:.1f}% above" if diff >= 0 else f"{diff:.1f}% below"

        gf_hi = round(a["gf"] + zone/2, 4); gf_lo = round(a["gf"] - zone/2, 4)
        mp_hi = round(a["mp"] + zone/2, 4); mp_lo = round(a["mp"] - zone/2, 4)
        cw_hi = round(a["cw"] + zone/2, 4); cw_lo = round(a["cw"] - zone/2, 4)
        pw_hi = round(a["pw"] + zone/2, 4); pw_lo = round(a["pw"] - zone/2, 4)

        gf_regime = "POS GAMMA" if spot > a["gf"] else "NEG GAMMA"
        gf_lbl = f"GAMMA FLIP ${a['gf']} -- {gf_regime} | {a['score']}/10 {a['label']}"
        mp_sign = "+" if a["mp"] >= spot else ""
        mp_lbl  = f"MAX PAIN ${a['mp']} -- {mp_sign}{((a['mp']-spot)/spot*100):.1f}%"
        cw_lbl  = f"CALL WALL ${a['cw']} -- {pct(a['cw'])}"
        pw_lbl  = f"PUT WALL ${a['pw']} -- {pct(a['pw'])}"

        leaps_lines = ""
        for lv in a["leaps"]:
            leaps_lines += f"""
        line.new(x1=bar_index, y1={lv}, x2=bar_index+1, y2={lv}, color=color.new(color.aqua,40), width=1, extend=extend.both)
        label.new(bar_index, {lv}, "LEAPS ${lv} -- {pct(lv)}", color=color.new(color.aqua,50), textcolor=color.white, size=size.small, style=label.style_label_right, xloc=xloc.bar_index)"""

        resist_lines = ""
        for lv in a["resistance"]:
            resist_lines += f"""
        line.new(x1=bar_index, y1={lv}, x2=bar_index+1, y2={lv}, color=color.new(color.orange,40), width=1, extend=extend.both)
        label.new(bar_index, {lv}, "Resistance ${lv} -- {pct(lv)}", color=color.new(color.orange,50), textcolor=color.white, size=size.small, style=label.style_label_right, xloc=xloc.bar_index)"""

        support_lines = ""
        for lv in a["support"]:
            support_lines += f"""
        line.new(x1=bar_index, y1={lv}, x2=bar_index+1, y2={lv}, color=color.new(color.red,40), width=2, extend=extend.both)
        label.new(bar_index, {lv}, "Support ${lv} -- {pct(lv)}", color=color.new(color.red,50), textcolor=color.white, size=size.small, style=label.style_label_right, xloc=xloc.bar_index)"""

        block = f"""
    // ── {sym} | {a['score']}/10 {a['label']} | GF:{a['gf']} MP:{a['mp']} CW:{a['cw']} PW:{a['pw']} ──
    if syminfo.ticker == "{sym}"
        box.new(bar_index, {gf_hi}, bar_index+1, {gf_lo}, bgcolor=color.new(color.aqua,82), border_color=color.new(color.aqua,50), extend=extend.both)
        label.new(bar_index, {a['gf']}, "{gf_lbl}", color=color.new(color.aqua,50), textcolor=color.white, size=size.small, style=label.style_label_right, xloc=xloc.bar_index)
        box.new(bar_index, {mp_hi}, bar_index+1, {mp_lo}, bgcolor=color.new(color.yellow,82), border_color=color.new(color.yellow,50), extend=extend.both)
        label.new(bar_index, {a['mp']}, "{mp_lbl}", color=color.new(color.yellow,50), textcolor=color.black, size=size.small, style=label.style_label_right, xloc=xloc.bar_index)
        box.new(bar_index, {cw_hi}, bar_index+1, {cw_lo}, bgcolor=color.new(color.green,82), border_color=color.new(color.green,50), extend=extend.both)
        label.new(bar_index, {a['cw']}, "{cw_lbl}", color=color.new(color.green,50), textcolor=color.white, size=size.small, style=label.style_label_right, xloc=xloc.bar_index)
        box.new(bar_index, {pw_hi}, bar_index+1, {pw_lo}, bgcolor=color.new(color.red,82), border_color=color.new(color.red,50), extend=extend.both)
        label.new(bar_index, {a['pw']}, "{pw_lbl}", color=color.new(color.red,50), textcolor=color.white, size=size.small, style=label.style_label_right, xloc=xloc.bar_index){leaps_lines}{resist_lines}{support_lines}"""
        blocks.append(block)

    tickers = " | ".join([a["symbol"] for a in analyses])
    return f"""//@version=6
indicator("Universal Option Levels — {tickers}", overlay=true, max_lines_count=500, max_boxes_count=200, max_labels_count=500)

var bool drawn = false
if not drawn
    drawn := true
{"".join(blocks)}
"""
