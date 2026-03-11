"""
REDIRECT: analyze_harvest.py -> lab.py --audit
This file is kept for backward compatibility.
"""
import os
import sys

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.lab import Lab

if __name__ == "__main__":
    print("Redirecting to lab.py --audit...")
    lab = Lab()
    lab.run_audit()
