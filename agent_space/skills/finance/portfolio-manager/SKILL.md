---
name: portfolio-manager
description: Manages asset allocation, budgeting, and risk assessment for family office portfolios.
version: 1.0.0
---

# Portfolio Manager Skill

<skill_definition>
The **Portfolio Manager** skill focuses on the mathematical optimization of a portfolio. It handles asset allocation (Stocks/Bonds/Cash/Crypto), risk management (calculating Beta, Sharpe Ratio), and personal finance budgeting (Income vs. Expenses).
</skill_definition>

<usage_instructions>
1.  **Asset Allocation**: Determine the target mix based on the user's risk tolerance and time horizon.
2.  **Rebalancing**: Identify when the actual allocation drifts too far from the target.
3.  **Risk Audit**: Calculate portfolio exposure to specific sectors or factors.
4.  **Budgeting**: Track inflows and outflows to ensure positive net worth growth.
</usage_instructions>

<output_template>
## Portfolio Health Check
*   **Current Allocation**: [Stocks X% | Bonds Y% | Cash Z%]
*   **Risk Metrics**:
    *   Beta: [Value]
    *   Concentration Risk: [High/Low]
*   **Action Required**: [Rebalance / Hold / Hedge]
</output_template>
