import os
import sys
import requests
import time
import base64
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

def test_prod_auth():
    load_dotenv(override=True)
    key_id = os.getenv('KALSHI_KEY_ID')
    key_path = os.getenv('KALSHI_PRIVATE_KEY_PATH')
    
    print(f"Testing Production API with Key: {key_id}")
    
    with open(key_path, 'rb') as f:
        key_data = f.read()
        
    priv_key = serialization.load_pem_private_key(key_data, password=None)
    
    timestamp = str(int(time.time() * 1000))
    method = 'GET'
    path = '/trade-api/v2/portfolio/balance'
    payload = f"{timestamp}{method}{path}"
    
    signature = base64.b64encode(priv_key.sign(
        payload.encode(),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH
        ),
        hashes.SHA256()
    )).decode()
    
    headers = {
        'KALSHI-ACCESS-KEY': key_id,
        'KALSHI-ACCESS-SIGNATURE': signature,
        'KALSHI-ACCESS-TIMESTAMP': timestamp
    }
    
    url = 'https://trading-api.kalshi.com/trade-api/v2/portfolio/balance'
    print(f"Requesting: {url}")
    resp = requests.get(url, headers=headers)
    print(f"Status: {resp.status_code}")
    print(f"Body: {resp.text}")

if __name__ == "__main__":
    test_prod_auth()
