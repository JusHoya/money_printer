
# Mr. Krabs Sprite Data
# High-Fidelity Pixel Grids (R, G, B) or None
# Generated for Money Printer Dashboard

# Palette for decompression
_P = {
    ' ': None,                  # Transparent
    'R': (235, 60, 60),         # Skin Red (Bright)
    'r': (180, 30, 30),         # Skin Dark (Shadow)
    'L': (200, 255, 200),       # Eye Green (Light)
    'E': (255, 255, 255),       # Eye White/Shine
    'K': (20, 20, 20),          # Black (Pupils/Belt/Outlines)
    'B': (135, 206, 250),       # Shirt Blue (Sky)
    'b': (100, 170, 220),       # Shirt Dark
    'P': (100, 100, 220),       # Pants Purple/Blue
    'G': (255, 215, 0),         # Gold (Buckle)
    'S': (50, 205, 50),         # Money Green
    '$': (255, 255, 255),       # Money White
    'W': (255, 255, 255),       # White (Shirt)
    'F': (255, 100, 100),       # Fire/Panic Red
}

def _decompress(grid_map):
    """Convert char map to RGB grid."""
    return [[_P.get(c) for c in row] for row in grid_map]

# --- SPRITE DEFINITIONS ---

_IDLE_1 = [
    "          LL  LL          ",
    "          LL  LL          ",
    "          KK  KK          ",
    "          LL  LL          ",
    "         RRRRRRRR         ",
    "        RR K RR K R       ",
    "       RRRRRRRRRRRR       ",
    "     RRRRRRRRRRRRRRRR     ",
    "    RRRRBBBBBBBBBBBBRR    ",
    "   K RRRBBBBBBBBBBBBRRR K ",
    "   K RRRBBBBBBBBBBBBRRR K ",
    "  DK RRRKKKKKKKKKKKKRRR KD",
    "  RR RRPPPPPGTPPPPPPRRR RR",
    "  RR  PPPPPPPPPPPPPPPP  RR",
    "      PP PPP    PPP PP    ",
    "      KK            KK    "
]

_IDLE_2 = [
    "          LL  LL          ",
    "          LL  LL          ",
    "          KK  KK          ",
    "          LL  LL          ",
    "         RRRRRRRR         ",
    "        RR K RR K R       ",
    "       RRRRRRRRRRRR       ",
    "     RRRRRRRRRRRRRRRR     ",
    "    RRRRBBBBBBBBBBBBRR    ",
    "   K RRRBBBBBBBBBBBBRRR K ",
    "   K RRRBBBBBBBBBBBBRRR K ",
    "  DK RRRKKKKKKKKKKKKRRR KD",
    "  RR RRPPPPPGTPPPPPPRRR RR",
    "  RR  PPPPPPPPPPPPPPPP  RR",
    "      PP PPP    PPP PP    ",
    "      KK            KK    "
] # (Bobbing would be handled by renderer offset or slight pixel shift, keeping identical for now)

_MONEY_EYES_1 = [
    "         SS    SS         ",
    "         $$    $$         ",
    "         SS    SS         ",
    "          SS  SS          ",
    "         RRRRRRRR         ",
    "        RR   RR   R       ",
    "       RRRRRRRRRRRR       ",
    "     RRRRRRRRRRRRRRRR     ",
    "    RRRRBBBBBBBBBBBBRR    ",
    "   SRRRRBBBBBBBBBBBBRRR S ",
    "   SRRRRBBBBBBBBBBBBRRR S ",
    "  DS RRRKKKKKKKKKKKKRRR SD",
    "  RR RRPPPPPGTPPPPPPRRR RR",
    "  RR  PPPPPPPPPPPPPPPP  RR",
    "      PP PPP    PPP PP    ",
    "      KK            KK    "
]

_TINY_VIOLIN_1 = [
    "          LL  LL          ",
    "          ..  ..          ",
    "          ..  ..          ",
    "          LL  LL          ",
    "         RRRRRRRR         ",
    "        RR - RR - R       ",
    "       RRRRRRRRRRRR       ",
    "     RRRRRRRRRRRRRRRR     ",
    "    RRRRBBBBBBBBBBBBRR    ",
    "       RBBBBBBBBBBBBR     ",
    "    ~  RKKKKKKKKKKKKR     ",
    "   -|- PPPPPGTPPPPPP      ",
    "    | PPPPPPPPPPPPPP      ",
    "      PP PPP    PPP PP    ",
    "      KK            KK    "
]

_PANIC_1 = [
    "          !!  !!          ",
    "          !!  !!          ",
    "          !!  !!          ",
    "          !!  !!          ",
    "         RRRRRRRR         ",
    "        RR O RR O R       ",
    "       RRRRRWRRRRRR       ",
    "     RRRRRRRRRRRRRRRR     ",
    "    FFFFBBBBBBBBBBBBFF    ",
    "   ! RRRBBBBBBBBBBBBRR !  ",
    "   ! RRRBBBBBBBBBBBBRR !  ",
    "  D! RRRKKKKKKKKKKKKRR D! ",
    "  RR RRPPPPPGTPPPPPPRR RR ",
    "  FF  PPPPPPPPPPPPPPPP FF ",
    "      PP PPP    PPP PP    ",
    "      KK            KK    "
]

# Exported Dictionary
SPRITE_FRAMES = {
    "IDLE": [_decompress(_IDLE_1), _decompress(_IDLE_2)],
    "MONEY_EYES": [_decompress(_MONEY_EYES_1)],
    "TINY_VIOLIN": [_decompress(_TINY_VIOLIN_1)],
    "PANIC": [_decompress(_PANIC_1)],
}
