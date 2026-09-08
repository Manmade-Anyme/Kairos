# Kairos Discord Alerts: OI Flow Signal Reference

**Target Audience:** Traders, Analysts, and System Operators
**Purpose:** A comprehensive guide to decoding the exact meaning of every component within the "OI Flow" line of the Kairos Discord alerts.

---

## Data Update Frequency
Kairos runs its full evaluation cycle every **60 seconds** during active market sessions. 
- **OI Flow Calculation:** The "OI Change" you see is calculated over a **5-minute rolling window** (comparing current OI against the snapshot from 5 cycles ago). This ensures that the signal captures genuine institutional momentum rather than 1-minute noise.

---

## The Anatomy of an OI Flow Signal

When Kairos posts an alert to Discord, the **Condition 3: OI Flow** line condenses thousands of data points into a single string. It follows this strict format:

`[Score Emoji] OI Flow : [Trend Phase] [Phase Emoji] | [GEX State] | [NDE State][Optional: Vega Trap]`

**Example:**
`🔴 OI Flow  : Short Covering 🔵 | GEX neutral | NDE confirms ✓ | Vega trap 🔥`

Here is exactly what every single piece of that message means.

---

## 1. The Core Trend Phase

The base of the signal is derived by comparing **Price Action** against the **Total Open Interest (OI) Change**. This tells us if a move is fueled by "Conviction" (new money entering) or "Pullbacks" (old money exiting).

| Phase in Discord | Price | Total OI | Core Meaning | Conviction |
| :--- | :--- | :--- | :--- | :--- |
| `Long Buildup 🟢` | Up | Up | **Aggressive Buying:** New buyers are initiating long positions. High momentum. | **High** |
| `Short Buildup 🔴` | Down | Up | **Aggressive Selling:** New short sellers are entering to drive the price down. | **High** |
| `Short Covering 🔵` | Up | Down | **Bearish Pullback:** Price is rising *only* because bears are buying to close their shorts. No new buyers. | **Low** |
| `Long Unwinding 🟠` | Down | Down | **Bullish Pullback:** Price is falling *only* because bulls are selling to take profits. No new sellers. | **Low** |
| `Neutral 🟡` | Flat | Flat | **Rangebound:** Neither buyers nor sellers are demonstrating clear intent. | **None** |

> **Scoring Note:** Because `Short Covering` and `Long Unwinding` lack aggressive new money (they are just pullbacks), Kairos defaults to scoring them as **🔴 AVOID (0 Points)**. The engine only awards points for actual Buildups.

---

## 2. GEX State (Dealer Gamma Exposure)

The next block, **GEX**, tracks the positioning of Options Dealers (Market Makers). Dealers hedge their books dynamically, and their hedging can either *suppress* or *amplify* market moves.

*   `GEX trend ✓`
    *   **Meaning:** Dealers are heavily "Short Gamma".
    *   **Impact:** When price moves, dealers are forced to buy into rising prices and sell into falling prices to stay neutral. This acts as **rocket fuel**, amplifying the trend. This is exactly what we want to see for a breakout.
*   `GEX pin ⚠️`
    *   **Meaning:** Dealers are heavily "Long Gamma".
    *   **Impact:** When price drops, dealers buy. When price rises, dealers sell. This creates a "shock absorber" effect that **kills momentum**, keeping the price pinned in a tight, choppy range. Kairos will score this as **🔴 AVOID** to prevent trading in a choppy market.
*   `GEX neutral`
    *   **Meaning:** Dealer exposure is balanced. The market is free to move naturally without massive dealer interference.

---

## 3. NDE State (Smart Money Net Delta Exposure)

**NDE** measures the aggregate directional bias (Delta) across the options chain. It asks: *"Does the Smart Money's positioning agree with what the price chart is showing?"*

*   `NDE confirms ✓`
    *   **Meaning:** The options chain delta aligns with the price action. If the price is crashing (Short Buildup), the NDE is heavily net-short. **The move is genuine.**
*   `NDE contra ✗`
    *   **Meaning:** **Contradiction.** The price chart shows a breakout, but the options chain shows heavy positioning in the opposite direction.
    *   **Impact:** This usually indicates a "Trap" (retail buying a fake breakout) or heavy institutional hedging. Kairos immediately scores this as a **🔴 AVOID**.
*   `NDE ambiguous`
    *   **Meaning:** The delta positioning is too balanced or weak to offer a strong conviction signal either way.

---

## 4. The Vega Trap Alert 🔥

This is the ultimate danger signal. It only appears in the Discord message when triggered.

*   `| Vega trap 🔥`
    *   **Meaning:** The ATM options have high Vega exposure (sensitive to volatility), BUT Implied Volatility (IV) is rapidly crashing.
    *   **Impact:** **IV Crush.** Even if you guess the direction perfectly, the shrinking premium will eat your profits. A bought option will lose value while the underlying index moves in your favor.
    *   **Rule:** If a Vega Trap is detected, Kairos will **always** override the OI Flow score to **🔴 AVOID (0 points)**, regardless of how strong the trend looks.

---

## 5. Decoding Full Combinations

Here is how you can read the "matrix" of these indicators when they arrive in Discord:

### The "Perfect Storm" Setup (Bullish)
> `🟢 OI Flow  : Long Buildup 🟢 | GEX trend ✓ | NDE confirms ✓`
*   **Translation:** "Price is ripping higher on massive new buying. Dealers are short-gamma and forced to buy, amplifying the rip. Smart money delta is heavily net-long. Total alignment. GREEN."

### The "Perfect Storm" Setup (Bearish)
> `🟢 OI Flow  : Short Buildup 🔴 | GEX trend ✓ | NDE confirms ✓`
*   **Translation:** "Price is crashing on massive new short-selling. Dealers are short-gamma and forced to sell into the drop, accelerating the crash. Smart money delta is heavily net-short. Total alignment. GREEN."

### The "Fakeout / Bear Trap"
> `🔴 OI Flow  : Short Buildup 🔴 | GEX neutral | NDE contra ✗`
*   **Translation:** "Price is dropping, and retail is aggressively shorting. BUT, the options chain delta is overwhelmingly net-LONG. Smart money is buying the dip and waiting to squeeze the shorts. Do not enter. RED."

### A Confirmed Long Unwinding
> `🟢 OI Flow  : Long Unwinding 🟠 | GEX neutral | NDE confirms ✓`
*   **Translation:** "Price and OI are both falling, while options flow confirms the bearish direction and dealers are not pinning the move. This phase remains eligible when the 5-of-8 directional consensus is met."

### The "Premium Sinkhole"
> `🔴 OI Flow  : Long Buildup 🟢 | GEX neutral | NDE confirms ✓ | Vega trap 🔥`
*   **Translation:** "A beautiful, genuine bullish trend is happening. However, IV is crashing so fast that Call options are losing their extrinsic value. Entering here is a mathematical trap for option buyers. RED."

### The "Chop Zone"
> `🔴 OI Flow  : Neutral 🟡 | GEX pin ⚠️ | NDE ambiguous`
*   **Translation:** "No one knows where the market is going, and dealers are actively pinning the price to suppress any breakouts. A complete theta-burn environment. RED."
