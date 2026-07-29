"""
Renders the read-only HTML dashboard served at GET /dashboard in server.py.
Pure functions: given the parsed trade log rows and the account list from
deriv_tokens.json, return a complete HTML page. No client-side JS — the page
is re-rendered fresh on every request, so a manual reload always shows the
current trade_log.csv state.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

# Palette — see the dataviz skill's references/palette.md for the source values.
_STYLE = """
:root {
  color-scheme: light;
  --surface-1:      #fcfcfb;
  --page:           #f9f9f7;
  --text-primary:   #0b0b0b;
  --text-secondary: #52514e;
  --text-muted:     #898781;
  --grid:           #e1e0d9;
  --baseline:       #c3c2b7;
  --border:         rgba(11,11,11,0.10);
  --series-1:       #2a78d6;
  --series-1-fill:  rgba(42,120,214,0.10);
  --good-text:      #006300;
  --good-bg:        #e6f4e6;
  --bad-text:       #b42318;
  --bad-bg:         #fbe9e7;
  --muted-bg:       #efeee9;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --surface-1:      #1a1a19;
    --page:           #0d0d0d;
    --text-primary:   #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted:     #898781;
    --grid:           #2c2c2a;
    --baseline:       #383835;
    --border:         rgba(255,255,255,0.10);
    --series-1:       #3987e5;
    --series-1-fill:  rgba(57,135,229,0.14);
    --good-text:      #0ca30c;
    --good-bg:        #113311;
    --bad-text:       #e66767;
    --bad-bg:         #3a1414;
    --muted-bg:       #232320;
  }
}

* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--page);
  color: var(--text-primary);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}
.wrap { max-width: 980px; margin: 0 auto; padding: 32px 20px 64px; }
.top { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 24px; }
h1 { font-size: 20px; margin: 0; }
.updated { color: var(--text-muted); font-size: 13px; }
.updated a { color: var(--text-secondary); }

.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 28px; }
.tile {
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px;
}
.tile .label { font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; }
.tile .value { font-size: 22px; font-weight: 600; }
.tile .value.good { color: var(--good-text); }
.tile .value.bad { color: var(--bad-text); }
.tile .sub { font-size: 12px; color: var(--text-muted); margin-top: 2px; }

figure.chart {
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px 16px 8px;
  margin: 0 0 28px;
}
figure.chart figcaption { font-size: 13px; color: var(--text-secondary); margin-bottom: 8px; }
figure.chart svg { width: 100%; height: auto; display: block; }
.empty { color: var(--text-muted); font-size: 13px; padding: 24px 0; text-align: center; }

table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
th { color: var(--text-secondary); font-weight: 600; font-size: 12px; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.tablewrap { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; overflow-x: auto; }

.badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; }
.badge.good { background: var(--good-bg); color: var(--good-text); }
.badge.bad { background: var(--bad-bg); color: var(--bad-text); }
.badge.muted { background: var(--muted-bg); color: var(--text-secondary); }
"""


def _fnum(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _status(row: dict) -> tuple[str, str]:
    profit = row.get("profit", "")
    if profit not in ("", None):
        return ("WON", "good") if _fnum(profit) > 0 else ("LOST", "bad")
    return ("QUOTED", "muted")


def _build_chart_svg(points: list[float]) -> str:
    if len(points) < 2:
        return ""
    width, height = 900, 220
    pad_l, pad_r, pad_t, pad_b = 48, 12, 16, 8
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    y_min, y_max = min(0.0, min(points)), max(0.0, max(points))
    if y_min == y_max:
        y_min, y_max = y_min - 1, y_max + 1
    span = y_max - y_min

    def x_at(i: int) -> float:
        return pad_l + (i * plot_w / (len(points) - 1))

    def y_at(v: float) -> float:
        return pad_t + plot_h - ((v - y_min) / span * plot_h)

    line_pts = [(x_at(i), y_at(v)) for i, v in enumerate(points)]
    line_path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in line_pts)
    zero_y = y_at(0.0)
    area_path = (
        f"M{line_pts[0][0]:.1f},{zero_y:.1f} "
        + " L".join(f"{x:.1f},{y:.1f}" for x, y in line_pts)
        + f" L{line_pts[-1][0]:.1f},{zero_y:.1f} Z"
    )

    end_val = points[-1]
    end_x, end_y = line_pts[-1]

    return f"""
<svg viewBox="0 0 {width} {height}" role="img" aria-label="Cumulative profit and loss over executed trades">
  <line x1="{pad_l}" y1="{zero_y:.1f}" x2="{width - pad_r}" y2="{zero_y:.1f}" stroke="var(--baseline)" stroke-width="1" />
  <text x="{pad_l - 6}" y="{zero_y:.1f}" text-anchor="end" dominant-baseline="middle" font-size="11" fill="var(--text-muted)">0</text>
  <text x="{pad_l - 6}" y="{pad_t + 4}" text-anchor="end" font-size="11" fill="var(--text-muted)">{y_max:,.0f}</text>
  <text x="{pad_l - 6}" y="{height - pad_b}" text-anchor="end" font-size="11" fill="var(--text-muted)">{y_min:,.0f}</text>
  <path d="{area_path}" fill="var(--series-1-fill)" stroke="none" />
  <path d="{line_path}" fill="none" stroke="var(--series-1)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />
  <circle cx="{end_x:.1f}" cy="{end_y:.1f}" r="4" fill="var(--series-1)" stroke="var(--surface-1)" stroke-width="2" />
  <text x="{end_x:.1f}" y="{end_y - 10:.1f}" text-anchor="end" font-size="12" font-weight="600" fill="var(--text-primary)">{end_val:+,.2f}</text>
</svg>
""".strip()


def render_dashboard(rows: list[dict], accounts: list[dict]) -> str:
    executed = [r for r in rows if r.get("profit", "") not in ("", None)]
    wins = [r for r in executed if _fnum(r["profit"]) > 0]
    net_pnl = sum(_fnum(r["profit"]) for r in executed)
    win_rate = (len(wins) / len(executed) * 100) if executed else None

    cumulative: list[float] = []
    running = 0.0
    for r in executed:
        running += _fnum(r["profit"])
        cumulative.append(running)

    balances: dict[str, tuple[str, float, str]] = {}
    for a in accounts:
        balances[a.get("account_type", "")] = ("", _fnum(a.get("balance")), a.get("currency", ""))
    for r in rows:
        acct = r.get("account", "")
        bal = r.get("balance_after", "")
        if acct and bal not in ("", None):
            balances[acct] = (r.get("timestamp", ""), _fnum(bal), "")

    def tile(label: str, value: str, cls: str = "", sub: str = "") -> str:
        sub_html = f'<div class="sub">{html.escape(sub)}</div>' if sub else ""
        return (
            f'<div class="tile"><div class="label">{html.escape(label)}</div>'
            f'<div class="value {cls}">{html.escape(value)}</div>{sub_html}</div>'
        )

    kpis = [
        tile("Executed trades", str(len(executed))),
        tile("Win rate", f"{win_rate:.0f}%" if win_rate is not None else "—",
             sub=f"{len(wins)}/{len(executed)} won" if executed else "no trades yet"),
        tile("Net P&L", f"{net_pnl:+,.2f}", "good" if net_pnl > 0 else "bad" if net_pnl < 0 else ""),
    ]
    for acct_type in ("demo", "real"):
        if acct_type in balances:
            _, bal, ccy = balances[acct_type]
            kpis.append(tile(f"{acct_type.capitalize()} balance", f"{bal:,.2f} {ccy}".strip()))

    chart_svg = _build_chart_svg(cumulative)
    chart_body = (
        chart_svg if chart_svg
        else '<div class="empty">Not enough executed trades yet to plot a trend — quotes don\'t count, only actual buys.</div>'
    )

    table_rows = []
    for r in reversed(rows):
        label, cls = _status(r)
        profit = r.get("profit", "")
        profit_str = f"{_fnum(profit):+,.2f}" if profit not in ("", None) else "—"
        table_rows.append(f"""
<tr>
  <td>{html.escape(r.get('timestamp', ''))}</td>
  <td>{html.escape(r.get('mode', ''))}</td>
  <td>{html.escape(r.get('account', ''))}</td>
  <td>{html.escape(r.get('symbol', ''))}</td>
  <td>{html.escape(r.get('contract_type', ''))}</td>
  <td class="num">{html.escape(str(r.get('barrier', '')))}</td>
  <td class="num">{_fnum(r.get('stake', '')):,.2f}</td>
  <td class="num">{_fnum(r.get('payout', '')):,.2f}</td>
  <td class="num">{profit_str}</td>
  <td class="num">{r.get('balance_after', '') and f"{_fnum(r['balance_after']):,.2f}" or ''}</td>
  <td><span class="badge {cls}">{label}</span></td>
</tr>""")

    table_html = (
        "".join(table_rows) if table_rows
        else '<tr><td colspan="11" class="empty">No sessions logged yet.</td></tr>'
    )

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Deriv bot dashboard</title>
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <h1>Deriv Over/Under bot</h1>
    <div class="updated">generated {html.escape(now)} · <a href="/dashboard">refresh</a></div>
  </div>

  <div class="kpis">{''.join(kpis)}</div>

  <figure class="chart">
    <figcaption>Cumulative P&amp;L across executed trades</figcaption>
    {chart_body}
  </figure>

  <div class="tablewrap">
    <table>
      <thead>
        <tr>
          <th>Time (UTC)</th><th>Mode</th><th>Account</th><th>Symbol</th><th>Contract</th>
          <th class="num">Barrier</th><th class="num">Stake</th><th class="num">Payout</th>
          <th class="num">Profit</th><th class="num">Balance after</th><th>Status</th>
        </tr>
      </thead>
      <tbody>{table_html}</tbody>
    </table>
  </div>
</div>
</body>
</html>"""
