#!/usr/bin/env python3
"""
Mr. Krabs Sprite Verification Script 🦀
Renders all animation states and frames in the terminal.
"""

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

    print("=" * 60)
    print("  🦀 MR KRABS SPRITE VERIFICATION 🦀")
    print("=" * 60)
    print(f"  Available States: {list(SPRITE_FRAMES.keys())}")
    print(f"  Total Frames: {sum(len(f) for f in SPRITE_FRAMES.values())}")
    print()

    states = ["IDLE", "MONEY_EYES", "TINY_VIOLIN", "PANIC", "RUNNING"]

    for state in states:
        if state not in SPRITE_FRAMES:
            print(f"\n  ⚠️  State '{state}' not found in sprite data!")
            continue
            
        frames = SPRITE_FRAMES[state]
        print(f"\n{'='*60}")
        print(f"  STATE: {state}  ({len(frames)} frames)")
        print(f"  Grid: {len(frames[0])} rows × {len(frames[0][0])} cols")
        print(f"  Terminal Lines: {len(frames[0]) // 2}")
        print(f"{'='*60}")
        
        for i in range(len(frames)):
            print(f"\n  --- Frame {i+1}/{len(frames)} ---")
            m.state = state
            m.frame_index = i
            print(m._render_grid(frames[i]))
            print()

    # Animation demo
    print(f"\n{'='*60}")
    print(f"  ANIMATION DEMO (IDLE - 8 frames at 0.3s)")
    print(f"{'='*60}")
    
    m.state = "IDLE"
    for cycle in range(8):
        clear()
        print(f"  🦀 ANIMATION DEMO — Frame {cycle+1}/8")
        print(m.get_frame())
        time.sleep(0.3)
    
    clear()
    print(f"\n{'='*60}")
    print(f"  ✅ Verification Complete!")
    print(f"{'='*60}")
