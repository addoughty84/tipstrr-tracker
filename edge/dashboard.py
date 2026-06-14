#!/usr/bin/env python3
"""Dashboard - renders a single self-contained HTML page summarising the edge
system: edge leaderboard, forward (paper) records, an equity curve, and the
data-gap queue. Data is baked into the file, so it opens anywhere with no DB
access. Regenerated on a schedule and committed to the repo.

  python dashboard.py            # writes dashboard.html
Env: SUPABASE_DB_URL"""
import json, datetime as dt
import engine


def q(conn, sql):
    cur = conn.cursor(); cur.execute(sql); rows = cur.fetchall()
    cols = [d[0] for d in cur.description]; cur.close()
    return [dict(zip(cols, r)) for r in rows]


def main():
    conn = engine.connect()
    edges = q(conn, """
        SELECT name, status, COALESCE(paper_n,0) paper_n, paper_roi,
               (bt_holdout->>'roi') bt_roi, (bt_holdout->>'n') bt_n, frozen_at::date frozen
        FROM edges ORDER BY (bt_holdout->>'roi')::numeric DESC NULLS LAST""")
    gaps = q(conn, "SELECT theory, needs_data, status FROM data_gaps ORDER BY created_at DESC")
    # equity curve: cumulative forward profit across all paper edges, by day
    curve = q(conn, """
        SELECT posted_at::date d, round(sum(profit)::numeric,2) day_profit
        FROM edge_bets GROUP BY 1 ORDER BY 1""")
    totals = q(conn, """
        SELECT (SELECT count(*) FROM edges) edges,
               (SELECT count(*) FROM edges WHERE status='paper') paper,
               (SELECT count(*) FROM edge_bets) fwd_bets,
               (SELECT count(*) FROM data_gaps WHERE status='open') open_gaps""")[0]
    conn.close()

    # build equity series (cumulative)
    cum = 0.0; pts = []
    for r in curve:
        cum += float(r["day_profit"] or 0)
        pts.append({"x": str(r["d"]), "y": round(cum, 2)})

    def rows_html(edges):
        out = []
        for e in edges:
            roi = e["bt_roi"]
            proi = ("%+d%%" % e["paper_roi"]) if e["paper_roi"] is not None else "—"
            out.append(
                f"<tr><td>{e['name']}</td><td><span class=pill>{e['status']}</span></td>"
                f"<td class=num>{('+'+roi+'%') if roi else '—'}</td><td class=num>{e['bt_n'] or '—'}</td>"
                f"<td class=num>{e['paper_n']}</td><td class=num>{proi}</td><td class=dim>{e['frozen']}</td></tr>")
        return "\n".join(out)

    def gap_rows(gaps):
        return "\n".join(
            f"<tr><td>{g['theory']}</td><td class=dim>{g['needs_data'] or ''}</td>"
            f"<td><span class=pill>{g['status']}</span></td></tr>" for g in gaps)

    html = f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Edge Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0e1117;color:#e6edf3;margin:0;padding:24px;}}
 h1{{font-size:22px;margin:0 0 4px}} .sub{{color:#8b949e;margin:0 0 20px;font-size:13px}}
 .cards{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:22px}}
 .card{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:14px 18px;min-width:130px}}
 .card .v{{font-size:26px;font-weight:700}} .card .l{{color:#8b949e;font-size:12px}}
 h2{{font-size:15px;margin:24px 0 8px;color:#c9d1d9}}
 table{{width:100%;border-collapse:collapse;background:#161b22;border:1px solid #30363d;border-radius:10px;overflow:hidden}}
 th,td{{text-align:left;padding:9px 12px;border-bottom:1px solid #21262d;font-size:13px}}
 th{{color:#8b949e;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em}}
 td.num{{text-align:right;font-variant-numeric:tabular-nums}} td.dim{{color:#8b949e}}
 .pill{{background:#21262d;border-radius:20px;padding:2px 9px;font-size:11px;color:#8b949e}}
 canvas{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:10px;margin-top:6px}}
 .note{{color:#8b949e;font-size:12px;margin-top:6px}}
</style></head><body>
<h1>🐎 Edge Dashboard</h1>
<p class=sub>tipstrr-tracker · generated {dt.datetime.utcnow():%Y-%m-%d %H:%M} UTC</p>
<div class=cards>
 <div class=card><div class=v>{totals['edges']}</div><div class=l>edges</div></div>
 <div class=card><div class=v>{totals['paper']}</div><div class=l>paper-testing</div></div>
 <div class=card><div class=v>{totals['fwd_bets']}</div><div class=l>forward bets</div></div>
 <div class=card><div class=v>{totals['open_gaps']}</div><div class=l>data gaps</div></div>
</div>

<h2>Forward equity (paper money, cumulative points)</h2>
<canvas id=eq height=90></canvas>
<p class=note>Forward records start the day each edge is frozen. Backtest numbers are NOT shown here — only money "bet" after freezing counts. This is the honest curve.</p>

<h2>Edges — ranked by out-of-sample (holdout) ROI</h2>
<table><tr><th>Edge</th><th>Status</th><th>Holdout ROI</th><th>Holdout bets</th><th>Fwd bets</th><th>Fwd ROI</th><th>Frozen</th></tr>
{rows_html(edges)}
</table>
<p class=note>Holdout ROI = backtest on data the finder never saw. Fwd ROI = real forward paper record (the one that counts). Trust Fwd once the bet count is high.</p>

<h2>Data-gap queue — theories awaiting / using new data</h2>
<table><tr><th>Theory</th><th>Needs</th><th>Status</th></tr>
{gap_rows(gaps)}
</table>

<script>
const pts={json.dumps(pts)};
new Chart(document.getElementById('eq'),{{type:'line',
 data:{{labels:pts.map(p=>p.x),datasets:[{{data:pts.map(p=>p.y),borderColor:'#3fb950',backgroundColor:'rgba(63,185,80,.1)',fill:true,tension:.2,pointRadius:0}}]}},
 options:{{plugins:{{legend:{{display:false}}}},scales:{{x:{{ticks:{{color:'#8b949e'}},grid:{{color:'#21262d'}}}},y:{{ticks:{{color:'#8b949e'}},grid:{{color:'#21262d'}}}}}}}}}});
</script>
</body></html>"""
    open("dashboard.html", "w", encoding="utf-8").write(html)
    print("Wrote dashboard.html (%d edges, %d gaps, %d forward bets)" %
          (len(edges), len(gaps), totals["fwd_bets"]))


if __name__ == "__main__":
    main()
