---
name: algo-trading-architect
description: Designs, scaffolds, and reviews algorithmic trading strategies and bot infrastructure.
version: 1.0.0
---

# Algo Trading Architect Skill

<skill_definition>
The **Algo Trading Architect** skill is the engineering backbone of the Financial Strategist. It translates high-level investment theses (from Market Analysis) into concrete, executable code structures. It understands the lifecycle of a bot: Strategy -> Backtest -> Paper Trade -> Live Trade.
</skill_definition>

<usage_instructions>
1.  **Design**: Define the class structure, event loops, and data ingestion pipelines for a bot.
2.  **Scaffold**: Generate boilerplate code (Python/C++) for standard frameworks (e.g., `backtrader`, `lean`, `ccxt`).
3.  **Backtest Config**: Define the parameters for testing (timeframe, slippage, commission models).
4.  **Review**: Analyze existing bot code for logic errors, look-ahead bias, or overfitting.
</usage_instructions>

<output_template>
## Algo Strategy Blueprint
*   **Strategy Name**: [Name]
*   **Target Asset**: [Asset Class]
*   **Logic Pseudocode**:
    ```python
    def on_market_data(self, data):
        if indicator > threshold:
            buy()
    ```
*   **Risk Constraints**: [Max Position Size, Stop Loss]
*   **Required Libraries**: [pandas, numpy, ccxt, etc.]
</output_template>
