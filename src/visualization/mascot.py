from datetime import datetime
import random

class Mascot:
    """
    Mr. Krabs High-Fidelity Terminal Sprite 🦀💰
    Uses ANSI TrueColor and block characters (▀) to render 2 vertical pixels per line.
    """
    
    def __init__(self):
        self.state = "IDLE"
        self.last_update = datetime.now()
        self.frame_index = 0
        
        # --- COLOR PALETTE (RGB) ---
        self.PALETTE = {
            ' ': None,                  # Transparent
            'R': (220, 20, 60),         # Skin Red (Crimson)
            'r': (178, 34, 34),         # Dark Red (Shadow)
            'L': (144, 238, 144),       # Eye Green (Light)
            'E': (255, 255, 255),       # Eye White/Shine
            'K': (0, 0, 0),             # Black (Pupils/Belt/Outlines)
            'B': (135, 206, 235),       # Shirt Blue (Sky)
            'P': (106, 90, 205),        # Pants Purple (SlateBlue)
            'G': (255, 215, 0),         # Gold (Buckle)
            'S': (34, 139, 34),         # Money Green
            '$': (255, 255, 255),       # Money White
        }

        # --- SPRITE MAPS ---
        # 2 Characters vertically = 1 Terminal Line
        # We process these in pairs of rows.
        
        self.SPRITE_DATA = {
            "IDLE": [
                # Frame 1: Normal
                [
                    "        LL  LL        ",
                    "        LL  LL        ",
                    "        LL  LL        ",
                    "        KK  KK        ",
                    "        KK  KK        ",
                    "        LL  LL        ",
                    "        LL  LL        ",
                    "       RRRRRRRR       ",
                    "      R K RR K R      ",
                    "     RRRRRKRRRRRR     ",
                    "    RRRRRRRRRRRRRR    ",
                    "   RRBBBBBBBBBBBBRR   ",
                    "  K RRBBBBWBBBBRR K   ",
                    "  K RRBBBBBBBBBBRR K  ",
                    " DK RKKKKKKKKKKKKR KD ",
                    " RR PPPPPTGTPPPPP RR  ",
                    " RR PPPPPPPPPPPPP RR  ",
                    "    PP PPP PPP PP     ",
                    "    KK     KK         "
                ],
                # Frame 2: Blink/Bob
                [
                    "                      ",
                    "        LL  LL        ",
                    "        LL  LL        ",
                    "        KK  KK        ",
                    "        KK  KK        ",
                    "        LL  LL        ",
                    "       RRRRRRRR       ",
                    "      R K RR K R      ",
                    "     RRRRRRRRRRRR     ",
                    "    RRRRRKRRKRRRRR    ",
                    "   RRBBBBBBBBBBBBRR   ",
                    "  K RRBBBBWBBBBRR K   ",
                    "  K RRBBBBBBBBBBRR K  ",
                    " DK RKKKKKKKKKKKKR KD ",
                    " RR PPPPPTGTPPPPP RR  ",
                    " RR PPPPPPPPPPPPP RR  ",
                    "    PP PPP PPP PP     ",
                    "    KK     KK         ",
                    "                      "
                ]
            ],
            "MONEY_EYES": [
                [
                    "        SS  SS        ",
                    "        SS  SS        ",
                    "        $$  $$        ",
                    "        $$  $$        ",
                    "        SS  SS        ",
                    "       RRRRRRRR       ",
                    "      R K RR K R      ",
                    "     RRRRRKRRRRRR     ",
                    "    RRRRRRRRRRRRRR    ",
                    "   RRBBBBBBBBBBBBRR   ",
                    "  S RRBBBBWBBBBRR S   ",
                    "  S RRBBBBBBBBBBRR S  ",
                    " DS RKKKKKKKKKKKKR SD ",
                    " RR PPPPPTGTPPPPP RR  ",
                    " RR PPPPPPPPPPPPP RR  ",
                    "    PP PPP PPP PP     ",
                    "    KK     KK         "
                ],
                [
                    "       $SS  SS$       ",
                    "        $$  $$        ",
                    "        $$  $$        ",
                    "        SS  SS        ",
                    "        SS  SS        ",
                    "       RRRRRRRR       ",
                    "      R K RR K R      ",
                    "     RRRRRKRRRRRR     ",
                    "    RRRRRRRRRRRRRR    ",
                    "   RRBBBBBBBBBBBBRR   ",
                    "  S RRBBBBWBBBBRR S   ",
                    "  S RRBBBBBBBBBBRR S  ",
                    " DS RKKKKKKKKKKKKR SD ",
                    " RR PPPPPTGTPPPPP RR  ",
                    " RR PPPPPPPPPPPPP RR  ",
                    "    PP PPP PPP PP     ",
                    "    KK     KK         "
                ]
            ],
            "TINY_VIOLIN": [
                [
                    "        LL  LL        ",
                    "        KK  KK        ",
                    "        KK  KK        ",
                    "        LL  LL        ",
                    "        LL  LL        ",
                    "       RRRRRRRR       ",
                    "      R - RR - R      ",
                    "     RRRRRRRRRRRR     ",
                    "    RRRRRKRRKRRRRR    ",
                    "   RRBBBBBBBBBBBBRR   ",
                    "    RRBBBBWBBBBRR     ",
                    "    RRBBBBBBBBBBRR    ",
                    "  ~ RKKKKKKKKKKKKR    ",
                    " -|-PPPPPTGTPPPPP     ",
                    "  | PPPPPPPPPPPPP     ",
                    "    PP PPP PPP PP     ",
                    "    KK     KK         "
                ]
            ],
            "PANIC": [
                [
                    "        !!  !!        ",
                    "        !!  !!        ",
                    "        !!  !!        ",
                    "        !!  !!        ",
                    "       RRRRRRRR       ",
                    "      R O RR O R      ",
                    "     RRRRRWRRRRRR     ",
                    "    RRRRRRRRRRRRRR    ",
                    "   RRBBBBBBBBBBBBRR   ",
                    "  ! RRBBBBWBBBBRR !   ",
                    "  ! RRBBBBBBBBBBRR !  ",
                    " D! RKKKKKKKKKKKKR !D ",
                    " RR PPPPPTGTPPPPP RR  ",
                    " RR PPPPPPPPPPPPP RR  ",
                    "    PP PPP PPP PP     ",
                    "    KK     KK         "
                ]
            ]
        }
    
    def _rgb_to_ansi(self, r, g, b, bg=False):
        mode = 48 if bg else 38
        return f"\033[{mode};2;{r};{g};{b}m"
        
    def _render_grid(self, grid):
        """
        Converts a grid of char codes into an ANSI block string.
        Takes 2 rows at a time to form one line of '▀' (upper block).
        Top char provides FG color, Bottom char provides BG color.
        """
        output = []
        rows = len(grid)
        width = len(grid[0])
        
        # Iterate in pairs of 2 rows
        for y in range(0, rows, 2):
            line = ""
            row_top = grid[y]
            row_bot = grid[y+1] if y+1 < rows else " " * width
            
            last_fg = None
            last_bg = None
            
            for x in range(width):
                char_t = row_top[x] if x < len(row_top) else ' '
                char_b = row_bot[x] if x < len(row_bot) else ' '
                
                rgb_t = self.PALETTE.get(char_t, None)
                rgb_b = self.PALETTE.get(char_b, None)
                
                # ANSI Optimization: Only print codes if changed
                if rgb_t != last_fg:
                    if rgb_t:
                        line += self._rgb_to_ansi(*rgb_t, bg=False)
                    else:
                        line += "\033[39m" # Reset FG
                    last_fg = rgb_t
                    
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
        if (now - self.last_update).total_seconds() < 5 and self.state != "IDLE":
            return

        if pnl_change > 0:
            self.state = "MONEY_EYES"
            self.last_update = now
        elif pnl_change < 0:
            self.state = "TINY_VIOLIN"
            self.last_update = now
        elif daily_pnl < -10.0:
            self.state = "PANIC"
        else:
            self.state = "IDLE"
            
    def get_frame(self) -> str:
        grids = self.SPRITE_DATA.get(self.state, self.SPRITE_DATA["IDLE"])
        
        # Cycle frames roughly every 0.25s
        idx = int(datetime.now().microsecond / 250000) % len(grids)
        
        # Render on demand to save memory? Or cache? 
        # Rendering 20x20 sprites is fast.
        return self._render_grid(grids[idx])
