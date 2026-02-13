import requests
import json
import base64
import time
import os
from datetime import datetime
from typing import Dict, Any, Optional
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from src.core.interfaces import DataProvider, MarketData
from src.utils.logger import logger

class KalshiProvider(DataProvider):
    """
    Data Provider for Kalshi. 
    Supports Authenticated (Private) and Anonymous (Public Read-Only) modes.
    """
    
    PUBLIC_API_URL = "https://api.elections.kalshi.com/trade-api/v2"
    
    def __init__(self, key_id: str = None, private_key_path: str = None, api_url: str = None, read_only: bool = False):
        self.key_id = key_id
        self.api_url = (api_url or self.PUBLIC_API_URL).rstrip('/')
        self.anonymous = not (key_id and private_key_path)
        self.read_only = read_only
        
        if not self.anonymous:
            self.private_key = self._load_private_key(private_key_path)
        else:
            self.private_key = None
            
        self.session = requests.Session()
        
    def _load_private_key(self, path_or_content: str):
        try:
            # Check if it's a file path
            if os.path.exists(path_or_content):
                with open(path_or_content, "rb") as key_file:
                    key_data = key_file.read()
            else:
                # Treat as raw key content (PEM)
                key_data = path_or_content.encode('utf-8')
                # Add PEM headers if missing
                if b"BEGIN PRIVATE KEY" not in key_data:
                    key_data = b"-----BEGIN RSA PRIVATE KEY-----\n" + key_data + b"\n-----END RSA PRIVATE KEY-----"
            
            return serialization.load_pem_private_key(
                key_data,
                password=None
            )
        except Exception as e:
            logger.error(f"[KalshiProvider] Error loading private key: {e}")
            raise e

    def _sign_request(self, method: str, path: str, timestamp: str) -> str:
        """Generates the RSA signature for the request."""
        if self.anonymous:
            return ""
            
        full_path = path
        if not path.startswith("/trade-api/v2"):
             full_path = "/trade-api/v2" + path
             
        payload = f"{timestamp}{method}{full_path}"
        
        signature = self.private_key.sign(
            payload.encode('utf-8'),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )
        return base64.b64encode(signature).decode('utf-8')

    def place_order(self, symbol: str, side: str, quantity: int, price: float):
        """
        Places an order.
        SAFETY: If read_only is True, this explicitly BLOCKS the request.
        """
        if self.read_only:
            raise RuntimeError(f"Trade blocked: Provider is in READ-ONLY mode. (Attempted: {side} {quantity} {symbol})")
            
        logger.warning("Order placement not implemented for live trading yet.")
        return False

    def connect(self) -> bool:
        """
        Verifies connection by hitting a public endpoint.
        """
        mode = "GHOST (Read-Only)" if self.anonymous else "AUTHENTICATED"
        logger.info(f"[KalshiProvider] Connecting to {self.api_url} in {mode} mode...")
        try:
            url = f"{self.api_url}/exchange/status"
            headers = {"Content-Type": "application/json"}
            
            if not self.anonymous:
                path = "/exchange/status"
                timestamp = str(int(time.time() * 1000))
                signature = self._sign_request("GET", path, timestamp)
                headers.update({
                    "KALSHI-ACCESS-KEY": self.key_id,
                    "KALSHI-ACCESS-SIGNATURE": signature,
                    "KALSHI-ACCESS-TIMESTAMP": timestamp
                })
                
            resp = self.session.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                 logger.info(f"[KalshiProvider] Exchange is Online. (Live Prod/Public)")
                 return True
            else:
                logger.error(f"[KalshiProvider] Exchange Status Error: {resp.status_code}")
                return False
        except Exception as e:
            logger.error(f"[KalshiProvider] Connection Error: {e}")
            return False

    def get_balance(self) -> float:
        """
        Fetches the user's available balance in cents and returns it in dollars.
        """
        if self.anonymous:
            return 0.0
            
        path = "/portfolio/balance"
        url = f"{self.api_url}{path}"
        
        timestamp = str(int(time.time() * 1000))
        signature = self._sign_request("GET", path, timestamp)
        
        headers = {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json"
        }
        
        try:
            resp = self.session.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            balance_cents = data.get('balance', 0)
            return balance_cents / 100.0
        except Exception as e:
            logger.error(f"[KalshiProvider] Failed to fetch balance: {e}")
            return 0.0

    def fetch_latest(self, symbol: str) -> MarketData:
        """
        Fetches market data for a specific ticker. 
        Works without auth on the public elections endpoint.
        """
        path = f"/markets/{symbol}"
        url = f"{self.api_url}{path}"
        
        headers = {"Content-Type": "application/json"}
        
        if not self.anonymous:
            timestamp = str(int(time.time() * 1000))
            signature = self._sign_request("GET", path, timestamp)
            headers.update({
                "KALSHI-ACCESS-KEY": self.key_id,
                "KALSHI-ACCESS-SIGNATURE": signature,
                "KALSHI-ACCESS-TIMESTAMP": timestamp
            })
        
        try:
            resp = self.session.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json().get('market', {})
            
            yes_bid = data.get('yes_bid', 0) / 100.0
            yes_ask = data.get('yes_ask', 0) / 100.0
            last_price = data.get('last_price', 0) / 100.0
            
            return MarketData(
                symbol=symbol,
                timestamp=datetime.now(),
                price=last_price,
                volume=data.get('volume', 0),
                bid=yes_bid,
                ask=yes_ask,
                extra={
                    "status": data.get('status'),
                    "close_time": data.get('close_time'),
                    "source": "live_kalshi_ghost" if self.anonymous else "live_kalshi"
                }
            )
            
        except Exception as e:
            logger.error(f"[KalshiProvider] Fetch Error for {symbol}: {e}")
            return None