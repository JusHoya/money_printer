import requests
import json

def probe_coinbase():
    """
    Fetches all products from Coinbase Exchange API and filters for potential
    prediction markets or futures contracts.
    """
    url = "https://api.exchange.coinbase.com/products"
    print(f"Probing Coinbase API: {url}...")
    
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        products = resp.json()
        
        print(f"Total Products Found: {len(products)}")
        
        # Filter for interesting keywords
        keywords = ["PRED", "FUT", "BINARY", "EVENT", "AUGUR"]
        interesting = []
        
        for p in products:
            pid = p.get('id', '')
            base = p.get('base_currency', '')
            type_ = p.get('product_type', 'unknown') # Some APIs expose this
            
            # Check ID for keywords
            if any(k in pid.upper() for k in keywords):
                interesting.append(p)
                continue
                
            # Check for non-standard quote currencies (e.g. not USD, USDC, BTC)
            # Prediction markets might use strange bases
            if type_ not in ['unknown', 'Spot']: # If type is available
                 interesting.append(p)

        if interesting:
            print(f"\n--- POTENTIAL PREDICTION MARKETS ({len(interesting)}) ---")
            for p in interesting:
                print(f"ID: {p['id']} | Base: {p['base_currency']} | Quote: {p['quote_currency']}")
        else:
            print("\n--- NO OBVIOUS PREDICTION MARKETS FOUND ---")
            print("Sampling first 5 products for context:")
            for p in products[:5]:
                print(f"ID: {p['id']}")
                
    except Exception as e:
        print(f"Probe Failed: {e}")

if __name__ == "__main__":
    probe_coinbase()
