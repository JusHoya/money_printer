
import json

def inspect_series():
    try:
        with open("all_series_definitions.json", "r") as f:
            data = json.load(f)
            
        print(f"Loaded {len(data)} series definitions.")
        
        targets = ["KXBTC", "KXBTC15M", "KXBTCHOURLY"]
        print(f"Checking targets: {targets}")
        
        for t in targets:
            match = next((s for s in data if s.get('ticker') == t), None)
            if match:
                print(f"FOUND: {t}")
                print(f"  Title: {match.get('title')}")
                print(f"  Frequency: {match.get('frequency')}")
                print(f"  Contract Type: {match.get('contract_type')}")
            else:
                print(f"MISSING: {t}")
            print("-" * 20)
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    inspect_series()
