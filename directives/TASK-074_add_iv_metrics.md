# TASK-074: Add ATM IV and IV Change % to Environment Alerts

## 1. Context and Objective
The trader has requested to see the absolute **ATM IV** value and the **IV Change %** alongside the current IV Trend condition in the Discord environment alerts.

Currently, the system outputs:
`IV Trend : +0.48 — IV expanding (DTE≤1) [2/2]`

The trader clarified that the new required metrics are:
- **ATM IV**: The current absolute value of ATM IV (`iv_now`).
- **IV Change %**: The percentage change of ATM IV relative to the lookback reference period (`iv_then`). 
  *Formula:* `((iv_now - iv_then) / iv_then) * 100`

## 2. Requirements
1. **Calculate IV Change %**:
   - In `src/kairos/processor.py` (inside `score_iv_change`), calculate the percentage change between `iv_now` and `iv_then`.
   - Ensure you handle any potential division-by-zero if `iv_then` is 0.

2. **Update the Output String**:
   - Modify the string returned in `score_iv_change` (or modify `src/kairos/notifier.py` formatting) to include these two new data points.
   - Example desired output format (you may adapt this to fit cleanly in a single line or as additional lines in the Discord message):
     `IV Trend : +0.48 — IV expanding (DTE≤1) | ATM IV: 10.82 | IV%: +42.96%`

## 3. Verification & TDD Requirement
- **CRITICAL**: You MUST follow Test-Driven Development (TDD) as per the Global Development Pipeline.
- **Before** updating the implementation in `src/kairos/processor.py`, write a failing test for `score_iv_change` that asserts the new format of the returned `detail` string containing `ATM IV` and `IV%`.
- The condition's points and core logic (the thresholds for GREEN/YELLOW/RED based on absolute change) remain **unchanged**. We are only adding display metrics.
- Ensure the changes pass all existing and new unit tests and that formatting is clean in Discord.
