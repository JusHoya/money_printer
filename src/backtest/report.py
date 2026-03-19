"""Backtest HTML report generator.

Sprint 5, Task 5.3.

Generates self-contained HTML reports with embedded matplotlib charts
(base64-encoded PNGs).
"""

import base64
import io
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_OUTPUT_DIR = Path("data/backtest_results")


def _fig_to_base64(fig) -> str:
    """Convert a matplotlib figure to a base64 data URI."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode("utf-8")
    buf.close()
    return f"data:image/png;base64,{b64}"


class BacktestReportGenerator:
    """Generate an HTML report from backtest results."""

    def __init__(self, output_dir: str = None):
        self.output_dir = Path(output_dir) if output_dir else _OUTPUT_DIR

    def generate(self, results: dict, filename: str = None) -> Path:
        """Write an HTML report and return the file path."""
        self.output_dir.mkdir(parents=True, exist_ok=True)

        if filename is None:
            ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            filename = f"backtest_{ts}.html"

        path = self.output_dir / filename
        html = self._build_html(results)
        path.write_text(html, encoding="utf-8")
        logger.info("Report saved to %s", path)
        return path

    def _build_html(self, results: dict) -> str:
        criteria = results.get("criteria", {})
        all_pass = all(criteria.values()) if criteria else False

        sections = [
            self._summary_section(results, criteria, all_pass),
            self._equity_chart_section(results),
            self._strategy_table(results),
            self._trades_table(results),
        ]

        body = "\n".join(sections)
        return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>Money Printer Backtest Report</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       max-width: 1000px; margin: 0 auto; padding: 20px; background: #0d1117; color: #c9d1d9; }}
h1 {{ color: #58a6ff; }} h2 {{ color: #8b949e; border-bottom: 1px solid #30363d; padding-bottom: 8px; }}
.card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin: 12px 0; }}
.pass {{ color: #3fb950; font-weight: bold; }} .fail {{ color: #f85149; font-weight: bold; }}
table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid #30363d; }}
th {{ color: #8b949e; }} .pos {{ color: #3fb950; }} .neg {{ color: #f85149; }}
img {{ max-width: 100%; border-radius: 4px; }}
</style>
</head><body>
<h1>Money Printer V2 — Backtest Report</h1>
<p>Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>
{body}
</body></html>"""

    def _summary_section(self, r: dict, criteria: dict, all_pass: bool) -> str:
        status = (
            '<span class="pass">ALL CRITERIA PASSED</span>'
            if all_pass
            else '<span class="fail">CRITERIA NOT MET</span>'
        )
        rows = ""
        labels = {
            "win_rate_85": "Win Rate >= 85%",
            "per_strategy_75": "Per-Strategy >= 75%",
            "positive_pnl": "Positive PnL After Fees",
            "max_drawdown_15": "Max Drawdown < 15%",
            "brier_below_20": "Brier Score < 0.20",
        }
        for key, label in labels.items():
            passed = criteria.get(key, False)
            cls = "pass" if passed else "fail"
            rows += f'<tr><td>{label}</td><td class="{cls}">{"PASS" if passed else "FAIL"}</td></tr>'

        pnl_cls = "pos" if r.get("net_pnl", 0) > 0 else "neg"
        return f"""<div class="card">
<h2>Summary — {status}</h2>
<table>
<tr><td>Total Trades</td><td>{r.get('total_trades', 0)}</td></tr>
<tr><td>Win Rate</td><td>{r.get('win_rate', 0):.1%}</td></tr>
<tr><td>Net PnL</td><td class="{pnl_cls}">${r.get('net_pnl', 0):.2f}</td></tr>
<tr><td>Total Fees</td><td>${r.get('total_fees', 0):.2f}</td></tr>
<tr><td>Sharpe Ratio</td><td>{r.get('sharpe_ratio', 0):.2f}</td></tr>
<tr><td>Max Drawdown</td><td>{r.get('max_drawdown_pct', 0):.1%}</td></tr>
<tr><td>Brier Score</td><td>{r.get('brier_score', 1):.4f}</td></tr>
<tr><td>Profit Factor</td><td>{r.get('profit_factor', 0):.2f}</td></tr>
</table>
<h3>Validation Criteria</h3>
<table>{rows}</table>
</div>"""

    def _equity_chart_section(self, r: dict) -> str:
        ec = r.get("equity_curve", [])
        if len(ec) < 2:
            return ""

        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
            fig.patch.set_facecolor("#0d1117")
            for ax in (ax1, ax2):
                ax.set_facecolor("#161b22")
                ax.tick_params(colors="#8b949e")

            # Equity curve
            ax1.plot(ec, color="#58a6ff", linewidth=1)
            ax1.set_ylabel("Equity ($)", color="#8b949e")
            ax1.set_title("Equity Curve", color="#c9d1d9")

            # Drawdown
            peak = [ec[0]]
            for v in ec[1:]:
                peak.append(max(peak[-1], v))
            dd = [(p - v) / p * 100 if p > 0 else 0 for p, v in zip(peak, ec)]
            ax2.fill_between(range(len(dd)), dd, color="#f85149", alpha=0.4)
            ax2.set_ylabel("Drawdown (%)", color="#8b949e")
            ax2.set_xlabel("Trade #", color="#8b949e")

            plt.tight_layout()
            img = _fig_to_base64(fig)
            plt.close(fig)
            return f'<div class="card"><img src="{img}"></div>'
        except ImportError:
            return '<div class="card"><p>matplotlib not available for charts</p></div>'

    def _strategy_table(self, r: dict) -> str:
        ps = r.get("per_strategy", {})
        if not ps:
            return ""
        rows = ""
        for name, data in ps.items():
            pnl_cls = "pos" if data.get("pnl", 0) > 0 else "neg"
            rows += (
                f'<tr><td>{name}</td><td>{data.get("trades", 0)}</td>'
                f'<td>{data.get("win_rate", 0):.1%}</td>'
                f'<td class="{pnl_cls}">${data.get("pnl", 0):.2f}</td>'
                f'<td>${data.get("fees", 0):.2f}</td></tr>'
            )
        return f"""<div class="card"><h2>Per-Strategy Breakdown</h2>
<table><tr><th>Strategy</th><th>Trades</th><th>Win Rate</th><th>PnL</th><th>Fees</th></tr>
{rows}</table></div>"""

    def _trades_table(self, r: dict) -> str:
        trades = r.get("trades_detail", [])
        if not trades:
            return ""
        sorted_trades = sorted(trades, key=lambda t: t.get("pnl", 0), reverse=True)
        best = sorted_trades[:5]
        worst = sorted_trades[-5:]

        def _row(t):
            cls = "pos" if t["pnl"] > 0 else "neg"
            return (
                f'<tr><td>{t["symbol"][:30]}</td><td>{t["strategy"]}</td>'
                f'<td>{t["contract_side"]}</td><td>{t["entry_price"]:.2f}</td>'
                f'<td>{t["quantity"]}</td>'
                f'<td class="{cls}">${t["pnl"]:.2f}</td></tr>'
            )

        best_rows = "".join(_row(t) for t in best)
        worst_rows = "".join(_row(t) for t in worst)
        header = "<tr><th>Symbol</th><th>Strategy</th><th>Side</th><th>Price</th><th>Qty</th><th>PnL</th></tr>"

        return f"""<div class="card"><h2>Best Trades</h2>
<table>{header}{best_rows}</table>
<h2>Worst Trades</h2>
<table>{header}{worst_rows}</table></div>"""
