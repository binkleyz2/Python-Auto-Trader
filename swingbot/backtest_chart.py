"""Interactive chart of a backtest's account value, as one HTML file that opens in any browser.

Hover anywhere on the chart: a box at the cursor shows the nearest point's account value, date and time,
and the trade that closed there (symbol, setup, exit price and reason, P&L). There is a point at every
trade's exit, so the line steps through the day, not just from close to close. Drag across the chart to
zoom in on some days; double-click (or "Reset zoom") to see everything again. A --try comparison draws one
line per combination; click names in the legend to show or hide them.

The page has no outside scripts or fonts, so it works offline and straight out of the run's zip. It fills
the browser window, with type sized for a large screen (e.g. 32" at 2560x1440).
"""
from __future__ import annotations

import html
import json
from pathlib import Path

import pandas as pd

SESSION_MIN = 390                      # 9:30 to 16:00
BEST = 10                              # lines shown at first in a comparison (the rest are a click away)


def _minutes(hhmm) -> int:
    try:
        h, m = str(hhmm).split(":")[:2]
        return min(SESSION_MIN, max(0, int(h) * 60 + int(m) - 570))
    except (ValueError, TypeError):
        return SESSION_MIN


def _num(v, nd=2):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f or f in (float("inf"), float("-inf")) else round(f, nd)


def equity_points(trades: pd.DataFrame, days: list[str], start: float) -> list[list]:
    """[x, account value, date, time, symbol, side, setup, exit price, exit reason, P&L, R] for the start and
    after each trade. x counts trading days, with the time of day as the fraction (9:30 = .0, 16:00 = 1.0)."""
    idx = {d: i for i, d in enumerate(days)}
    pts = [[0.0, round(start, 2), days[0] if days else "", "09:30", "", "", "", None, "", None, None]]
    eq = float(start)
    if len(trades):
        t = trades.copy()
        t["_m"] = [_minutes(x) for x in t.exit_time]
        for r in t.sort_values(["date", "_m"], kind="stable").to_dict("records"):
            if r["date"] not in idx:
                continue
            eq += float(r["pnl"])
            pts.append([round(idx[r["date"]] + r["_m"] / SESSION_MIN, 4), round(eq, 2), r["date"],
                        str(r["exit_time"])[:5], r.get("symbol", ""), r.get("side", "long"), r.get("kind", ""),
                        _num(r.get("exit")), r.get("reason", ""), _num(r.get("pnl")), _num(r.get("r_multiple"))])
    if days:                                                   # carry the line to the end of the last day
        last = pts[-1]
        if last[0] < len(days):
            pts.append([float(len(days)), last[1], days[-1], "16:00", "", "", "", None, "", None, None])
    return pts


def _days(curves) -> list[str]:
    s = set()
    for c in curves:
        s |= {pd.Timestamp(d).strftime("%Y-%m-%d") for d in c.index}
    return sorted(s)


def write_chart(path: Path, title: str, subtitle: str, series: list[dict], days: list[str], start: float) -> Path:
    """``series``: [{"name", "trades" (DataFrame), "net", "ret"}]; the first BEST by net are shown at first."""
    order = sorted(range(len(series)), key=lambda i: -(series[i].get("net") or float("-inf")))
    shown = set(order[:BEST])
    data = {"title": title, "subtitle": subtitle, "days": days, "start": start, "series": [
        {"name": s["name"], "net": _num(s.get("net")), "ret": _num(s.get("ret")), "on": i in shown,
         "pts": equity_points(s["trades"], days, start)} for i, s in enumerate(series)]}
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    page = PAGE.replace("__TITLE__", html.escape(title)).replace("__DATA__", blob)
    path.write_text(page, encoding="utf-8")
    return path


def run_chart(path: Path, trades: pd.DataFrame, curve: pd.Series, cfg, title: str) -> Path:
    net = float(curve.iloc[-1]) - cfg.starting_equity if len(curve) else 0.0
    sub = (f"{len(trades)} trades over {len(curve)} trading days, starting with ${cfg.starting_equity:,.0f}. "
           f"Net {'+' if net >= 0 else '-'}${abs(net):,.2f}.")
    return write_chart(path, title, sub, [{"name": "Account value", "trades": trades, "net": net,
                                           "ret": 100 * net / cfg.starting_equity}], _days([curve]), cfg.starting_equity)


def compare_chart(path: Path, table: pd.DataFrame, curves: dict, trades: dict, cfg) -> Path:
    series = [{"name": lab, "trades": trades.get(lab, pd.DataFrame()),
               "net": table.loc[lab, "net $"] if "net $" in table else None,
               "ret": table.loc[lab, "return %"] if "return %" in table else None} for lab in table.index]
    more = f" The {BEST} best by net are shown; click a name to add or hide it." if len(series) > BEST else \
        " Click a name to hide or show it."
    sub = (f"{len(series)} combinations, each starting with ${cfg.starting_equity:,.0f} on the same days and "
           f"stocks.{more}")
    return write_chart(path, "Backtest comparison", sub, series, _days(curves.values()), cfg.starting_equity)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#fbfbfa;--panel:#fff;--fg:#1c1d20;--muted:#6a6c73;--grid:#e8e8ea;--axis:#a4a6ad;--line:#d5d6da;
 --tipbg:#fff;--tipbd:#cfd0d5;--up:#1f8a4c;--down:#c8372d;--sel:rgba(47,111,223,.12)}
@media (prefers-color-scheme:dark){:root{--bg:#141518;--panel:#1b1c20;--fg:#ececef;--muted:#9c9ea6;--grid:#2a2c31;
 --axis:#5f616a;--line:#34363c;--tipbg:#202227;--tipbd:#3d4047;--up:#4cc27c;--down:#ef6b5e;--sel:rgba(110,160,255,.16)}}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:17px/1.45 "Segoe UI",system-ui,-apple-system,sans-serif}
body{display:flex;flex-direction:column}
header{padding:20px 32px 8px;display:flex;align-items:flex-end;gap:24px;flex-wrap:wrap}
h1{font-size:28px;margin:0;font-weight:650;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:16px;margin-top:4px}
.tools{margin-left:auto;display:flex;gap:10px;align-items:center;color:var(--muted);font-size:15px}
button{font:inherit;font-size:15px;color:var(--fg);background:var(--panel);border:1px solid var(--line);border-radius:7px;
 padding:6px 14px;cursor:pointer}
button:hover{border-color:var(--axis)}
#legend{display:flex;flex-wrap:wrap;gap:6px 22px;padding:6px 32px 4px;font-size:16px}
#legend label{display:flex;align-items:center;gap:8px;cursor:pointer;white-space:nowrap;user-select:none}
#legend label.off{opacity:.38}
#legend input{display:none}
.sw{width:26px;height:4px;border-radius:2px;display:inline-block}
.net{font-variant-numeric:tabular-nums;color:var(--muted)}
#wrap{position:relative;flex:1;min-height:560px;margin:8px 20px 20px;background:var(--panel);border:1px solid var(--line);
 border-radius:12px}
canvas{position:absolute;inset:0;width:100%;height:100%;cursor:crosshair}
#tip{position:absolute;pointer-events:none;display:none;background:var(--tipbg);border:1px solid var(--tipbd);
 border-radius:9px;padding:12px 16px;font-size:16px;line-height:1.5;box-shadow:0 6px 22px rgba(0,0,0,.18);
 white-space:nowrap;z-index:2;font-variant-numeric:tabular-nums}
#tip .v{font-size:22px;font-weight:650}
#tip .when{color:var(--muted)}
#tip .who{display:flex;align-items:center;gap:8px;margin-bottom:4px;font-weight:600}
#tip .up{color:var(--up)} #tip .down{color:var(--down)}
.hint{color:var(--muted);font-size:14px}
</style></head>
<body>
<header><div><h1 id="title"></h1><div class="sub" id="sub"></div></div>
<div class="tools"><span class="hint">Hover for details &middot; drag to zoom &middot; double-click to reset</span>
<span id="many"></span><button id="reset">Reset zoom</button></div></header>
<div id="legend"></div>
<div id="wrap"><canvas id="c"></canvas><div id="tip"></div></div>
<script>
const D = __DATA__;
const COLORS = ["#2f6fdf","#e0533d","#22995a","#9b51e0","#e3970f","#1aa4b8","#d6458f","#7a8a16","#8a5a3c","#5a6478"];
const dark = matchMedia("(prefers-color-scheme: dark)").matches;
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const cv = document.getElementById("c"), ctx = cv.getContext("2d"), wrap = document.getElementById("wrap"),
      tip = document.getElementById("tip");
const S = D.series, N = Math.max(1, D.days.length), single = S.length === 1;
const color = i => COLORS[i % COLORS.length], dashed = i => i >= COLORS.length;
const money = (v, sign) => (sign && v > 0 ? "+" : v < 0 ? "-" : "") + "$" +
  Math.abs(v).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const dayName = d => new Date(d + "T12:00:00").toLocaleDateString("en-US",
  {weekday: "short", month: "short", day: "numeric", year: "numeric"});
const shortDay = d => new Date(d + "T12:00:00").toLocaleDateString("en-US", {month: "short", day: "numeric"});
const clock = t => { const [h, m] = t.split(":").map(Number); return (h % 12 || 12) + ":" + String(m).padStart(2, "0") + (h < 12 ? " AM" : " PM"); };

document.getElementById("title").textContent = D.title;
document.getElementById("sub").textContent = D.subtitle;
let view = {x0: 0, x1: N}, hover = null, drag = null, W = 0, H = 0, Y = {y0: 0, y1: 1};
const M = {l: 110, r: 36, t: 24, b: 64};

// legend
const legend = document.getElementById("legend");
function buildLegend() {
  if (single) { legend.style.display = "none"; return; }
  legend.innerHTML = "";
  S.forEach((s, i) => {
    const lab = document.createElement("label");
    lab.className = s.on ? "" : "off";
    const net = s.net == null ? "" : `<span class="net">${money(s.net, true)}</span>`;
    lab.innerHTML = `<span class="sw" style="background:${color(i)};${dashed(i) ? "opacity:.6" : ""}"></span>${esc(s.name)} ${net}`;
    lab.onclick = () => { s.on = !s.on; lab.className = s.on ? "" : "off"; hover = null; tip.style.display = "none"; draw(); };
    legend.appendChild(lab);
  });
  if (S.length > 10) {
    const many = document.getElementById("many");
    const all = document.createElement("button"), best = document.createElement("button");
    all.textContent = "Show all"; best.textContent = "Best 10";
    all.onclick = () => { S.forEach(s => s.on = true); buildLegend(); draw(); };
    const ranked = S.map((s, i) => [s.net ?? -Infinity, i]).sort((a, b) => b[0] - a[0]).slice(0, 10).map(p => p[1]);
    best.onclick = () => { S.forEach((s, i) => s.on = ranked.includes(i)); buildLegend(); draw(); };
    many.replaceChildren(best, all);
  }
}

function niceStep(span, target) {
  const raw = span / target, p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p;
  return p * (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10);
}
const sx = x => M.l + (x - view.x0) / (view.x1 - view.x0) * (W - M.l - M.r);
const sy = y => M.t + (1 - (y - Y.y0) / (Y.y1 - Y.y0)) * (H - M.t - M.b);
const ix = px => view.x0 + (px - M.l) / (W - M.l - M.r) * (view.x1 - view.x0);

function yRange() {
  let lo = Infinity, hi = -Infinity;
  S.forEach(s => { if (!s.on) return; const p = s.pts;
    for (let k = 0; k < p.length; k++) {
      const inside = p[k][0] >= view.x0 && p[k][0] <= view.x1,
            near = (k + 1 < p.length && p[k + 1][0] >= view.x0 && p[k][0] < view.x0) || (k > 0 && p[k - 1][0] <= view.x1 && p[k][0] > view.x1);
      if (inside || near) { lo = Math.min(lo, p[k][1]); hi = Math.max(hi, p[k][1]); } } });
  if (!isFinite(lo)) { lo = D.start - 100; hi = D.start + 100; }
  const pad = Math.max((hi - lo) * 0.08, 25);
  Y = {y0: lo - pad, y1: hi + pad};
}

function draw() {
  ctx.clearRect(0, 0, W, H);
  yRange();
  const grid = css("--grid"), axis = css("--axis"), muted = css("--muted"), fg = css("--fg");
  ctx.font = "15px Segoe UI, system-ui, sans-serif";
  // y grid + labels
  const step = niceStep(Y.y1 - Y.y0, Math.max(4, Math.round((H - M.t - M.b) / 80)));
  ctx.textAlign = "right"; ctx.textBaseline = "middle";
  for (let v = Math.ceil(Y.y0 / step) * step; v <= Y.y1; v += step) {
    const y = Math.round(sy(v)) + .5;
    ctx.strokeStyle = grid; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(M.l, y); ctx.lineTo(W - M.r, y); ctx.stroke();
    ctx.fillStyle = muted; ctx.fillText("$" + Math.round(v).toLocaleString("en-US"), M.l - 12, y);
  }
  // starting value
  if (D.start >= Y.y0 && D.start <= Y.y1) {
    const y = Math.round(sy(D.start)) + .5;
    ctx.save(); ctx.setLineDash([6, 5]); ctx.strokeStyle = axis; ctx.beginPath(); ctx.moveTo(M.l, y); ctx.lineTo(W - M.r, y); ctx.stroke(); ctx.restore();
  }
  // x: one tick per trading day, labels spaced to fit
  const pxDay = (W - M.l - M.r) / (view.x1 - view.x0);
  const every = Math.max(1, Math.ceil(90 / pxDay));
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  const d0 = Math.max(0, Math.floor(view.x0)), d1 = Math.min(N - 1, Math.ceil(view.x1));
  for (let d = d0; d <= d1; d++) {
    const x = Math.round(sx(d)) + .5;
    if (x < M.l - 1 || x > W - M.r + 1) continue;
    if (pxDay > 14 || d % every === 0) { ctx.strokeStyle = grid; ctx.beginPath(); ctx.moveTo(x, M.t); ctx.lineTo(x, H - M.b); ctx.stroke(); }
    if (d % every === 0 && D.days[d]) {
      const lx = Math.min(W - M.r - 20, Math.max(M.l + 20, sx(d + (pxDay > 60 ? .5 : 0))));
      ctx.fillStyle = muted; ctx.fillText(shortDay(D.days[d]), lx, H - M.b + 12);
    }
  }
  ctx.strokeStyle = axis; ctx.beginPath(); ctx.moveTo(M.l, H - M.b + .5); ctx.lineTo(W - M.r, H - M.b + .5); ctx.stroke();
  // lines
  ctx.save(); ctx.beginPath(); ctx.rect(M.l, M.t - 4, W - M.l - M.r, H - M.t - M.b + 8); ctx.clip();
  S.forEach((s, i) => {
    if (!s.on) return;
    const hot = hover && hover.s === i;
    ctx.strokeStyle = color(i); ctx.lineWidth = hot || single ? 2.6 : 1.8; ctx.globalAlpha = hover && !hot ? .45 : 1;
    ctx.setLineDash(dashed(i) ? [8, 5] : []); ctx.lineJoin = "round";
    ctx.beginPath();
    s.pts.forEach((p, k) => { const x = sx(p[0]), y = sy(p[1]); k ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.stroke(); ctx.setLineDash([]);
    const vis = s.pts.filter(p => p[0] >= view.x0 && p[0] <= view.x1).length;
    if ((W - M.l - M.r) / Math.max(1, vis) > 7) {             // dots when there's room for them
      ctx.fillStyle = color(i);
      s.pts.forEach(p => { if (p[4]) { ctx.beginPath(); ctx.arc(sx(p[0]), sy(p[1]), 2.6, 0, 7); ctx.fill(); } });
    }
  });
  ctx.globalAlpha = 1;
  if (hover) {
    const p = S[hover.s].pts[hover.k], x = sx(p[0]), y = sy(p[1]);
    ctx.save(); ctx.setLineDash([4, 4]); ctx.strokeStyle = axis; ctx.beginPath(); ctx.moveTo(x, M.t); ctx.lineTo(x, H - M.b); ctx.stroke(); ctx.restore();
    ctx.fillStyle = css("--panel"); ctx.strokeStyle = color(hover.s); ctx.lineWidth = 3;
    ctx.beginPath(); ctx.arc(x, y, 7, 0, 7); ctx.fill(); ctx.stroke();
  }
  ctx.restore();
  if (drag && Math.abs(drag.x - drag.x2) > 2) {
    ctx.fillStyle = css("--sel");
    ctx.fillRect(Math.min(drag.x, drag.x2), M.t, Math.abs(drag.x2 - drag.x), H - M.t - M.b);
  }
}

function showTip(mx, my) {
  if (!hover) { tip.style.display = "none"; return; }
  const s = S[hover.s], p = s.pts[hover.k], prev = hover.k ? s.pts[hover.k - 1][1] : D.start;
  let h = "";
  if (!single) h += `<div class="who"><span class="sw" style="background:${color(hover.s)}"></span>${esc(s.name)}</div>`;
  h += `<div class="v">${money(p[1])}</div>`;
  h += `<div class="when">${p[2] ? dayName(p[2]) : ""} &middot; ${clock(p[3])} ET</div>`;
  if (p[4]) {
    const setup = (p[5] === "short" ? "short " : "long ") + p[6].replace(/^short /, "");
    h += `<div>${esc(p[4])} &middot; ${esc(setup)}</div>`;
    if (p[7] != null) h += `<div>Exit $${p[7].toFixed(2)}${p[8] ? " (" + esc(p[8]) + ")" : ""}</div>`;
    if (p[9] != null) h += `<div class="${p[9] >= 0 ? "up" : "down"}">P&amp;L ${money(p[9], true)}${p[10] != null ? ` (${p[10] > 0 ? "+" : ""}${p[10].toFixed(2)}R)` : ""}</div>`;
  } else {
    h += `<div class="when">${hover.k === 0 ? "Starting value" : "End of the last day"}</div>`;
  }
  const tot = p[1] - D.start;
  h += `<div class="when">Since start: <span class="${tot >= 0 ? "up" : "down"}">${money(tot, true)}</span></div>`;
  tip.innerHTML = h; tip.style.display = "block";
  const r = tip.getBoundingClientRect();
  let x = mx + 18, y = my + 18;
  if (x + r.width > W - 8) x = mx - r.width - 18;
  if (y + r.height > H - 8) y = my - r.height - 18;
  tip.style.left = Math.max(8, x) + "px"; tip.style.top = Math.max(8, y) + "px";
}

function nearest(mx, my) {
  let best = null, bd = 34 * 34;
  S.forEach((s, i) => { if (!s.on) return;
    s.pts.forEach((p, k) => { if (p[0] < view.x0 - .01 || p[0] > view.x1 + .01) return;
      const dx = sx(p[0]) - mx, dy = sy(p[1]) - my, d = dx * dx + dy * dy;
      if (d < bd) { bd = d; best = {s: i, k: k}; } }); });
  if (!best) {                                               // nothing close: the point just left of the cursor
    const x = ix(mx);                                         // on the line nearest the cursor's height
    let bdy = Infinity;
    S.forEach((s, i) => { if (!s.on) return; let k = -1;
      for (let j = 0; j < s.pts.length && s.pts[j][0] <= x; j++) k = j;
      if (k < 0) return; const dy = Math.abs(sy(s.pts[k][1]) - my);
      if (dy < bdy && dy < 60) { bdy = dy; best = {s: i, k: k}; } });
  }
  return best;
}

function pos(e) { const r = cv.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; }
cv.addEventListener("mousemove", e => {
  const [mx, my] = pos(e);
  if (drag) { drag.x2 = Math.max(M.l, Math.min(W - M.r, mx)); hover = null; tip.style.display = "none"; draw(); return; }
  hover = (mx >= M.l && mx <= W - M.r && my >= M.t && my <= H - M.b) ? nearest(mx, my) : null;
  draw(); showTip(mx, my);
});
cv.addEventListener("mouseleave", () => { hover = null; tip.style.display = "none"; if (!drag) draw(); });
cv.addEventListener("mousedown", e => { const [mx] = pos(e); if (mx >= M.l && mx <= W - M.r) drag = {x: mx, x2: mx}; });
addEventListener("mouseup", () => {
  if (!drag) return;
  if (Math.abs(drag.x2 - drag.x) > 12) {
    const a = ix(Math.min(drag.x, drag.x2)), b = ix(Math.max(drag.x, drag.x2));
    if (b - a > .05) view = {x0: Math.max(0, a), x1: Math.min(N, b)};
  }
  drag = null; draw();
});
cv.addEventListener("dblclick", () => { view = {x0: 0, x1: N}; draw(); });
document.getElementById("reset").onclick = () => { view = {x0: 0, x1: N}; draw(); };

function size() {
  const r = wrap.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  W = r.width; H = r.height; cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); draw();
}
buildLegend();
new ResizeObserver(size).observe(wrap);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
size();
</script></body></html>
"""
