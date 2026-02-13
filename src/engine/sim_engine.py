import time
from typing import List
from src.core.interfaces import Strategy, DataProvider, ExecutionEngine, TradeSignal

class SimulationEngine(ExecutionEngine):
    """
    The Gym 🏋️
    Runs the loop: Fetch Data -> Strategy Analyze -> Execute Mock Order -> Track PnL
    """
    
    def __init__(self, strategy: Strategy, data_providers: List[DataProvider]):
        self.strategy = strategy
        self.providers = data_providers
        self.pnl = 0.0
        self.trades = 0
        
    def execute(self, signal: TradeSignal) -> bool:
        print(f"[{self.strategy.name()}] EXECUTE: {signal.side.upper()} {signal.quantity} {signal.symbol} @ {signal.limit_price}")
        self.trades += 1
        # Mock PnL impact (random win/loss for now)
        import random
        impact = random.choice([-5.0, 10.0]) 
        self.pnl += impact
        return True

    def run(self, steps: int = 5, symbol: str = None):
        print(f"--- Starting Simulation for {self.strategy.name()} ---")
        
        # Connect providers
        for p in self.providers:
            p.connect()
            
        for i in range(steps):
            print(f"\nStep {i+1}/{steps}")
            
            # Fetch Data
            data = self.providers[0].fetch_latest(symbol)
            
            if not data:
                print(f"[Engine] Warning: No data returned for symbol '{symbol}'")
                continue

            print(f"Data: {data.extra.get('source', 'unknown')} @ {data.price if data.price else 'N/A'}")
            
            # Strategy Analysis
            signals = self.strategy.analyze(data)
            
            # Normalize to list if strategy returns single object
            if signals and not isinstance(signals, list):
                signals = [signals]
            
            if signals:
                print(f"Signals Generated: {len(signals)}")
                for signal in signals:
                    self.execute(signal)
            else:
                print("No Signal.")
                
            time.sleep(0.5)
            
        print("\n--- Simulation Complete ---")
        print(f"Total Trades: {self.trades}")
        print(f"Hypothetical PnL: ${self.pnl:.2f}")

if __name__ == "__main__":
    # Test Bench
    from src.data.mock_providers import MockNWSProvider
    from src.strategies.weather_strategy import WeatherArbitrageStrategy
    
    sim = SimulationEngine(
        strategy=WeatherArbitrageStrategy(),
        data_providers=[MockNWSProvider()]
    )
    sim.run()
