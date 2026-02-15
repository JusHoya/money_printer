
from datetime import datetime
import random
from src.visualization.sprite_data import SPRITE_FRAMES

class Mascot:
    """
    Mr. Krabs High-Fidelity Terminal Sprite 🦀💰
    Uses ANSI TrueColor and block characters (▀) to render 2 vertical pixels per line.
    Now imports high-fidelity pixel data from sprite_data.py.
    """
    
    def __init__(self):
        self.state = "IDLE"
        self.last_update = datetime.now()
        self.frame_index = 0
    
    def _rgb_to_ansi(self, r, g, b, bg=False):
        mode = 48 if bg else 38
        return f"\033[{mode};2;{r};{g};{b}m"
        
    def _render_grid(self, grid):
        """
        Converts a grid of RGB tuples into an ANSI block string.
        Takes 2 rows at a time to form one line of '▀' (upper block).
        Top char provides FG color, Bottom char provides BG color.
        grid: List[List[Tuple(r,g,b) | None]]
        """
        output = []
        rows = len(grid)
        if rows == 0: return ""
        width = len(grid[0])
        
        # Iterate in pairs of 2 rows
        for y in range(0, rows, 2):
            line = ""
            row_top = grid[y]
            row_bot = grid[y+1] if y+1 < rows else [None] * width
            
            last_fg = None
            last_bg = None
            
            for x in range(width):
                rgb_t = row_top[x] if x < len(row_top) else None
                rgb_b = row_bot[x] if x < len(row_bot) else None
                
                # FG Optimization
                if rgb_t != last_fg:
                    if rgb_t:
                        line += self._rgb_to_ansi(*rgb_t, bg=False)
                    else:
                        line += "\033[39m" # Reset FG
                    last_fg = rgb_t
                    
                # BG Optimization
                if rgb_b != last_bg:
                    if rgb_b:
                        line += self._rgb_to_ansi(*rgb_b, bg=True)
                    else:
                        line += "\033[49m" # Reset BG
                    last_bg = rgb_b
                
                # The Character: ▀
                # Top half gets FG color, Bottom half gets BG color.
                if rgb_t is None and rgb_b is None:
                    line += " " # Pure transparent
                else:
                    line += "▀"
                    
            line += "\033[0m" # Reset all at end of line
            output.append(line)
            
        return "\n".join(output)

    def set_state(self, pnl_change: float, daily_pnl: float):
        now = datetime.now()
        # "Bounce" logic - don't flicker states too fast unless urgency changes
        if (now - self.last_update).total_seconds() < 2.0 and self.state != "IDLE":
             # Allow PANIC to override anything
            if self.state != "PANIC" and daily_pnl < -10.0:
                 pass # Fall through to set PANIC
            else:
                 return

        if daily_pnl < -10.0:
            self.state = "PANIC"
            self.last_update = now
        elif pnl_change > 0:
            self.state = "MONEY_EYES"
            self.last_update = now
        elif pnl_change < 0:
            self.state = "TINY_VIOLIN"
            self.last_update = now
        else:
            self.state = "IDLE"
            
    def get_frame(self) -> str:
        frames = SPRITE_FRAMES.get(self.state, SPRITE_FRAMES["IDLE"])
        
        # Cycle frames roughly every 0.25s
        idx = int(datetime.now().microsecond / 250000) % len(frames)
        
        return self._render_grid(frames[idx])
