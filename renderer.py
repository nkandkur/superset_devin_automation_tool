def render_metric_cards(m: dict) -> str:
    turnaround_display = f"{m['avg_turnaround']}h" if m["avg_turnaround"] is not None else "—"
    cards = [
        ("Issues Triggered", m["total"], None, "#2c3e50"),
        ("Completed", f"{m['completed']}", f"{m['completed_pct']}% of total", "#27ae60"),
        ("PRs Opened", m["prs_opened"], f"{m['pr_rate']}% of total", "#2980b9"),
        ("Active / In Progress", m["active"], None, "#f39c12"),
        ("Errors / Blocked", m["errored"], f"{m['errored_pct']}% of total", "#c0392b"),
        ("Avg Turnaround", turnaround_display, "issue → PR", "#8e44ad"),
    ]
    html = ""
    for label, value, sublabel, color in cards:
        sub = f'<div class="metric-sub">{sublabel}</div>' if sublabel else ""
        html += f"""
        <div class="metric-card" style="border-top: 4px solid {color};">
            <div class="metric-value" style="color: {color};">{value}</div>
            <div class="metric-label">{label}</div>
            {sub}
        </div>
        """
    return html