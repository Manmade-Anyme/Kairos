# Kairos Discord Alert — Trading Decision Cheat Sheet

Use this strict, top-to-bottom checklist when a new Kairos alert arrives in Discord. **If any step fails, DO NOT take the trade.**

---

## Step 1: The Top-Line Status
Look at the very first line of the alert. This is the master gatekeeper.

| Status | Action |
| :--- | :--- |
| 🟢 **ENVIRONMENT: GO** | **Proceed to Step 2.** Conditions are favorable. |
| 🟡 **ENVIRONMENT: CAUTION**| **NO TRADE.** Conditions are mixed. |
| 🔴 **ENVIRONMENT: AVOID** | **NO TRADE.** Environment is hostile to option buyers. |

---

## Step 2: The Volatility/Premium Check
Look just below the top line and at the end of the `OI Flow` line for trap warnings.

| Warning Tag | Meaning | Action |
| :--- | :--- | :--- |
| `⚠️ IV contracting` | Premium is decaying rapidly. | **NO TRADE.** (Even if direction is right, you will lose money to IV crush). |
| `\| Vega trap 🔥` | Mathematical trap for buyers. | **NO TRADE.** |
| *(No warnings present)* | Volatility is safe. | **Proceed to Step 3.** |

---

## Step 3: The OI Flow Alignment
If the environment is **GO** and there are no warnings, analyze the **Condition 3: OI Flow** line. You need specific alignment to confirm a genuine breakout.

### A. The Trend Phase (Direction)
Must show an eligible direction with matching consensus.
*   ✅ `Long Buildup 🟢` → Prepare to **Buy Calls**.
*   ✅ `Short Buildup 🔴` → Prepare to **Buy Puts**.
*   ✅ `Short Covering 🔵`, `Long Unwinding 🟠` → Can qualify only when the displayed direction has valid GEX and confirming NDE.
*   ❌ `Neutral 🟡` → **NO TRADE**.

### B. The GEX State (Dealer Positioning)
Market makers should not be fighting the trend.
*   ✅ `GEX trend 🟢` → **Excellent.** Dealers are fueling the move.
*   ✅ `GEX neutral 🟡` → **Acceptable.** Dealers are out of the way.
*   ❌ `GEX pin 🔴` → **NO TRADE.** Dealers will suppress the breakout.

### C. The NDE State (Smart Money Delta)
Smart money must agree with the chart.
*   ✅ `NDE confirms 🟢` → **VALIDATED.** Options flow matches price action.
*   ❌ `NDE contra 🔴` → **NO TRADE.** It's a trap; smart money is fading the move.

---

## 🚦 Final Execution Summary

Take the trade **ONLY IF** the alert looks like one of these "Perfect Storms":

### 📈 Perfect Setup for CALLS
> 🟢 **ENVIRONMENT: GO**
> *(No IV warnings)*
> 🟢 **OI Flow:** Long Buildup 🟢 | GEX trend 🟢 | NDE confirms 🟢

### 📉 Perfect Setup for PUTS
> 🟢 **ENVIRONMENT: GO**
> *(No IV warnings)*
> 🟢 **OI Flow:** Short Buildup 🔴 | GEX trend 🟢 | NDE confirms 🟢

*If required valid alignment is missing, wait for the next alert.*

PCR is passive telemetry only; it is not a trade requirement. GEX neutral is acceptable when data is valid and NDE confirms the displayed direction. The prior buildup-only checklist policy is intentionally not enforced by the current engine.
