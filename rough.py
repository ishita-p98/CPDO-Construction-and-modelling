# =========================================================
# CPDO MONTE CARLO SIMULATION ENGINE (FULLY COMMENTED)
# =========================================================

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from dataclasses import dataclass


# =========================================================
# PARAMETER CLASSES
# =========================================================

@dataclass
class JumpCIRParams:
    """
    Parameters of the Jump-CIR model
    """
    kappa: float        # speed of mean reversion
    theta: float        # long-run mean spread
    sigma: float        # volatility coefficient
    lam: float          # jump intensity (Poisson rate)
    jump_sizes: np.ndarray  # empirical jump size distribution
    s0: float           # initial spread


@dataclass
class CPDOParams:
    """
    CPDO structural parameters
    """
    target_nav: float = 100.0
    floor_nav: float = 10.0
    max_leverage: float = 15.0
    min_leverage: float = 1.0
    dv01: float = 5.0
    risk_free_rate: float = 0.02
    management_fee: float = 0.0025  # 25 bps
    roll_cost_bps: float = 1.5
    roll_every_steps: int = 26


# =========================================================
# 1. CALIBRATION FUNCTION
# =========================================================

def calibrate_jump_cir(spread_series, dt=1/52):
    """
    Calibrate Jump-CIR model from historical spreads

    Steps:
    1. Estimate mean reversion (kappa, theta)
    2. Estimate volatility (sigma)
    3. Detect jumps
    4. Estimate jump intensity and size distribution
    """

    s = pd.Series(spread_series).dropna().reset_index(drop=True)

    # Spread level and changes
    s_t = s[:-1].values
    ds = s.diff().dropna().values

    # -----------------------------
    # Mean reversion estimation
    # -----------------------------
    # ΔS = α + β S
    X = np.vstack([np.ones_like(s_t), s_t]).T
    alpha, beta = np.linalg.lstsq(X, ds, rcond=None)[0]

    # Map regression to CIR parameters
    kappa = -beta / dt
    theta = alpha / (kappa * dt)

    # -----------------------------
    # Jump detection (3-sigma rule)
    # -----------------------------
    std_ds = np.std(ds)
    jump_mask = np.abs(ds) > 3 * std_ds

    # -----------------------------
    # Volatility estimation
    # -----------------------------
    residuals = ds - (alpha + beta * s_t)

    # For CIR: Var ≈ sigma² * S * dt
    sigma = np.sqrt(np.mean((residuals**2) / (s_t * dt)))

    # -----------------------------
    # Jump parameters
    # -----------------------------
    jump_sizes = residuals[jump_mask]
    lam = np.sum(jump_mask) / (len(ds) * dt)

    return JumpCIRParams(kappa, theta, sigma, lam, jump_sizes, s.iloc[0])


# =========================================================
# 2. SPREAD SIMULATION
# =========================================================

def simulate_spread_paths(params, n_paths, T=10, dt=1/52, seed=42):
    """
    Simulate spread paths using Jump-CIR

    S_{t+1} = S_t + drift + diffusion + jump
    """

    n_steps = int(T / dt)
    paths = np.zeros((n_steps + 1, n_paths))
    paths[0] = params.s0

    rng = np.random.default_rng(seed)

    for t in range(n_steps):
        S = np.maximum(paths[t], 0)

        # -----------------------------
        # CIR diffusion
        # -----------------------------
        dW = rng.standard_normal(n_paths) * np.sqrt(dt)

        drift = params.kappa * (params.theta - S) * dt
        diffusion = params.sigma * np.sqrt(S) * dW

        # -----------------------------
        # Jump process
        # -----------------------------
        jump_counts = rng.poisson(params.lam * dt, size=n_paths)

        jumps = np.zeros(n_paths)

        # (loop unavoidable here for sampling multiple jumps)
        for i in range(n_paths):
            if jump_counts[i] > 0:
                jumps[i] = np.sum(
                    rng.choice(params.jump_sizes, jump_counts[i])
                )

        # Update spreads
        paths[t+1] = np.maximum(S + drift + diffusion + jumps, 0)

    return paths


# =========================================================
# 3. CPDO SIMULATION
# =========================================================

def run_cpdo_simulation(spread_paths, cpdo_params, dt=1/52):
    """
    Simulate NAV evolution

    Components:
    - Carry (spread income)
    - MTM loss/gain
    - Interest
    - Fees
    - Roll cost
    """

    n_steps, n_paths = spread_paths.shape[0] - 1, spread_paths.shape[1]

    nav = np.zeros((n_steps + 1, n_paths))
    nav[0] = 100

    default = np.zeros(n_paths, dtype=bool)

    for t in range(n_steps):
        S = spread_paths[t]
        dS = spread_paths[t+1] - S
        NAV = nav[t]

        # -----------------------------
        # Leverage rule
        # -----------------------------
        L = (cpdo_params.target_nav - NAV) / (S * cpdo_params.dv01 * NAV)
        L = np.clip(L, cpdo_params.min_leverage, cpdo_params.max_leverage)

        # -----------------------------
        # PnL components
        # -----------------------------
        carry = L * S * dt
        mtm = -L * cpdo_params.dv01 * dS
        interest = cpdo_params.risk_free_rate * NAV * dt
        fees = cpdo_params.management_fee * NAV * dt

        # -----------------------------
        # Roll cost (every 6 months)
        # -----------------------------
        roll_cost = 0
        if t % cpdo_params.roll_every_steps == 0 and t > 0:
            roll_cost = cpdo_params.roll_cost_bps * 1e-4 * NAV

        # NAV update
        nav[t+1] = NAV + carry + mtm + interest - fees - roll_cost

        # -----------------------------
        # Default condition
        # -----------------------------
        default |= nav[t+1] <= cpdo_params.floor_nav
        nav[t+1] = np.maximum(nav[t+1], cpdo_params.floor_nav)

    return nav, default


# =========================================================
# 4. PROBABILITY OF DEFAULT
# =========================================================

def compute_pd(default):
    return np.mean(default)


# =========================================================
# 5. VISUALIZATION
# =========================================================

def plot_results(spread_paths, nav_paths):

    plt.figure()
    plt.plot(spread_paths[:, :20])
    plt.title("Sample Spread Paths")
    plt.show()

    plt.figure()
    plt.plot(nav_paths[:, :20])
    plt.title("Sample NAV Paths")
    plt.show()

    plt.figure()
    plt.hist(nav_paths[-1], bins=50)
    plt.title("Terminal NAV Distribution")
    plt.show()


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    # Load CDX spread data
    data = pd.read_csv("cdx_spreads.csv")
    spreads = data["spread"]

    # 1. Calibrate model
    params = calibrate_jump_cir(spreads)

    # 2. Simulate spreads
    spread_paths = simulate_spread_paths(params, n_paths=50000)

    # 3. Run CPDO engine
    cpdo_params = CPDOParams()
    nav_paths, default = run_cpdo_simulation(spread_paths, cpdo_params)

    # 4. Compute PD
    pd_estimate = compute_pd(default)
    print("Probability of Default:", pd_estimate)

    # 5. Plot results
    plot_results(spread_paths, nav_paths)