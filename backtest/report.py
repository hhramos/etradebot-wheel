"""
backtest/report.py — HTML Report Generator
==========================================
Produces a self-contained HTML file (no server needed) that matches
the dark aesthetic of the existing projection.html and index.html.

Sections:
  1. KPI strip — P&L, win rate, premium collected, assignments
  2. Portfolio value chart (Chart.js, embedded)
  3. Monthly income bar chart
  4. Per-ticker breakdown table
  5. All-cycles trade log
  6. Open positions — what the live bot inherits
  7. Phase timeline — each ticker's journey through CSP→CC phases
"""

import datetime
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backtest.engine import BacktestResults


def _fmt_usd(v: float, sign: bool = False) -> str:
    prefix = "+" if sign and v >= 0 else ""
    return f"{prefix}${v:,.2f}"


def _pct(v: float, denom: float) -> str:
    if denom == 0:
        return "—"
    return f"{v / denom * 100:.1f}%"


def render_html(results: "BacktestResults", path: str) -> None:
    L       = results.ledger
    closed  = L.closed_cycles()
    curve   = L.portfolio_curve()
    monthly = L.monthly_income()
    stats   = L.per_ticker_stats()
    open_pos = L.open_positions_summary()

    # ── Chart data ────────────────────────────────────────────────────────
    curve_labels = json.dumps([str(d) for d, _ in curve])
    curve_values = json.dumps([round(v, 2) for _, v in curve])

    mo_labels = json.dumps(list(monthly.keys()))
    mo_values = json.dumps(list(monthly.values()))

    # ── KPIs ──────────────────────────────────────────────────────────────
    end_val    = results.capital + L.total_net_pnl()
    net_pnl    = L.total_net_pnl()
    total_prem = L.total_premium_collected()
    total_comm = L.total_commissions()
    win_r      = L.win_rate()
    asgn_r     = L.assignment_rate()
    n_closed   = len(closed)
    pnl_color  = "#00C896" if net_pnl >= 0 else "#FF4D6A"
    pnl_sign   = "+" if net_pnl >= 0 else ""

    # ── Ticker rows ───────────────────────────────────────────────────────
    ticker_rows = ""
    for t, s in stats.items():
        pnl_c = "#00C896" if s["net_pnl"] >= 0 else "#FF4D6A"
        ticker_rows += f"""
        <tr>
          <td style="font-weight:500;font-family:var(--mono)">{t}</td>
          <td>{s['cycles']}</td>
          <td style="color:{pnl_c};font-family:var(--mono)">{_fmt_usd(s['net_pnl'], sign=True)}</td>
          <td style="font-family:var(--mono);color:var(--green)">{_fmt_usd(s['premium'])}</td>
          <td style="font-family:var(--mono)">{s['win_rate']}%</td>
          <td style="font-family:var(--mono)">{s['assign_rate']}%</td>
          <td style="font-family:var(--mono)">{s['rolls']}</td>
        </tr>"""

    # ── Cycle log rows ────────────────────────────────────────────────────
    cycle_rows = ""
    for c in sorted(closed, key=lambda x: x.start_date, reverse=True)[:100]:
        pnl_c   = "#00C896" if c.net_pnl >= 0 else "#FF4D6A"
        outcome_badge = {
            "BTC":               ('<span style="background:var(--green-dim);color:var(--green);'
                                  'font-size:9px;padding:1px 5px;border-radius:3px">BTC 50%</span>'),
            "EXPIRED":           ('<span style="background:var(--blue-dim);color:var(--blue);'
                                  'font-size:9px;padding:1px 5px;border-radius:3px">EXPIRED</span>'),
            "ASSIGNED_CC_CALLED":('<span style="background:#1E1440;color:#9B7FFF;'
                                  'font-size:9px;padding:1px 5px;border-radius:3px">CC CALLED</span>'),
            "ASSIGNED_CC_HELD":  ('<span style="background:var(--amber-dim);color:var(--amber);'
                                  'font-size:9px;padding:1px 5px;border-radius:3px">CC HELD</span>'),
            "OPEN":              ('<span style="background:var(--bg3);color:var(--text3);'
                                  'font-size:9px;padding:1px 5px;border-radius:3px">OPEN</span>'),
        }.get(c.outcome, c.outcome)
        roll_txt = f" ×{c.rolls} rolls" if c.rolls else ""
        cycle_rows += f"""
        <tr>
          <td style="font-family:var(--mono);font-weight:500">{c.ticker}</td>
          <td style="font-family:var(--mono);font-size:11px">{c.start_date}</td>
          <td style="font-family:var(--mono);font-size:11px">{c.end_date or '—'}</td>
          <td style="font-family:var(--mono)">${c.csp_strike:.0f} / ${c.csp_premium:.2f}</td>
          <td style="font-family:var(--mono)">{c.csp_expiry}</td>
          <td>{outcome_badge}{roll_txt}</td>
          <td style="color:{pnl_c};font-family:var(--mono)">{_fmt_usd(c.net_pnl, sign=True)}</td>
        </tr>"""

    # ── Open positions ─────────────────────────────────────────────────────
    open_rows = ""
    if open_pos:
        for p in open_pos:
            phase_color = {"CSP_OPEN": "var(--blue)", "CC_OPEN": "var(--purple)",
                           "ASSIGNED": "var(--amber)"}.get(p["phase"], "var(--text2)")
            detail = (f"CC ${p['cc_strike']} {p['cc_expiry']}"
                      if p.get("cc_strike") else
                      f"CSP ${p['csp_strike']} {p['csp_expiry']}")
            open_rows += f"""
            <tr>
              <td style="font-family:var(--mono);font-weight:500">{p['ticker']}</td>
              <td style="color:{phase_color};font-family:var(--mono);font-size:11px">{p['phase']}</td>
              <td style="font-family:var(--mono)">{detail}  ×{p['contracts']}c</td>
              <td style="font-family:var(--mono);font-size:11px">{p['since']}</td>
              <td><span style="background:var(--green-dim);color:var(--green);font-size:9px;
                  padding:2px 7px;border-radius:3px">→ live bot</span></td>
            </tr>"""
    else:
        open_rows = '<tr><td colspan="5" style="text-align:center;color:var(--text3)">All positions closed at end of backtest window</td></tr>'

    # ── Phase timeline data ───────────────────────────────────────────────
    # Build one row per ticker showing phases as coloured segments
    tickers_with_data = list(stats.keys())[:12]   # cap at 12 for display
    timeline_js = "const timelineData = " + json.dumps([
        {
            "ticker": t,
            "events": [
                {"date": str(s.date), "phase": s.phase}
                for s in L.snapshots if s.ticker == t
            ]
        }
        for t in tickers_with_data
    ]) + ";"

    skipped_html = ""
    if results.skipped:
        items = ", ".join(f"{t} ({r})" for t, r in list(results.skipped.items())[:20])
        skipped_html = f'<div style="font-size:10px;color:var(--text3);padding:6px 14px">Skipped: {items}</div>'

    # ── Full HTML ─────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ETradeBot — Backtest Report {results.start} → {results.end}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@2.47.0/tabler-icons.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{{
  --bg:#0D0F12;--bg2:#141720;--bg3:#1C2030;
  --border:#252A3A;--border2:#2E3550;
  --text:#E8EAF0;--text2:#8B91A8;--text3:#555E7A;
  --green:#00C896;--green-dim:#0D3D2E;
  --red:#FF4D6A;--red-dim:#3D0D16;
  --blue:#4B8EFF;--blue-dim:#0D1F3D;
  --amber:#FFB347;--amber-dim:#3D2900;
  --purple:#9B7FFF;--purple-dim:#1E1440;
  --mono:'IBM Plex Mono',monospace;
  --sans:'DM Sans',sans-serif;
  --r:6px;--rl:10px;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:13px;line-height:1.5}}
::-webkit-scrollbar{{width:4px}}::-webkit-scrollbar-track{{background:var(--bg2)}}
::-webkit-scrollbar-thumb{{background:var(--border2);border-radius:2px}}
.topbar{{display:flex;align-items:center;justify-content:space-between;padding:10px 24px;
         background:var(--bg2);border-bottom:.5px solid var(--border);position:sticky;top:0;z-index:10}}
.logo{{font-family:var(--mono);font-size:13px;font-weight:500;display:flex;align-items:center;gap:8px}}
.wrap{{max-width:1200px;margin:0 auto;padding:20px 24px}}
.panel{{background:var(--bg2);border:.5px solid var(--border);border-radius:var(--rl);overflow:hidden;margin-bottom:16px}}
.ph{{display:flex;align-items:center;justify-content:space-between;padding:9px 16px;
     background:var(--bg3);border-bottom:.5px solid var(--border);font-size:12px;font-weight:500}}
.pb{{padding:16px}}
.kpi-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}}
.kpi{{background:var(--bg2);border:.5px solid var(--border);border-radius:var(--rl);padding:14px 16px;text-align:center}}
.kpi-label{{font-size:11px;color:var(--text3);margin-bottom:4px}}
.kpi-val{{font-size:24px;font-weight:500;margin-bottom:2px}}
.kpi-sub{{font-size:11px;color:var(--text3)}}
.two-col{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{padding:6px 8px;font-size:10px;font-weight:500;color:var(--text3);
    background:var(--bg3);border-bottom:.5px solid var(--border);text-align:left}}
td{{padding:7px 8px;border-bottom:.5px solid var(--border)}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:var(--bg3)}}
.badge{{display:inline-block;font-size:9px;padding:2px 6px;border-radius:3px}}
.notice{{display:flex;gap:8px;align-items:flex-start;padding:8px 12px;border-radius:var(--r);
         font-size:11px;margin-bottom:12px;border:.5px dashed}}
.notice.info{{background:var(--blue-dim);border-color:var(--blue);color:var(--blue)}}
.continuity-box{{background:var(--green-dim);border:.5px solid var(--green);border-radius:var(--rl);
                  padding:14px 16px;margin-bottom:16px}}
.continuity-title{{font-size:12px;font-weight:500;color:var(--green);margin-bottom:6px;
                    display:flex;align-items:center;gap:7px}}
.continuity-body{{font-size:12px;color:#80E8C0;line-height:1.6}}
</style>
</head>
<body>

<div class="topbar">
  <div class="logo">
    <i class="ti ti-chart-dots" style="color:var(--purple)"></i>
    ETradeBot — Wheel Backtest Report
    <span style="font-size:10px;color:var(--text3);font-weight:400">{results.start} → {results.end}</span>
  </div>
  <span style="font-size:11px;color:var(--text3)">{len(results.universe)} tickers · ${results.capital:,.0f} capital</span>
</div>

<div class="wrap">

  <!-- KPIs -->
  <div class="kpi-grid">
    <div class="kpi">
      <div class="kpi-label">Net P&L</div>
      <div class="kpi-val" style="color:{pnl_color}">{pnl_sign}{_fmt_usd(net_pnl)}</div>
      <div class="kpi-sub">{pnl_sign}{net_pnl / results.capital * 100:.1f}% on ${results.capital:,.0f}</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Premium collected</div>
      <div class="kpi-val" style="color:var(--green)">{_fmt_usd(total_prem)}</div>
      <div class="kpi-sub">minus {_fmt_usd(total_comm)} commissions</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Win rate</div>
      <div class="kpi-val" style="color:var(--blue)">{win_r}%</div>
      <div class="kpi-sub">{n_closed} closed cycles</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Assignment rate</div>
      <div class="kpi-val" style="color:var(--amber)">{asgn_r}%</div>
      <div class="kpi-sub">of closed cycles → CC</div>
    </div>
  </div>

  <!-- Continuity callout -->
  <div class="continuity-box">
    <div class="continuity-title"><i class="ti ti-player-play"></i> Will the bot continue your wheel?</div>
    <div class="continuity-body">
      {len(open_pos)} position{'s' if len(open_pos) != 1 else ''} {'are' if len(open_pos) != 1 else 'is'} still open at the end of the backtest window.
      The backtest saves <code style="color:var(--green)">wheel_state.json</code> with these exact phases — the live bot loads it on next startup and
      picks up immediately: CSP positions get monitored for BTC targets, ASSIGNED positions get a covered call written,
      CC positions get monitored for expiry or call-away. <strong>No manual re-entry needed.</strong>
    </div>
  </div>

  <!-- Portfolio curve -->
  <div class="panel">
    <div class="ph"><i class="ti ti-chart-line"></i> Portfolio value — daily</div>
    <div class="pb"><canvas id="curveChart" height="180"></canvas></div>
  </div>

  <div class="two-col">

    <!-- Monthly income -->
    <div class="panel">
      <div class="ph"><i class="ti ti-coin"></i> Monthly net income</div>
      <div class="pb"><canvas id="moChart" height="220"></canvas></div>
    </div>

    <!-- Per-ticker table -->
    <div class="panel">
      <div class="ph"><i class="ti ti-trophy"></i> Per-ticker breakdown</div>
      <table>
        <thead><tr>
          <th>Ticker</th><th>Cycles</th><th>Net P&L</th>
          <th>Premium</th><th>Win%</th><th>Asgn%</th><th>Rolls</th>
        </tr></thead>
        <tbody>{ticker_rows}</tbody>
      </table>
      {skipped_html}
    </div>

  </div>

  <!-- Open positions -->
  <div class="panel">
    <div class="ph">
      <span><i class="ti ti-arrow-right-circle" style="color:var(--green)"></i> Open positions — carried forward to live bot</span>
      <span style="font-size:11px;color:var(--green)">{len(open_pos)} position{'s' if len(open_pos) != 1 else ''}</span>
    </div>
    <table>
      <thead><tr><th>Ticker</th><th>Phase</th><th>Position detail</th><th>Open since</th><th>Handoff</th></tr></thead>
      <tbody>{open_rows}</tbody>
    </table>
    <div style="padding:8px 14px;font-size:10px;color:var(--text3)">
      Run <code>python backtest/run_backtest.py --save-state</code> to write wheel_state.json and activate live continuation.
    </div>
  </div>

  <!-- Trade log -->
  <div class="panel">
    <div class="ph">
      <span><i class="ti ti-list"></i> Trade log — closed cycles</span>
      <span style="font-size:11px;color:var(--text3)">(newest first · max 100 shown)</span>
    </div>
    <table>
      <thead><tr>
        <th>Ticker</th><th>Opened</th><th>Closed</th>
        <th>Strike / Premium</th><th>Expiry</th><th>Outcome</th><th>Net P&L</th>
      </tr></thead>
      <tbody>{cycle_rows}</tbody>
    </table>
  </div>

</div><!-- /wrap -->

<script>
{timeline_js}

// Portfolio curve
const curveCtx = document.getElementById('curveChart').getContext('2d');
const startVal  = {results.capital};
new Chart(curveCtx, {{
  type: 'line',
  data: {{
    labels: {curve_labels},
    datasets: [{{
      label: 'Portfolio value',
      data: {curve_values},
      borderColor: '#4B8EFF',
      borderWidth: 2,
      tension: 0.3,
      pointRadius: 0,
      fill: {{ target: 'origin', above: 'rgba(75,142,255,0.07)' }},
    }}, {{
      label: 'Starting capital',
      data: Array({len(curve)}).fill(startVal),
      borderColor: '#555E7A',
      borderWidth: 1,
      borderDash: [4,4],
      pointRadius: 0,
      fill: false,
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{
        backgroundColor: '#1C2030', borderColor: '#2E3550', borderWidth: 1,
        titleColor: '#8B91A8', bodyColor: '#E8EAF0',
        callbacks: {{ label: c => ` ${{c.dataset.label}}: $${{Math.round(c.raw).toLocaleString()}}` }}
      }}
    }},
    scales: {{
      x: {{ ticks: {{ color:'#555E7A', font:{{size:9}}, maxTicksLimit:18 }}, grid:{{ color:'#252A3A' }} }},
      y: {{ ticks: {{ color:'#555E7A', font:{{size:9}}, callback: v=>'$'+Math.round(v/1000)+'k' }}, grid:{{ color:'#252A3A' }} }},
    }}
  }}
}});

// Monthly income bars
const moCtx = document.getElementById('moChart').getContext('2d');
const moVals = {mo_values};
const colors = moVals.map(v => v >= 0 ? 'rgba(0,200,150,0.7)' : 'rgba(255,77,106,0.7)');
new Chart(moCtx, {{
  type: 'bar',
  data: {{
    labels: {mo_labels},
    datasets: [{{ label:'Net income', data: moVals, backgroundColor: colors, borderWidth:0 }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{
        backgroundColor: '#1C2030', borderColor: '#2E3550', borderWidth: 1,
        titleColor: '#8B91A8', bodyColor: '#E8EAF0',
        callbacks: {{ label: c => ` $${{c.raw.toFixed(2)}}` }}
      }}
    }},
    scales: {{
      x: {{ ticks: {{ color:'#555E7A', font:{{size:9}} }}, grid:{{ display:false }} }},
      y: {{ ticks: {{ color:'#555E7A', font:{{size:9}}, callback: v=>'$'+v }}, grid:{{ color:'#252A3A' }} }},
    }}
  }}
}});
</script>
</body>
</html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
