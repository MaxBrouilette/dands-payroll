"""
4x10 Schedule Restructure — Reference Guide
Standalone Streamlit app for presenting the 4x10 transition plan.
"""
import streamlit as st
import pandas as pd
from employer_config import EMPLOYER as _CO

st.set_page_config(page_title="4x10 Schedule Guide", page_icon="📋", layout="wide")

# ── Custom CSS ──
st.markdown("""
<style>
    .big-number { font-size: 2.2rem; font-weight: 700; margin: 0; }
    .big-label  { font-size: 0.9rem; color: #666; margin: 0; }
    .section-hdr { font-size: 1.1rem; font-weight: 600; margin-top: 1.5rem; margin-bottom: 0.5rem; }
    .green-box  { background: #d4edda; padding: 12px 16px; border-radius: 8px; margin: 8px 0; }
    .blue-box   { background: #e3f2fd; padding: 12px 16px; border-radius: 8px; margin: 8px 0; }
    .orange-box { background: #fff3e0; padding: 12px 16px; border-radius: 8px; margin: 8px 0; }
    .grey-box   { background: #f5f5f5; padding: 12px 16px; border-radius: 8px; margin: 8px 0; }
    .roi-bar    { background: linear-gradient(90deg, #1b5e20 0%, #388e3c 100%);
                  color: white; padding: 14px 20px; border-radius: 8px; margin-top: 2rem;
                  display: flex; justify-content: space-between; align-items: center; }
    .roi-bar span { font-size: 0.95rem; }
    .roi-bar strong { font-size: 1.15rem; }
    .coverage-in  { background: #c8e6c9; padding: 6px 10px; border-radius: 4px;
                    text-align: center; font-weight: 600; font-size: 0.85rem; }
    .coverage-off { background: #ffcdd2; padding: 6px 10px; border-radius: 4px;
                    text-align: center; color: #b71c1c; font-size: 0.85rem; }
    .stat-dodge   { background: #c8e6c9; text-align: center; font-weight: 600; }
    .stat-work    { background: #fff9c4; text-align: center; }
    .stat-na      { background: #f5f5f5; text-align: center; color: #999; }
</style>
""", unsafe_allow_html=True)

st.title("4x10 Schedule Restructure")
st.caption(f"{_CO['operating_as']} — {_CO['legal_name']}")

# ──────────────────────────────────────────────
# ROI FOOTER (reusable)
# ──────────────────────────────────────────────
def roi_footer():
    st.markdown("""
    <div class="roi-bar">
        <span>Hard Savings <strong>$7,563/yr</strong></span>
        <span>Efficiency Gains <strong>$7,846/yr</strong></span>
        <span>Total Return <strong>$15,408/yr</strong></span>
        <span>Settlement <strong>$3,430</strong></span>
        <span>Payback <strong>2.7 months</strong></span>
    </div>
    """, unsafe_allow_html=True)


# ──────────────────────────────────────────────
# TABS
# ──────────────────────────────────────────────
tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "Overview", "Employer Savings", "Efficiency Gains",
    "Employee Benefits", "Stat Holidays", "Settlements & Compliance"
])

# ══════════════════════════════════════════════
# TAB 1 — OVERVIEW
# ══════════════════════════════════════════════
with tab1:
    st.header("Executive Summary")

    # Hero metrics
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Annual Return", "$15,408", delta="+$15,408/yr")
    m2.metric("One-Time Cost", "$3,430", delta="-$3,430")
    m3.metric("Payback Period", "2.7 months")
    m4.metric("Compliance Risk Eliminated", "$8,585/yr", delta="+$8,585 saved")

    st.markdown("---")

    # Schedule assignments
    st.subheader("Schedule Assignments")
    sched_df = pd.DataFrame({
        "Employee": ["Danielle", "Petro", "Tiffany", "Lovepreet"],
        "Role": ["Production", "Designer", "Installer", "Installer"],
        "Current Schedule": ["5x8.5 @ $26/hr", "5x9 @ $23.25/hr", "5x8 @ $34/hr", "5x8 @ $24/hr"],
        "New Schedule": ["4x10 @ $27/hr", "4x10 @ $23.25/hr", "4x10 @ $34/hr", "4x10 @ $24/hr"],
        "Off Day": ["Monday", "Friday", "Friday", "Monday"],
        "Rate Change": ["+$1.00 raise", "No change", "No change", "No change"],
    })

    def style_sched(df):
        s = pd.DataFrame("", index=df.index, columns=df.columns)
        for i, row in df.iterrows():
            if row["Off Day"] == "Monday":
                s.loc[i, "Off Day"] = "background-color: #e3f2fd; font-weight: bold"
            else:
                s.loc[i, "Off Day"] = "background-color: #fff3e0; font-weight: bold"
            if row["Rate Change"] != "No change":
                s.loc[i, "Rate Change"] = "background-color: #d4edda; font-weight: bold"
        return s

    st.dataframe(sched_df.style.apply(style_sched, axis=None), use_container_width=True, hide_index=True)

    st.markdown("---")

    # Weekly coverage grid
    st.subheader("Weekly Coverage")
    st.caption("Green = working | Red = off day")

    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    employees_coverage = {
        "Danielle (Production)": ["OFF", "IN", "IN", "IN", "IN"],
        "Petro (Designer)":      ["IN",  "IN", "IN", "IN", "OFF"],
        "Tiffany (Installer)":   ["IN",  "IN", "IN", "IN", "OFF"],
        "Lovepreet (Installer)": ["OFF", "IN", "IN", "IN", "IN"],
    }

    # Build HTML table
    html = '<table style="width:100%; border-collapse:collapse; margin:8px 0;">'
    html += '<tr style="border-bottom:2px solid #ddd;">'
    html += '<th style="text-align:left; padding:8px;"></th>'
    for d in days:
        html += f'<th style="text-align:center; padding:8px;">{d}</th>'
    html += '</tr>'

    for emp, statuses in employees_coverage.items():
        html += '<tr>'
        html += f'<td style="padding:8px; font-weight:600;">{emp}</td>'
        for s in statuses:
            if s == "IN":
                html += '<td><div class="coverage-in">IN</div></td>'
            else:
                html += '<td><div class="coverage-off">OFF</div></td>'
        html += '</tr>'

    # Headcount row
    html += '<tr style="border-top:2px solid #ddd;">'
    html += '<td style="padding:8px; font-weight:700;">Headcount</td>'
    for i in range(5):
        count = sum(1 for emp in employees_coverage.values() if emp[i] == "IN")
        html += f'<td style="text-align:center; padding:8px; font-weight:700; font-size:1.1rem;">{count}</td>'
    html += '</tr>'
    html += '</table>'

    st.markdown(html, unsafe_allow_html=True)

    st.info("**Full Mon-Fri coverage maintained.** Tue-Thu all 4 staff are in. "
            "Mon and Fri operate with 2 staff each — leaner, more independent, more accountable.")

    roi_footer()

# ══════════════════════════════════════════════
# TAB 2 — EMPLOYER SAVINGS
# ══════════════════════════════════════════════
with tab2:
    st.header("Employer Cost Comparison")

    # Per-employee 3-column table
    cost_df = pd.DataFrame({
        "Employee": ["Danielle ($27)", "Petro ($23.25)", "Tiffany ($34)", "Lovepreet ($24)", "TOTAL"],
        "A: Paying Now": ["$59,117.50", "$55,346.62", "$73,168.00", "$51,648.00", "$239,280.12"],
        "B: Compliant": ["$62,796.50", "$60,252.38", "$73,168.00", "$51,648.00", "$247,864.88"],
        "C: 4x10": ["$57,510.00", "$49,987.50", "$73,100.00", "$51,120.00", "$231,717.50"],
        "Save vs Current (A-C)": ["$1,607.50", "$5,359.12", "$68.00", "$528.00", "$7,562.62"],
        "Save vs Compliant (B-C)": ["$5,286.50", "$10,264.88", "$68.00", "$528.00", "$16,147.38"],
    })

    def style_cost(df):
        s = pd.DataFrame("", index=df.index, columns=df.columns)
        for i in range(len(df)):
            s.iloc[i]["C: 4x10"] = "background-color: #d4edda; font-weight: bold"
            s.iloc[i]["Save vs Current (A-C)"] = "background-color: #d4edda"
        s.iloc[-1] = "font-weight: bold; background-color: #e8f5e9"
        return s

    st.dataframe(cost_df.style.apply(style_cost, axis=None), use_container_width=True, hide_index=True)

    c1, c2, c3 = st.columns(3)
    c1.metric("Save vs Current", "$7,563/yr", delta="+$7,563")
    c2.metric("Save vs Compliant", "$16,147/yr", delta="+$16,147")
    c3.metric("Compliance Exposure Eliminated", "$8,585/yr", delta="+$8,585")

    st.markdown("---")

    # Danielle deep dive
    st.subheader("Danielle Deep Dive — The $1 Raise Play")

    st.markdown("""
    <div class="blue-box">
    <strong>The pitch:</strong> Give Danielle a $1/hr raise ($26 → $27) while moving her to 4x10.
    She sees a raise. You save $1,608/yr. Everyone wins.
    </div>
    """, unsafe_allow_html=True)

    d1, d2, d3 = st.columns(3)
    d1.metric("Current Annual Pay", "$59,117.50", help="5x8.5 @ $26/hr + 5 stats worked at 1.5x")
    d2.metric("New Annual Pay (4x10 @ $27)", "$57,510.00", help="4x10 @ $27/hr + 5 stats x ADW $270")
    d3.metric("Your Savings", "$1,607.50/yr", delta="+$1,608")

    st.markdown("**Her weekly difference:** -$26/week")

    st.markdown("""
    <div class="green-box">
    <strong>What -$26/week buys her:</strong><br>
    &bull; 52 Mondays off = three-day weekends every week ($0.50/day)<br>
    &bull; Never works a stat holiday again (was working 5 of 9)<br>
    &bull; $270/yr gas savings (1,800 fewer km driven)<br>
    &bull; 58 hrs/yr commute time back (includes lunch trips for dog)<br>
    &bull; $1,547 settlement cheque<br>
    &bull; A $1/hr raise she can tell everyone about
    </div>
    """, unsafe_allow_html=True)

    roi_footer()

# ══════════════════════════════════════════════
# TAB 3 — EFFICIENCY GAINS
# ══════════════════════════════════════════════
with tab3:
    st.header("Efficiency Gains")
    st.caption("Time recovered and converted to dollar value — conservative estimates")

    eff_df = pd.DataFrame({
        "Category": [
            "Owner time back (no daily small talk Mon)",
            "Reduced social overlap (Danielle/Tiffany)",
            "Danielle productivity recovery",
            "Solo day accountability (catch issues faster)",
            "Petro less burned out (fewer commute days)",
            "TOTAL"
        ],
        "Hours Recovered": ["12.5 hrs/yr", "66.7 hrs/yr", "75.0 hrs/yr", "—", "50.0 hrs/yr", "204.2 hrs/yr"],
        "Rate/Basis": ["$50/hr (owner)", "$30.50/hr (avg)", "$27/hr", "Est. rework saved", "$23.25/hr", ""],
        "Annual Value": ["$625", "$2,033", "$2,025", "$2,000", "$1,163", "$7,846"],
    })

    def style_eff(df):
        s = pd.DataFrame("", index=df.index, columns=df.columns)
        s.iloc[-1] = "font-weight: bold; background-color: #e8f5e9"
        s.iloc[:, -1] = "background-color: #d4edda"
        s.iloc[-1, -1] = "font-weight: bold; background-color: #c8e6c9"
        return s

    st.dataframe(eff_df.style.apply(style_eff, axis=None), use_container_width=True, hide_index=True)

    e1, e2 = st.columns(2)
    e1.metric("Total Efficiency Gains", "$7,846/yr")
    e2.metric("Total Hours Recovered", "204 hrs/yr")

    st.markdown("---")
    st.subheader("Strategic Benefits")

    s1, s2 = st.columns(2)
    with s1:
        st.markdown("""
        <div class="blue-box">
        <strong>Women Separation</strong><br>
        Danielle (off Mon) and Tiffany (off Fri) overlap <strong>3 days</strong> instead of 5.
        20 min/day x 2 people x 2 fewer days x 50 weeks = <strong>67 hrs/yr</strong> of recovered productivity.
        Less drama, less ego collisions, more independence.
        </div>
        """, unsafe_allow_html=True)

        st.markdown("""
        <div class="blue-box">
        <strong>Culture Shift</strong><br>
        From "grind 5 days" to "perform 4 days, earn your time off."
        People don't quit jobs that give them three-day weekends.
        Retention up, recruitment easier, morale higher.
        </div>
        """, unsafe_allow_html=True)

    with s2:
        st.markdown("""
        <div class="blue-box">
        <strong>Solo Day Accountability</strong><br>
        Mon and Fri each person operates independently — 2 days/week with no one to lean on.
        Performance issues become visible immediately.
        When one person's output drops on solo days, you know exactly where the issue is.
        </div>
        """, unsafe_allow_html=True)

        st.markdown("""
        <div class="blue-box">
        <strong>Owner Freedom</strong><br>
        52 days/year without the strongest personality in the building.
        Tue-Thu = full crew collaboration. Mon/Fri = lean ops, time to think, plan, build.
        Stop babysitting, start leading.
        </div>
        """, unsafe_allow_html=True)

    roi_footer()

# ══════════════════════════════════════════════
# TAB 4 — EMPLOYEE BENEFITS
# ══════════════════════════════════════════════
with tab4:
    st.header("Employee Benefits")
    st.caption("What each person gains from the 4x10 transition")

    # ── DANIELLE ──
    with st.expander("Danielle — Production ($26 → $27/hr, off Monday)", expanded=True):
        dc1, dc2, dc3, dc4 = st.columns(4)
        dc1.metric("Pay Change", "-$26/wk", delta="-$1,608/yr", delta_color="inverse")
        dc2.metric("Gas Savings", "+$270/yr", delta="+$270")
        dc3.metric("Commute Time Back", "58 hrs/yr")
        dc4.metric("Settlement Cheque", "$1,547")

        st.markdown("""
        <div class="green-box">
        <strong>The full picture:</strong><br>
        &bull; $1/hr raise — she tells people she got promoted<br>
        &bull; 52 Mondays off with her dog — $0.50/day for a three-day weekend<br>
        &bull; Never works a stat again (was working 5 of 9)<br>
        &bull; Gets back 5 stat days + 52 Mondays = <strong>57 extra days off/yr</strong><br>
        &bull; 1,800 fewer km driven (including lunch trips home for dog)<br>
        &bull; One fewer "startup" per week — less tardiness impact
        </div>
        """, unsafe_allow_html=True)

        st.markdown("**Pitch order:** Last. Let her see everyone else already signed. "
                     "Settlement cheque + raise + Mondays off kills the ego resistance.")

    # ── PETRO ──
    with st.expander("Petro — Designer ($23.25/hr, off Friday)", expanded=True):
        pc1, pc2, pc3, pc4 = st.columns(4)
        pc1.metric("Pay Change", "-$5,359/yr", delta="-$5,359", delta_color="inverse")
        pc2.metric("Commute Time Back", "150 hrs/yr", help="3hr bus round trip x 50 weeks")
        pc3.metric("Time Value", "$3,488/yr")
        pc4.metric("Settlement Cheque", "$1,883")

        st.markdown("""
        <div class="green-box">
        <strong>The full picture:</strong><br>
        &bull; Currently: 9hr days + 3hr bus commute = <strong>12-hour days, 5 days a week</strong><br>
        &bull; New: 10hr days + 3hr commute but only 4 days = gains a full day of life every week<br>
        &bull; <strong>150 hours/year</strong> of bus time gone — almost a month of waking hours<br>
        &bull; $1,883 settlement cheque he didn't know he was owed<br>
        &bull; Pay drops on paper but he was never getting paid for those 5 OT hours anyway<br>
        &bull; Net with time value: only -$1,872/yr — and that's before the settlement
        </div>
        """, unsafe_allow_html=True)

        st.markdown("**Pitch order:** Second. The commute story sells itself. "
                     "This kid will be your most loyal employee.")

    # ── TIFFANY ──
    with st.expander("Tiffany — Installer ($34/hr, off Friday)", expanded=True):
        tc1, tc2, tc3, tc4 = st.columns(4)
        tc1.metric("Pay Change", "-$68/yr", delta="-$68", delta_color="inverse")
        tc2.metric("Gas Savings", "+$105/yr", delta="+$105")
        tc3.metric("Commute Time Back", "21 hrs/yr")
        tc4.metric("Net w/ Time Value", "+$745/yr", delta="+$745")

        st.markdown("""
        <div class="green-box">
        <strong>The full picture:</strong><br>
        &bull; Same 40 hours, just compressed — pay barely moves (-$68/yr, invisible)<br>
        &bull; <strong>Cash positive</strong> after gas savings: +$37/yr tangible<br>
        &bull; +$745/yr including time value<br>
        &bull; Every Friday off<br>
        &bull; 6 months in, still in "prove myself" phase — solo days build confidence
        </div>
        """, unsafe_allow_html=True)

        st.markdown("**Pitch order:** First. Instant yes. Builds momentum for everyone else.")

    # ── LOVEPREET ──
    with st.expander("Lovepreet — Installer ($24/hr, off Monday)", expanded=True):
        lc1, lc2, lc3, lc4 = st.columns(4)
        lc1.metric("Pay Change", "-$528/yr", delta="-$528", delta_color="inverse")
        lc2.metric("Gas Savings", "+$540/yr", delta="+$540")
        lc3.metric("Commute Time Back", "50 hrs/yr")
        lc4.metric("Net w/ Time Value", "+$1,212/yr", delta="+$1,212")

        st.markdown("""
        <div class="green-box">
        <strong>The full picture:</strong><br>
        &bull; Same 40 hours, just compressed<br>
        &bull; <strong>Cash positive</strong>: gas savings ($540) exceed pay reduction ($528) = +$12/yr tangible<br>
        &bull; 36km drive each way — 3,600 fewer km/yr, 50 hours of windshield time back<br>
        &bull; Every Monday off<br>
        &bull; 8 months in — won't complain about $10/week less when he saves more than that in gas
        </div>
        """, unsafe_allow_html=True)

        st.markdown("**Pitch order:** Second (with Tiffany). Same easy sell, same instant yes.")

    st.markdown("---")
    st.subheader("Recommended Pitch Order")
    st.markdown("""
    1. **Tiffany + Lovepreet** — instant yes from both, builds momentum
    2. **Petro** — the commute story sells itself, settlement cheque seals it
    3. **Danielle** — sees everyone already signed, settlement + raise + Mondays off kills the ego
    """)

    roi_footer()

# ══════════════════════════════════════════════
# TAB 5 — STAT HOLIDAYS
# ══════════════════════════════════════════════
with tab5:
    st.header("Stat Holiday Strategy")

    st.subheader("Alberta's 9 General Holidays (2026)")
    stat_df = pd.DataFrame({
        "Holiday": [
            "New Year's Day", "Family Day", "Good Friday",
            "Victoria Day", "Canada Day", "Labour Day",
            "Thanksgiving", "Remembrance Day", "Christmas Day"
        ],
        "2026 Date": [
            "Jan 1 (Thu)", "Feb 16 (Mon)", "Apr 3 (Fri)",
            "May 18 (Mon)", "Jul 1 (Wed)", "Sep 7 (Mon)",
            "Oct 12 (Mon)", "Nov 11 (Wed)", "Dec 25 (Fri)"
        ],
        "Day": ["Thu", "Mon", "Fri", "Mon", "Wed", "Mon", "Mon", "Wed", "Fri"],
        "Mon-Off Dodges": ["", "DODGE", "", "DODGE", "", "DODGE", "DODGE", "", ""],
        "Fri-Off Dodges": ["", "", "DODGE", "", "", "", "", "", "DODGE"],
    })

    def style_stats(df):
        s = pd.DataFrame("", index=df.index, columns=df.columns)
        for i, row in df.iterrows():
            if row["Mon-Off Dodges"] == "DODGE":
                s.loc[i, "Mon-Off Dodges"] = "background-color: #c8e6c9; font-weight: bold; text-align: center"
            if row["Fri-Off Dodges"] == "DODGE":
                s.loc[i, "Fri-Off Dodges"] = "background-color: #c8e6c9; font-weight: bold; text-align: center"
        return s

    st.dataframe(stat_df.style.apply(style_stats, axis=None), use_container_width=True, hide_index=True)

    sc1, sc2, sc3 = st.columns(3)
    sc1.metric("Mon-Off Dodges", "4 of 9 stats", help="Family Day, Victoria Day, Labour Day, Thanksgiving")
    sc2.metric("Fri-Off Dodges", "2 of 9 stats", help="Good Friday, Christmas")
    sc3.metric("Combined Coverage", "8 of 9", help="Only New Year's (Thu) has nobody on their off day")

    st.markdown("---")
    st.subheader("How Stat Dodging Works")

    st.markdown("""
    <div class="blue-box">
    <strong>The 5 of 9 Rule (Alberta ESA)</strong><br><br>
    A day of the week is only a "regular workday" for an employee if they worked that day
    at least <strong>5 of the last 9 occurrences</strong>.<br><br>
    If the stat falls on their <strong>off day</strong> (not a regular workday), they owe nothing —
    no ADW, no 1.5x. You <em>can</em> call them in, and you only pay <strong>1.5x</strong> (no ADW on top).
    </div>
    """, unsafe_allow_html=True)

    st.markdown("---")
    st.subheader("Call-In Economics")

    econ_df = pd.DataFrame({
        "Scenario": [
            "Regular workday stat (5x8 employee works it)",
            "Off-day stat (4x10 employee called in)",
            "Nobody works it"
        ],
        "Pay Owed": [
            "1.5x hours + ADW",
            "1.5x hours only",
            "ADW (regular day) or $0 (off day)"
        ],
        "Cost @ $26/hr, 8hrs": [
            "$312 + $208 = $520",
            "$312 (no ADW)",
            "$208 or $0"
        ],
        "Savings vs Regular": [
            "—",
            "$208 saved (no ADW)",
            "$520 or $312 saved"
        ]
    })

    st.dataframe(econ_df, use_container_width=True, hide_index=True)

    st.success("**Bottom line:** Off-day call-ins cost 40% less than regular-day stat work. "
               "The 4x10 schedule makes every stat cheaper by default.")

    roi_footer()

# ══════════════════════════════════════════════
# TAB 6 — SETTLEMENTS & COMPLIANCE
# ══════════════════════════════════════════════
with tab6:
    st.header("Settlements & Compliance")

    st.subheader("Settlement Breakdown")

    settle_df = pd.DataFrame({
        "Employee": ["Danielle", "Petro", "TOTAL"],
        "Issue": [
            "7 stats — missed ADW payments",
            "9 stats — missed ADW payments",
            ""
        ],
        "Calculation": [
            "7 x $221.00 (ADW at $26 x 8.5hrs)",
            "9 x $209.25 (ADW at $23.25 x 9hrs)",
            ""
        ],
        "Amount": ["$1,547.00", "$1,883.25", "$3,430.25"],
    })

    def style_settle(df):
        s = pd.DataFrame("", index=df.index, columns=df.columns)
        s.iloc[-1] = "font-weight: bold; background-color: #fff3e0"
        s.iloc[:, -1] = "background-color: #fff3e0"
        return s

    st.dataframe(settle_df.style.apply(style_settle, axis=None), use_container_width=True, hide_index=True)

    sc1, sc2, sc3 = st.columns(3)
    sc1.metric("Total Settlement", "$3,430.25")
    sc2.metric("Payback from Savings", "2.7 months")
    sc3.metric("Annual Savings After", "$15,408/yr")

    st.markdown("---")
    st.subheader("Why These Numbers")

    st.markdown("""
    <div class="grey-box">
    <strong>What's owed vs what's being paid:</strong><br><br>
    &bull; <strong>No OT owed</strong> — both Danielle (5x8.5) and Petro (5x9) had mutual verbal agreements
    on their schedules. The overtime was understood and accepted by both parties.<br><br>
    &bull; <strong>Stat ADW at 1x only</strong> — paying back the average daily wage for stats they either
    worked without ADW or had off without pay. Paying 1x (not 1.5x+ADW) because they were happy with
    their arrangement and never raised it.<br><br>
    &bull; <strong>Settlement buys a clean slate</strong> — in exchange for the cheque, they sign the 4x10
    averaging agreement. Both sides benefit: they get back-pay + a better schedule,
    you get compliance + cost savings going forward.<br><br>
    &bull; <strong>Tiffany & Lovepreet</strong> — no settlement needed. Both under 1 year,
    all stats have been paid correctly.
    </div>
    """, unsafe_allow_html=True)

    st.markdown("---")
    st.subheader("Compliance Before vs After")

    comp_df = pd.DataFrame({
        "Area": [
            "Overtime (Danielle 5x8.5)",
            "Overtime (Petro 5x9)",
            "Stat Holiday ADW",
            "Averaging Agreements",
            "Overall Status"
        ],
        "Before (Current)": [
            "No OT paid on 0.5hr/day — verbal only",
            "No OT paid on 1hr/day — verbal only",
            "Missing for worked + unworked stats",
            "None signed (verbal)",
            "EXPOSED — $8,585/yr risk"
        ],
        "After (4x10)": [
            "No daily OT (10hr threshold w/ agreement)",
            "No daily OT (10hr threshold w/ agreement)",
            "ADW auto-correct via schedule dodge",
            "Signed averaging agreements for all",
            "COMPLIANT"
        ]
    })

    def style_comp(df):
        s = pd.DataFrame("", index=df.index, columns=df.columns)
        s.iloc[:, 1] = "background-color: #ffcdd2"
        s.iloc[:, 2] = "background-color: #c8e6c9"
        s.iloc[-1] = "font-weight: bold"
        s.iloc[-1, 1] = "font-weight: bold; background-color: #ffcdd2; color: #b71c1c"
        s.iloc[-1, 2] = "font-weight: bold; background-color: #c8e6c9; color: #1b5e20"
        return s

    st.dataframe(comp_df.style.apply(style_comp, axis=None), use_container_width=True, hide_index=True)

    st.success("**After implementation:** Zero compliance exposure. Every employee on a signed averaging "
               "agreement. All stat obligations handled automatically by the schedule structure.")

    roi_footer()
