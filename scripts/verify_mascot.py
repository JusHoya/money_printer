
import sys
import os
import time

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.visualization.mascot import Mascot
from src.visualization.sprite_data import SPRITE_FRAMES

def clear():
    os.system('cls' if os.name == 'nt' else 'clear')

if __name__ == "__main__":
    m = Mascot()

    print("=== MR KRABS SPRITE VERIFICATION ===")
    print("Rendering all frames...\n")

    states = ["IDLE", "MONEY_EYES", "TINY_VIOLIN", "PANIC"]

    for state in states:
        print(f"\n--- STATE: {state} ---")
        m.state = state
        # Print all frames for this state
        frames = SPRITE_FRAMES[state]
        for i in range(len(frames)):
            print(f"Frame {i+1}:")
            # We can use the internal _render_grid for direct access
            print(m._render_grid(frames[i])) 
            print("-" * 40)

    print("\nVerification Complete.")
