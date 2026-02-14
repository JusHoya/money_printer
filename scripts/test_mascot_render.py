
import sys
import os
import time

# Add project root to path
sys.path.append(os.getcwd())

from src.visualization.mascot import Mascot

def test_render():
    print("Initializing Mascot with High-Fidelity Graphics...")
    m = Mascot()
    
    print("\n[IDLE FRAME TEST]")
    frame = m.get_frame()
    if not frame:
        raise ValueError("Frame is empty!")
    print(frame)
    if "\033[" not in frame:
        raise ValueError("Frame missing ANSI codes!")
        
    print("\n[MONEY_EYES TEST]")
    m.state = "MONEY_EYES"
    print(m.get_frame())
    
    print("\n[Verficiation Complete] Renderer is functional.")

if __name__ == "__main__":
    test_render()
