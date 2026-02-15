
from datetime import datetime
import random
from src.visualization.sprite_data import SPRITE_FRAMES

class Mascot:
    """
    Mr. Krabs High-Fidelity Terminal Sprite 🦀💰
    Uses ANSI TrueColor and block characters (▀) to render 2 vertical pixels per line.
    Imports PNG-extracted pixel data from sprite_data.py.
    
    States:
        IDLE        - Default bobbing animation (profitable or flat)
        MONEY_EYES  - Money grabbing! (trade just won)
        RUNNING     - Active trade execution in progress
        TINY_VIOLIN - Small loss / flat period
        PANIC       - Large drawdown
    """
    
    # Animation speed: seconds per frame
    FRAME_INTERVAL = 0.3
    
    # State cooldown: minimum seconds before state can change
    STATE_COOLDOWN = 2.0
    
    def __init__(self):
        self.state = "IDLE"
        self.last_state_change = datetime.now()
        self.frame_index = 0
        self.last_frame_time = datetime.now()
    
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

    def set_state(self, pnl_change: float, daily_pnl: float, has_open_trades: bool = False):
        """
        Update mascot state based on trading conditions.
        
        Args:
            pnl_change: PnL delta since last check
            daily_pnl: Total daily realized PnL
            has_open_trades: Whether there are currently open positions
        """
        now = datetime.now()
        elapsed = (now - self.last_state_change).total_seconds()
        
        # PANIC always overrides immediately
        if daily_pnl < -10.0:
            if self.state != "PANIC":
                self.state = "PANIC"
                self.frame_index = 0
                self.last_state_change = now
            return
        
        # Respect cooldown for non-panic transitions
        if elapsed < self.STATE_COOLDOWN and self.state != "IDLE":
            return
        
        # Priority-based state selection
        if pnl_change > 0.5:
            new_state = "MONEY_EYES"
        elif pnl_change < -0.5:
            new_state = "TINY_VIOLIN"
        elif has_open_trades:
            new_state = "RUNNING"
        else:
            new_state = "IDLE"
        
        if new_state != self.state:
            self.state = new_state
            self.frame_index = 0
            self.last_state_change = now
            
    def get_frame(self) -> str:
        """Get the current animation frame as ANSI art string."""
        frames = SPRITE_FRAMES.get(self.state, SPRITE_FRAMES["IDLE"])
        
        now = datetime.now()
        elapsed = (now - self.last_frame_time).total_seconds()
        
        # Advance frame on interval
        if elapsed >= self.FRAME_INTERVAL:
            self.frame_index = (self.frame_index + 1) % len(frames)
            self.last_frame_time = now
        
        # Clamp index
        idx = self.frame_index % len(frames)
        
        return self._render_grid(frames[idx])
