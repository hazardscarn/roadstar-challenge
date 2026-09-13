"""Deck-ready charts from sim/backtest/solver_backtest.py's daily_results.csv -- REAL vs. AI Mode
A ("as it was" -- only the drivers REAL actually used that day) vs. AI Mode B ("period pool" --
the full 42-driver South-Ontario roster every day), faceted by metric, at weekly/monthly/overall
granularity (real user ask: "facet wrap plot for each reward on the monthly side and overall
period" -- weekly added too since the real data only spans ~8.5 weeks / 2 calendar months, too
short for a monthly TREND to show anything; monthly and overall are still produced as the coarser
summary views actually asked for).

Colors: the project's validated 3-series categorical set (dataviz skill's reference palette,
slots 1-3 -- blue/orange/aqua, the ONLY 3 slots that clear the CVD floor under all-pairs
comparison, not just adjacent) -- REAL is always blue, Mode A always orange, Mode B always aqua,
in every chart.

Run: `python -m sim.backtest.plot_backtest` (after sim/backtest/solver_backtest.py has produced
the CSV) -- writes 3 PNGs to documents/results/dispatch_solver_backtest/.
"""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd

IN_CSV = 'documents/results/dispatch_solver_backtest/daily_results.csv'
OUT_DIR = 'documents/results/dispatch_solver_backtest'

REAL_COLOR = '#2a78d6'   # validated categorical slot 1 (blue)
A_COLOR = '#eb6834'      # validated categorical slot 2 (orange) -- AI, "as it was"
B_COLOR = '#1baf7a'      # validated categorical slot 3 (aqua) -- AI, "period pool"
INK = '#0b0b0b'
MUTED = '#898781'
GRID = '#e1e0d9'
SURFACE = '#fcfcfb'

SERIES = [('REAL', REAL_COLOR), ('AI - as it was', A_COLOR), ('AI - period pool', B_COLOR)]

# (key, label, real_col, a_col, b_col, kind)
METRICS = [
    ('net_revenue', 'Net revenue (CAD)', 'real_net', 'a_net_revenue', 'b_net_revenue', 'sum'),
    ('deadhead_miles', 'Deadhead miles', 'real_deadhead_miles', 'a_deadhead_miles', 'b_deadhead_miles', 'sum'),
    ('orders_missed', 'Orders missed', 'real_missed', 'a_unassigned', 'b_unassigned', 'sum'),
    ('service_rate', 'Service rate (%)', None, None, None, 'rate'),
]


def _style_axis(ax, title):
    ax.set_title(title, fontsize=11, color=INK, loc='left', fontweight='bold')
    ax.spines[['top', 'right']].set_visible(False)
    ax.spines[['left', 'bottom']].set_color(MUTED)
    ax.tick_params(colors=MUTED, labelsize=8.5)
    ax.grid(axis='y', color=GRID, linewidth=0.8, zorder=0)
    ax.set_facecolor(SURFACE)


def _rate(df: pd.DataFrame, served_col: str, missed_col: str) -> float:
    total = df[served_col].sum() + df[missed_col].sum()
    return (df[served_col].sum() / total * 100) if total else 0.0


def _series_values(grouped, real_col, a_col, b_col, kind):
    if kind == 'sum':
        return grouped[real_col].sum(), grouped[a_col].sum(), grouped[b_col].sum()
    real_vals = grouped.apply(lambda g: _rate(g, 'real_dispatched', 'real_missed'), include_groups=False)
    a_vals = grouped.apply(lambda g: _rate(g, 'a_assigned', 'a_unassigned'), include_groups=False)
    b_vals = grouped.apply(lambda g: _rate(g, 'b_assigned', 'b_unassigned'), include_groups=False)
    return real_vals, a_vals, b_vals


def _weekly_facet(df: pd.DataFrame) -> None:
    fig, axes = plt.subplots(2, 2, figsize=(13, 8.5), facecolor=SURFACE)
    fig.suptitle('REAL vs. AI (CP-SAT) — weekly', fontsize=13, color=INK, fontweight='bold', x=0.02, ha='left')
    weekly = df.groupby('week')
    for ax, (key, label, real_col, a_col, b_col, kind) in zip(axes.flat, METRICS):
        real_s, a_s, b_s = _series_values(weekly, real_col, a_col, b_col, kind)
        for series, (name, color) in zip((real_s, a_s, b_s), SERIES):
            ax.plot(series.index, series.values, color=color, linewidth=2, marker='o', markersize=5, label=name)
        _style_axis(ax, label)
        ax.tick_params(axis='x', rotation=30)
    axes.flat[0].legend(frameon=False, fontsize=8.5, loc='upper left', labelcolor=INK)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(f'{OUT_DIR}/weekly_facet.png', dpi=160, facecolor=SURFACE)
    plt.close(fig)


def _bar_facet(df: pd.DataFrame, group_col: str, title: str, out_name: str) -> None:
    fig, axes = plt.subplots(2, 2, figsize=(12, 8.5), facecolor=SURFACE)
    fig.suptitle(title, fontsize=13, color=INK, fontweight='bold', x=0.02, ha='left')
    grouped = df.groupby(group_col)
    for ax, (key, label, real_col, a_col, b_col, kind) in zip(axes.flat, METRICS):
        real_vals, a_vals, b_vals = _series_values(grouped, real_col, a_col, b_col, kind)
        x = range(len(real_vals))
        width = 0.26
        for offset, (series, (name, color)) in zip((-1, 0, 1), zip((real_vals, a_vals, b_vals), SERIES)):
            bars = ax.bar([i + offset * width for i in x], series.values, width, color=color, label=name)
            ax.bar_label(bars, fmt=lambda v: f'{v:,.0f}', fontsize=6.5, color=MUTED, padding=2)
        ax.set_xticks(list(x))
        ax.set_xticklabels([str(v) for v in real_vals.index], fontsize=8.5)
        _style_axis(ax, label)
    axes.flat[0].legend(frameon=False, fontsize=8.5, loc='upper left', labelcolor=INK)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(f'{OUT_DIR}/{out_name}.png', dpi=160, facecolor=SURFACE)
    plt.close(fig)


def _overall_summary(df: pd.DataFrame) -> None:
    fig, ax = plt.subplots(1, 4, figsize=(15, 4.2), facecolor=SURFACE)
    fig.suptitle(
        f'REAL vs. AI (CP-SAT) — {len(df)}-day overall totals',
        fontsize=13, color=INK, fontweight='bold', x=0.02, ha='left',
    )
    for a, (key, label, real_col, a_col, b_col, kind) in zip(ax, METRICS):
        if kind == 'sum':
            vals = [df[real_col].sum(), df[a_col].sum(), df[b_col].sum()]
        else:
            vals = [
                _rate(df, 'real_dispatched', 'real_missed'),
                _rate(df, 'a_assigned', 'a_unassigned'),
                _rate(df, 'b_assigned', 'b_unassigned'),
            ]
        names = ['REAL', 'AI\n(as it was)', 'AI\n(period pool)']
        colors = [c for _, c in SERIES]
        bars = a.bar(names, vals, color=colors, width=0.6)
        a.bar_label(bars, fmt=lambda v: f'{v:,.0f}', fontsize=9.5, color=INK, padding=3, fontweight='bold')
        _style_axis(a, label)
        a.tick_params(axis='x', labelsize=8.5)
    fig.tight_layout(rect=[0, 0, 1, 0.90])
    fig.savefig(f'{OUT_DIR}/overall_summary.png', dpi=160, facecolor=SURFACE)
    plt.close(fig)


def make_plots() -> None:
    df = pd.read_csv(IN_CSV, parse_dates=['day'])
    df['week'] = df['day'].dt.to_period('W').apply(lambda p: p.start_time.date())
    df['month'] = df['day'].dt.to_period('M').astype(str)

    _weekly_facet(df)
    _bar_facet(df, 'month', 'REAL vs. AI (CP-SAT) — monthly', 'monthly_facet')
    _overall_summary(df)
    print(f"Wrote weekly_facet.png, monthly_facet.png, overall_summary.png to {OUT_DIR}/")


if __name__ == "__main__":
    make_plots()
