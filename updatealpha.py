# =========================================================
# CPDO MONTE CARLO - FINAL WORKING VERSION
# =========================================================

import numpy as np
import pandas as pd
from dataclasses import dataclass


@dataclass
class CIRParams:
    kappa: float
    theta: float
    sigma: float
    s0: float


@dataclass
class CPDOParams:
    target_nav: float = 100.0
    floor_nav: float = 10.0
    max_leverage: float = 6.0
    dv01: float = 0.0005
    management_fee: float = 0.0025
    roll_cost_bps: float = 1.5
    roll_every_steps: int = 26
    recovery_rate: float = 0.4


def calibrate_cir(spread_series, dt=1/52):

    s = pd.Series(spread_series).dropna().reset_index(drop=True)

    s_t = s[:-1].values
    ds = s.diff().dropna().values

    X = np.vstack([np.ones_like(s_t), s_t]).T
    alpha, beta = np.linalg.lstsq(X, ds, rcond=None)[0]

    kappa = max(0.1, -beta / dt)
    theta = max(1, alpha / (kappa * dt))
    sigma = np.std(ds) / np.sqrt(dt)

    return CIRParams(kappa, theta, sigma, s.iloc[0])


def simulate_spread_paths(params, n_paths, T=10, dt=1/52, seed=42):

    n_steps = int(T / dt)
    paths = np.zeros((n_steps + 1, n_paths))
    paths[0] = params.s0

    rng = np.random.default_rng(seed)

    sigma = params.sigma * 0.8  # increased vol

    for t in range(n_steps):

        S = np.maximum(paths[t], 1e-6)

        dW = rng.standard_normal(n_paths) * np.sqrt(dt)

        drift = params.kappa * (params.theta - S) * dt
        diffusion = sigma * np.sqrt(S) * dW

        dS = drift + diffusion
        dS = np.clip(dS, -0.25 * S, 0.25 * S)

        paths[t+1] = np.clip(S + dS, 1e-6, 1000)

    return paths


def run_cpdo_simulation(spread_paths, cpdo_params, sofr_series, dt=1/52):

    n_steps, n_paths = spread_paths.shape[0] - 1, spread_paths.shape[1]

    nav = np.zeros((n_steps + 1, n_paths))
    nav[0] = 100

    default = np.zeros(n_paths, dtype=bool)

    sofr_interp = np.interp(
        np.arange(n_steps),
        np.linspace(0, n_steps, len(sofr_series)),
        sofr_series
    )

    for t in range(n_steps):

        S = spread_paths[t]
        dS = spread_paths[t+1] - S
        NAV = nav[t]

        alive = NAV > cpdo_params.floor_nav
        NAV_safe = np.maximum(NAV, 1e-6)

        # 🔥 stronger leverage (but controlled)
        L = 1 + 0.4 * (cpdo_params.target_nav - NAV_safe) / cpdo_params.target_nav
        L = np.clip(L, 0, cpdo_params.max_leverage)

        L = np.where(alive, L, 0)
        exposure = L * NAV

        # PnL
        carry = exposure * (S / 10000) * dt
        mtm = -exposure * cpdo_params.dv01 * dS
        interest = sofr_interp[t] * NAV * dt
        fees = cpdo_params.management_fee * NAV * dt

        # 🔥 stronger default mechanism
        hazard = 0.03 * S / (1 - cpdo_params.recovery_rate)
        hazard = np.clip(hazard, 0, 1.5)

        default_prob = hazard * dt

        effective_LGD = 0.2
        default_loss = exposure * effective_LGD * default_prob

        # roll
        roll_cost = 0
        if t % cpdo_params.roll_every_steps == 0 and t > 0:
            roll_cost = cpdo_params.roll_cost_bps * 1e-4 * NAV

        nav_next = NAV + carry + mtm + interest - fees - roll_cost - default_loss
        nav_next = np.nan_to_num(nav_next, nan=cpdo_params.floor_nav)

        nav[t+1] = np.where(alive, nav_next, cpdo_params.floor_nav)
        nav[t+1] = np.maximum(nav[t+1], cpdo_params.floor_nav)

        default |= nav[t+1] <= cpdo_params.floor_nav

    return nav, default


if __name__ == "__main__":

    print("Loading data...")

    cdx = pd.read_csv("CDX IG CDSI GEN 5Y Corp(CDX IG CDSI GEN 5Y Corp).csv", sep=";")
    cdx.columns = ["Date", "Spread"]
    cdx["Spread"] = pd.to_numeric(cdx["Spread"], errors="coerce")
    spreads = cdx["Spread"].dropna()

    sofr = pd.read_csv("SOFR rates(Daily).csv", sep=";")
    sofr["SOFR"] = (
        sofr.iloc[:, 1].astype(str).str.replace(",", ".", regex=False).astype(float)
    )
    sofr_series = (sofr["SOFR"] / 100).dropna().values

    cpdo_params = CPDOParams()

    print("\nRunning simulation...\n")

    params = calibrate_cir(spreads)

    spread_paths = simulate_spread_paths(params, n_paths=10000)

    nav_paths, default = run_cpdo_simulation(
        spread_paths, cpdo_params, sofr_series
    )

    pd_est = np.mean(default)
    mean_nav = np.mean(nav_paths[-1])

    print(f"PD: {pd_est:.4f}")
    print(f"Mean NAV: {mean_nav:.2f}")