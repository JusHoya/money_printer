"""
Mr. Krabs Sprite Data — PNG Runtime Loader 🦀
Loads sprite sheets from assets/ at import time using PIL.
Uses LANCZOS resampling for high-fidelity downscaling.
Outputs RGB grids compatible with the half-block (▀) terminal renderer.
"""

import os
import logging
from PIL import Image
import numpy as np

logger = logging.getLogger(__name__)

# --- CONFIGURATION ---
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ASSETS_DIR = os.path.join(_PROJECT_ROOT, "assets")

# Target width in terminal characters
# Each char = 1 pixel wide, 2 pixels tall (▀ block)
# 20 chars wide → ~10 terminal lines for a square sprite
TARGET_WIDTH = 20

# Background removal threshold (Euclidean RGB distance)
BG_THRESHOLD = 50

# Sprite sheet definitions: filename → (state_name, expected_frames)
_SHEET_DEFS = {
    "Idle.png":    ("IDLE", 4),
    "money.png":   ("MONEY_EYES", 4),
    "Panic.png":   ("PANIC", 3),
    "Running.png": ("RUNNING", 3),
    "Violin.png":  ("TINY_VIOLIN", 1),
}


def _detect_bg_color(img_array):
    """Sample corners to determine background color."""
    h, w = img_array.shape[:2]
    samples = [
        img_array[0, 0, :3],
        img_array[0, w-1, :3],
        img_array[h-1, 0, :3],
        img_array[h-1, w-1, :3],
        img_array[0, w//2, :3],       # Top center
        img_array[h-1, w//2, :3],     # Bottom center
    ]
    return np.mean(samples, axis=0).astype(np.uint8)


def _find_frame_bounds(img_array, bg_color):
    """Find horizontal frame boundaries by scanning for background-only columns."""
    h, w = img_array.shape[:2]
    
    # Per-column: fraction of pixels that are background
    bg_cols = []
    for x in range(w):
        col = img_array[:, x, :3].astype(float)
        dists = np.sqrt(np.sum((col - bg_color.astype(float)) ** 2, axis=1))
        bg_cols.append(np.mean(dists < BG_THRESHOLD) > 0.92)
    
    # Find contiguous content regions
    frames = []
    in_frame = False
    start = 0
    
    for x in range(w):
        if not bg_cols[x] and not in_frame:
            start = x
            in_frame = True
        elif bg_cols[x] and in_frame:
            if x - start > 15:
                frames.append((start, x))
            in_frame = False
    
    if in_frame and w - start > 15:
        frames.append((start, w))
    
    return frames


def _trim_vertical(img_array, bg_color):
    """Trim top/bottom background rows."""
    h, w = img_array.shape[:2]
    
    top = 0
    for y in range(h):
        row = img_array[y, :, :3].astype(float)
        dists = np.sqrt(np.sum((row - bg_color.astype(float)) ** 2, axis=1))
        if np.any(dists >= BG_THRESHOLD):
            top = y
            break

    bottom = h
    for y in range(h - 1, -1, -1):
        row = img_array[y, :, :3].astype(float)
        dists = np.sqrt(np.sum((row - bg_color.astype(float)) ** 2, axis=1))
        if np.any(dists >= BG_THRESHOLD):
            bottom = y + 1
            break
    
    return img_array[max(0, top-1):min(h, bottom+1), :, :]


def _process_frame(img_array, bg_color, target_width):
    """
    Process a single frame:
    1. Trim whitespace
    2. Resize with LANCZOS (high-quality anti-aliasing)
    3. Convert to RGB grid with None for transparent pixels
    """
    # Trim
    trimmed = _trim_vertical(img_array, bg_color)
    h, w = trimmed.shape[:2]
    
    if h == 0 or w == 0:
        return [[None] * target_width] * 2
    
    # Calculate target height preserving aspect ratio
    # Terminal chars are ~2x tall as wide, so we halve the height
    # (since ▀ already doubles vertical resolution)
    scale = target_width / w
    target_height = int(h * scale)
    
    # Make even for ▀ pairing
    if target_height % 2 != 0:
        target_height += 1
    target_height = max(2, target_height)
    
    # Resize using PIL LANCZOS — the key to high fidelity
    pil_frame = Image.fromarray(trimmed)
    resized = pil_frame.resize((target_width, target_height), Image.LANCZOS)
    resized_array = np.array(resized)
    
    # Convert to RGB grid with background removal
    grid = []
    for y in range(target_height):
        row = []
        for x in range(target_width):
            pixel = resized_array[y, x]
            rgb = pixel[:3]
            alpha = pixel[3] if len(pixel) > 3 else 255
            
            # Alpha check
            if alpha < 100:
                row.append(None)
                continue
            
            # Background color check
            dist = np.sqrt(np.sum((rgb.astype(float) - bg_color.astype(float)) ** 2))
            if dist < BG_THRESHOLD:
                row.append(None)
            else:
                row.append((int(rgb[0]), int(rgb[1]), int(rgb[2])))
        grid.append(row)
    
    return grid


def _load_sprite_sheet(filename, expected_frames):
    """Load a sprite sheet and extract animation frames."""
    filepath = os.path.join(_ASSETS_DIR, filename)
    
    if not os.path.exists(filepath):
        logger.warning(f"Sprite sheet not found: {filepath}")
        return []
    
    img = Image.open(filepath).convert("RGBA")
    img_array = np.array(img)
    
    bg_color = _detect_bg_color(img_array)
    
    # Split into frames
    if expected_frames == 1:
        frame_bounds = [(0, img_array.shape[1])]
    else:
        frame_bounds = _find_frame_bounds(img_array, bg_color)
        
        # If auto-detection found wrong count, fall back to equal-width split.
        # This handles sprites with intra-frame gaps (e.g. Running.png
        # where spread claws create background columns within a single pose).
        if len(frame_bounds) != expected_frames:
            w = img_array.shape[1]
            frame_w = w // expected_frames
            frame_bounds = [(i * frame_w, (i + 1) * frame_w) for i in range(expected_frames)]
    
    if not frame_bounds:
        frame_bounds = [(0, img_array.shape[1])]
    
    frames = []
    for x_start, x_end in frame_bounds:
        frame_slice = img_array[:, x_start:x_end, :]
        grid = _process_frame(frame_slice, bg_color, TARGET_WIDTH)
        frames.append(grid)
    
    return frames


def _load_all_sprites():
    """Load all sprite sheets and return the SPRITE_FRAMES dictionary."""
    result = {}
    
    for filename, (state_name, expected_frames) in _SHEET_DEFS.items():
        frames = _load_sprite_sheet(filename, expected_frames)
        if frames:
            result[state_name] = frames
        else:
            # Fallback: single empty frame
            result[state_name] = [[[None] * TARGET_WIDTH] * 2]
    
    return result


# Load sprites at import time
SPRITE_FRAMES = _load_all_sprites()

# Also export PALETTE for backward compatibility (not used by new renderer)
PALETTE = {}
