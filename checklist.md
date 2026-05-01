# CPDO Implementation Checklist

Work through each section in order. Each section corresponds to a notebook cell group.
Check off items as completed. Explanations of what was done are included under each section for review.

---

## Section 1 — Data Loading & EDA

- [x] 1.1 Load CDX.NA.IG 5Y data, parse dates, sort ascending, drop NaNs
- [x] 1.2 Load SOFR daily data, parse dates, convert from % to decimal, sort ascending
- [x] 1.3 Validate: check for gaps, duplicate dates, negative spreads
- [x] 1.4 Plot: CDX spread history with annotations (COVID-19 March 2020, missing GFC period)
- [x] 1.5 Plot: SOFR history
- [x] 1.6 Print summary statistics: mean, std, min, max, percentiles for spreads
- [x] 1.7 State explicitly: data covers 2011–2026, GFC (2007–2009) is absent, and what that implies for calibration

**What was done:**
Both CSV files use semicolons as delimiters and European decimal notation (commas instead of periods), requiring `.str.replace(',', '.')` before casting to float. SOFR rates were divided by 100 to convert from percent to decimal.

**Validation findings:** Both datasets are clean — no duplicates, no nulls, no negative spreads. CDX has 3,655 trading days (Sep 2011 – Apr 2026); SOFR has 5,579 days (Jan 2004 – Apr 2026).

**Key data statistics:** CDX spread mean = 69.6 bps, std = 19.0 bps, range = 43.8–151.8 bps. SOFR (overlapping period) mean = 2.1%, range = 0.19–4.95%.

**Critical limitation flagged:** The GFC (2007–2009) is absent from the CDX data. During the crisis, CDX.NA.IG spreads peaked at approximately 280 bps — nearly double our sample's maximum. Any model calibrated to this data will underestimate tail spread risk. This is precisely the same data blindspot that affected Moody's and S&P in 2005–2007, and it is addressed via stress testing in Section 9.

---

## Section 2 — CPDO Product Design

- [x] 2.1 Define and document all CPDO parameters in a single dataclass:
  - Initial NAV = 100
  - Target NAV = 150  ← CPDO "cashes out" here
  - Floor NAV = 10  ← CPDO "defaults" here (90% principal loss)
  - Max leverage = 15×
  - Tenor = 10 years
  - Investor coupon = 3M SOFR + 150 bps, paid quarterly
  - Management fee = 30 bps per annum
  - Roll frequency = every 6 months (steps 126 apart at dt=1/252)
  - Roll cost = 2 bps per roll
  - Recovery rate = 40% (LGD = 60%)
  - N names in index = 125
  - Risky duration (DV01 base) = 4.5 years
- [x] 2.2 Write a clear prose description of the product
- [x] 2.3 Describe all cash flow components with formulas (carry, MTM, interest, coupon, fee, roll, default loss)
- [x] 2.4 Describe the leverage mechanism and feedback loop

**What was done:**
All parameters are stored in a `CPDOParams` dataclass. Two derived constants computed at instantiation: `dv01 = risky_duration × 1e-4 = 0.00045` (sensitivity of $1 notional to a 1bp spread move) and `lgd = 1 − recovery_rate = 60%`.

**Cash flow anatomy (illustrated at NAV=100, S=70bps, SOFR=4%, year 1):**
At these values the leverage formula gives L = 7.94×, exposure = 793.7.
- Carry income: +5.556/yr
- Interest on collateral: +4.000/yr
- Investor coupon: −5.500/yr (SOFR + 150bps on par notional of 100)
- Management fee: −0.300/yr
- Net carry: +3.756/yr (before MTM and defaults)
- MTM per 1bp widening: −0.357 (a one-time event loss = 24 days of net carry)

**Leverage formula:** `L = shortfall / (nav × carry_rate × T_remaining)`, where shortfall = target_nav − nav, carry_rate = S/10000. This sets exposure so that carry income exactly fills the gap to target over the remaining tenor. Capped at max_leverage (15×), floored at 0.

**Why target_nav = 150:** Designed so the CPDO accumulates 50 in carry above principal (approx. 10yr × 5% annual coupon buffer) before cashing out. Coupon payments are deducted from NAV daily as they accrue.

**Investor coupon is on par (100), not current NAV.** When NAV falls below 100, the coupon drain accelerates the decline as a percentage of current NAV — this makes the product more fragile under stress and is consistent with standard CPDO structures.

**The leverage feedback loop:** Spread widening causes MTM losses (NAV falls), which increases the shortfall, which forces higher leverage, which amplifies the next spread move. This is the primary default mechanism — not individual name defaults.

---

## Section 3 — Spread Model: CIR Calibration

- [x] 3.1 State the CIR model: `dS = κ(θ - S)dt + σ√S dW`
- [x] 3.2 Implement OLS calibration on daily increments:
  - Regress `ΔS` on `[1, S_t]` to get `α, β`
  - `κ = -β / dt`, `θ = α / (κ × dt)`
  - **Correct sigma**: `σ = sqrt(mean(residuals² / (S_t × dt)))` — NOT `std(ΔS)/sqrt(dt)`
- [x] 3.3 Print calibrated parameters: κ, θ, σ, s₀
- [x] 3.4 Sanity check: κ > 0 (mean-reverting), θ > 0, σ > 0
- [x] 3.5 Plot: 20 simulated paths + distribution comparison vs historical

**What was done:**
Calibration by OLS on daily spread increments. The discretised CIR model is `ΔS = α + β·S_t + ε`, where `α = κθ·dt` and `β = −κ·dt`. Inverting gives κ and θ directly.

**Critical sigma correction:** The CIR diffusion variance is `σ²·S_t·dt` (not constant). The naive estimator `std(ΔS)/√dt` assumes constant variance and gives σ = 37.3 — nine times too large and would produce wildly unrealistic paths. The correct CIR estimator `σ = √(mean(ε²/(S_t·dt)))` gives σ = 4.06.

**Calibrated parameters:**
- κ = 2.28 (mean-reversion speed)
- θ = 67.23 bps (long-term mean; close to sample mean of 69.6 bps ✓)
- σ = 4.06 (CIR-correct volatility coefficient)
- s₀ = 54.29 bps (last observed spread)
- Half-life of mean reversion: 0.30 yr ≈ 77 trading days

**Distributional fit check (500 paths × 10yr):** Simulated mean = 66.6, std = 15.7 vs historical mean = 69.6, std = 19.0. The mean is well-captured but variance is lighter than historical — pure CIR misses the fat-tailed spread spikes. This motivates Section 4.

---

## Section 4 — Jump Component: Crisis Calibration

- [x] 4.1 Explain why pure CIR is insufficient (fat tails, sudden spread spikes)
- [x] 4.2 Identify large moves from historical data: |ΔS| ≥ 85th pct (both directions — signed)
- [x] 4.3 Fit jump distribution: empirical resample from crisis ΔS subsample
- [x] 4.4 Compute state-dependent jump probabilities (p_high ≤ 10%, p_low ≤ 5%)
- [x] 4.5 Print calibrated crisis parameters
- [x] 4.6 Implement `simulate_spread_paths()` combining CIR + jumps (daily, dt=1/252)
- [x] 4.7 Plot: 20 paths comparison + three-way distribution (historical / CIR / CIR+Jump)

**What was done:**
Large moves defined as |ΔS| ≥ 85th percentile of |ΔS| ≈ 2.5 bps. 548 such events identified (15% of days).

**Key design decision — both directions (signed), not widening-only:** Using only widening moves as jumps introduces a systematic upward drift: 7.6% of days with +4.6bps average jumps pushes the simulated mean to 111 bps vs historical 70 bps. Keeping both widening and tightening preserves the correct mean (jump mean ≈ +0.18 bps, nearly zero).

**Calibrated jump parameters:**
- Jump mean = +0.18 bps, std = 5.42 bps (signed, both directions)
- Regime threshold: spread > 82.7 bps (80th pct of historical)
- p_high = 10.0% (capped from raw 33.7%) — jump probability in high-spread regime
- p_low = 5.0% (capped from raw 10.3%) — jump probability in low-spread regime

**Jump implementation:** At each step, fire a jump with probability p_j (state-dependent on current spread). If it fires, draw one sample from the empirical jump distribution using `rng.choice(jump_sizes)`. This preserves the empirical shape without assuming Normality.

**Distributional fit with jumps (500 paths):** Simulated mean = 67.9, std = 18.7 vs historical mean = 69.6, std = 19.0. Near-perfect match. The jump component recovers the fat-tail variance that pure CIR underestimates.

**Runtime:** 10,000 paths × 2,520 steps ≈ 1.2 seconds.

---

## Section 5 — Default Loss Model

- [x] 5.1 CDX mechanics: N=125, each default costs (1/N) × LGD × exposure
- [x] 5.2 Risk-neutral λ = S / (10000 × LGD × risky_duration) — state-dependent, theoretically grounded
- [x] 5.3 n_defaults ~ Poisson(N×λ_rn×dt); loss = n_def × (1/N) × exposure × LGD
- [x] 5.4 Loss/carry = 1/risky_duration ≈ 22% by construction — MTM still dominant (1bp widen = 16 days carry)

**What was done:**
Implemented Approach B (Poisson aggregate) rather than Approach A (binary single-event). Each CDX default costs `(1/N) × exposure × LGD`, which at L=8×, NAV=100 equals 3.81 per name (0.48% of exposure). This is small and bounded.

**Risk-neutral λ vs physical λ:**
Used risk-neutral default intensity `λ = S / (10000 × LGD × risky_duration)` rather than a fixed physical rate (~0.15%/yr). The risk-neutral rate has clean theoretical grounding: it is the intensity implied by the CDS spread under the no-arbitrage condition spread ≈ λ × LGD × risky_duration. It is also state-dependent — higher spreads imply higher default intensity, which is more realistic.

The consequence: expected annual default loss = carry income / risky_duration ≈ 22% of carry. This is the risk-neutral break-even by construction. The CPDO's profitability comes from the spread risk premium (physical default rates are lower than risk-neutral, so actual losses are less than the 22% carried).

**Confirmed MTM dominance:** At L=8×, S=70bps: 1bp spread widening erases 16 days of net carry income. Default losses consume only 22% of carry. Spread dynamics (MTM) dominate default losses as the primary risk channel.

---

## Section 6 — CPDO Monte Carlo Simulation (Base Case)

- [x] 6.1 Implemented `run_cpdo_simulation()` with all cash flow components
- [x] 6.2 All components applied per step: leverage, carry, MTM, interest, coupon (quarterly on par), fee, roll, default loss, upper/lower triggers
- [x] 6.3 10,000 paths, T=10yr, dt=1/252, runtime ~2s
- [x] 6.4 Returns: final_nav, defaulted, cashed_out, event_year
- NOTE: Base case PD ≈ 0% — correct, mirrors why calm-data calibration gave AAA ratings. Stress tests in Section 9 will show non-trivial PD.

**What was done:**
`run_cpdo_simulation()` loops over 2,520 daily steps. At each step, for all 10,000 paths simultaneously (vectorised numpy):
1. Look up pre-generated spread S[t] and compute dS = S[t+1] − S[t]
2. Compute leverage L from the shortfall formula
3. Apply all cash flows: carry, interest, MTM, default loss, fee, roll (every 126 steps), quarterly coupon
4. Check both triggers: floor (NAV ≤ 10) and target (NAV ≥ 150)
5. Freeze and deactivate triggered paths

**Spread paths are pre-generated upfront** using `simulate_spread_paths()`, then indexed into at each step. This keeps the spread dynamics and NAV dynamics cleanly separated. Memory cost: 2521 × 10000 × 8 bytes ≈ 200MB (acceptable).

**Coupon is paid on par (initial_nav=100)** as a scalar, not on current NAV. This is consistent with standard CPDO contract terms and means the coupon drain is constant regardless of NAV — which makes low-NAV paths bleed faster.

**SOFR held constant** at last observed value (3.91%). SOFR and interest income effectively cancel (SOFR earned on collateral offsets SOFR component of investor coupon), so only the 150bps investor spread matters for NAV dynamics.

**Added optional parameters for stress testing:** `s0_override` (replaces cir.s0 for Scenario A) and `shock_bps / shock_year` (applies a permanent spread shift from a given year for Scenario B).

---

## Section 7 — Results & Analysis (Base Case)

- [x] 7.1 PD=0%, Cash-out=37%, Alive=63%, Mean NAV=119, Median=118
- [x] 7.2 50 sample NAV paths color-coded (green=cashed-out, blue=alive, red=defaulted)
- [x] 7.3 Final NAV histogram from 10,000 paths
- [~] 7.4 Skipped — PD=0% in base case, no defaults to plot
- [~] 7.5 Skipped — leverage dynamics clear from Section 2 formula
- [~] 7.6 Skipped — spread paths shown in Section 4

**What was done:**
A separate 50-path trajectory simulation (`record_cpdo_paths()`) records full NAV histories for plotting. The 10,000-path results from Section 6 are used for the distribution histogram.

**Base case findings:**
- PD = 0.00% — the floor (NAV=10) is never reached. Min final NAV across all 10,000 paths = ~40.
- 37% of paths cash out (hit NAV=150), typically within 3–5 years.
- The remaining 63% reach maturity with NAVs between ~40 and 150 (mean 119, median 118).
- Max simulated spread across all paths ≈ 200 bps; the GFC peak of 280 bps is never reached.

**Interpretation:** This near-zero PD under calm calibration is exactly the intended result — it mirrors why rating agencies assigned AAA to CPDOs in 2005–2007. A model calibrated to benign data shows no default risk. The danger only becomes apparent when realistic crisis dynamics are applied (Section 9).

---

## Section 8 — Rating Grade Assignment

- [ ] 8.1 Present standard annual PD to rating mapping table (S&P / Moody's scale)
- [ ] 8.2 Convert simulated 10-year PD to implied annual PD: `annual_PD = 1 - (1 - PD_10yr)^(1/10)`
- [ ] 8.3 Assign rating grade and justify based on the mapping
- [ ] 8.4 Comment on what rating this CPDO "should" have received vs what it got (AAA) in 2005

*(Not yet implemented)*

---

## Section 9 — Stress Test: Crisis Scenario

- [x] 9.1 Scenario A (s0=200bps): PD=0%, Cash-out=98% — high spreads → low leverage (2.5×) → carry fills gap quickly. Key insight: high-spread issuance is actually SAFER.
- [x] 9.2 Scenario B (+150bps shock at yr 3): PD=51%, Cash-out=2% — mid-life shock hits already-leveraged CPDO; NAV spiral drives half of paths to floor. This is the GFC story.
- [x] 9.3 2×2 plot: NAV paths + distributions for both scenarios
- [x] 9.4 Summary table: Base (0%) / Scenario A (0%, 98% CO) / Scenario B (51%)

**What was done:**

**Scenario A — Elevated starting spread (s0=200bps):**
The CIR model is re-run from s0=200bps (GFC-era level). All other parameters (κ, θ, σ, jump parameters) are unchanged.

Result: PD=0%, Cash-out=**98%**, Mean NAV=149.

Explanation: at s0=200bps, the leverage formula gives L = shortfall/(nav × carry_rate × T_rem) = 50/(100 × 0.02 × 10) = **2.5×** — very low. Because carry_rate (S/10000) is high, the CPDO needs only modest leverage to fill the shortfall. With 2.5× leverage, MTM sensitivity is tiny (0.11 per bp) and annual carry is massive (5 per year). Nearly all paths hit the target within 1–2 years before spreads have a chance to cause damage. **Key insight: a CPDO issued into a high-spread environment is actually safer because it requires less leverage.**

**Scenario B — Mid-life spread shock (+150bps at year 3):**
Pre-generated spread paths have +150bps added permanently from year 3 onward. The CIR mean-reversion is present within the existing paths but the level is shifted up. This models a structural credit regime change mid-life.

Result: PD=**51%**, Cash-out=2%, Mean NAV=35.

Explanation: by year 3, many paths have NAV still near 100 (not much progress toward 150) because of coupon drain and modest net carry. At that point, leverage from the formula is elevated (L ≈ 10-12×). When spreads jump by 150bps, MTM losses = L × 100 × 4.5e-4 × 150 ≈ 67-81 NAV points in one step — immediately near or through the floor. For paths that don't immediately default, the elevated spreads and higher jump probabilities (p_high=10%) drive continued deterioration. **This is exactly the GFC story: CPDOs issued in 2005 at ~30bps spreads were running at max leverage when spreads widened from 30→280bps in 2007–2008.**

**Summary table:**

| Scenario | PD | Cash-out | Mean NAV | Why |
|---|---|---|---|---|
| Base (s0=54bps) | 0% | 37% | 119 | Calm calibration; spreads never extreme |
| Scenario A (s0=200bps) | 0% | 98% | 149 | High spreads → low L (2.5×) → safe |
| Scenario B (+150bps yr3) | 51% | 2% | 35 | Mid-life shock hits high-L position; NAV spiral |

---

## Section 10 — Why Rating Agencies Got It Wrong

- [ ] 10.1 Explain how Moody's and S&P rated CPDOs at the time:
  - Used Gaussian copula / CDOROM to model joint default probabilities
  - Assumed low, stable default correlation between IG names
  - Relied on short historical samples (2002–2006) with no major credit stress
  - Assumed mean reversion would prevent prolonged spread widening
- [ ] 10.2 Identify the three core errors:
  - **Correlation underestimation**: IG names became highly correlated in a crisis — the copula assumption broke down
  - **Tail risk ignored**: Gaussian assumptions missed fat tails and spread gap risk
  - **Feedback loop not modelled**: No model captured the leverage spiral (spread widening → higher leverage → larger MTM losses → faster NAV collapse)
- [ ] 10.3 Connect to your own model:
  - Show that even with available (post-crisis) data and a jump model, your PD is non-trivial
  - Argue that a model calibrated to 2002–2006 data (no crisis, spreads ~25–50 bps) would produce a near-zero PD
  - Use your stress test results to demonstrate how sensitive PD is to crisis dynamics
- [ ] 10.4 Conclude with the rating implication: models that ignore fat tails and the leverage feedback loop will systematically understate PD, leading to AAA ratings that should have been BBB or worse

*(Not yet implemented)*

---

## Implementation Notes

- All simulations use `dt = 1/252` (daily)
- Random seed fixed for reproducibility (spread simulation: seed+1, CPDO simulation: seed)
- Vectorised numpy operations across paths at every step; the time loop over steps is unavoidable due to path-dependence of NAV
- Runtime: ~2s per 10,000-path simulation; ~7s for all three scenarios combined
- Notebook has 31 cells across Sections 1–7 and 9 (Section 8 and 10 pending)
