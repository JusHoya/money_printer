#!/usr/bin/env python3
"""
Mr. Krabs Sprite Extraction Pipeline 🦀
Converts PNG sprite sheets → compressed RGB grids for terminal rendering.

Each PNG contains up to 4 animation frames arranged horizontally.
Output: sprite_data.py with SPRITE_FRAMES dict of RGB grids.
"""

import os
import sys
import math
import zlib
import base64
import json
from PIL import Image
import numpy as np

# --- CONFIGURATION ---
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(PROJECT_ROOT, "assets")
OUTPUT_PATH = os.path.join(PROJECT_ROOT, "src", "visualization", "sprite_data.py")

# Target width in terminal columns (each col = 1 pixel wide)
TARGET_WIDTH = 15

# Background color detection threshold (Euclidean distance in RGB space)
BG_THRESHOLD = 45

# Sprite sheet definitions: filename → state name
SPRITE_SHEETS = {
    "Idle.png":    "IDLE",
    "money.png":   "MONEY_EYES",
    "Panic.png":   "PANIC",
    "Running.png": "RUNNING",
    "Violin.png":  "TINY_VIOLIN",
}


def detect_background_color(img_array):
    """Sample corners to determine the background color."""
    h, w = img_array.shape[:2]
    corners = [
        img_array[0, 0, :3],
        img_array[0, w-1, :3],
        img_array[h-1, 0, :3],
        img_array[h-1, w-1, :3],
    ]
    # Average the corner colors
    bg = np.mean(corners, axis=0).astype(np.uint8)
    return bg


def color_distance(c1, c2):
    """Euclidean distance between two RGB colors."""
    return math.sqrt(sum((int(a) - int(b)) ** 2 for a, b in zip(c1, c2)))


def split_frames(img_array, bg_color):
    """
    Auto-detect frame boundaries by finding vertical separator columns
    that are entirely background-colored.
    """
    h, w = img_array.shape[:2]
    
    # For each column, check if it's a "separator" (all background)
    is_bg_col = []
    for x in range(w):
        col = img_array[:, x, :3]
        # Check if most pixels in this column match background
        dists = np.sqrt(np.sum((col.astype(float) - bg_color.astype(float)) ** 2, axis=1))
        bg_ratio = np.mean(dists < BG_THRESHOLD)
        is_bg_col.append(bg_ratio > 0.95)
    
    # Find contiguous non-bg regions (frames)
    frames = []
    in_frame = False
    frame_start = 0
    
    for x in range(w):
        if not is_bg_col[x] and not in_frame:
            frame_start = x
            in_frame = True
        elif is_bg_col[x] and in_frame:
            # End of frame — must be at least 20px wide to be real
            if x - frame_start > 20:
                frames.append((frame_start, x))
            in_frame = False
    
    # Catch frame at right edge
    if in_frame and w - frame_start > 20:
        frames.append((frame_start, w))
    
    return frames


def crop_frame(img_array, x_start, x_end, bg_color):
    """Crop a single frame and trim vertical whitespace."""
    frame = img_array[:, x_start:x_end]
    h, w = frame.shape[:2]
    
    # Find top/bottom content bounds
    top = 0
    for y in range(h):
        row = frame[y, :, :3]
        dists = np.sqrt(np.sum((row.astype(float) - bg_color.astype(float)) ** 2, axis=1))
        if np.any(dists >= BG_THRESHOLD):
            top = y
            break
    
    bottom = h
    for y in range(h - 1, -1, -1):
        row = frame[y, :, :3]
        dists = np.sqrt(np.sum((row.astype(float) - bg_color.astype(float)) ** 2, axis=1))
        if np.any(dists >= BG_THRESHOLD):
            bottom = y + 1
            break
    
    # Add 1px padding
    top = max(0, top - 1)
    bottom = min(h, bottom + 1)
    
    return frame[top:bottom, :, :]


def remove_background(img_array, bg_color):
    """Replace background pixels with None markers."""
    h, w = img_array.shape[:2]
    grid = []
    
    for y in range(h):
        row = []
        for x in range(w):
            pixel = img_array[y, x]
            rgb = pixel[:3]
            alpha = pixel[3] if len(pixel) > 3 else 255
            
            # Check alpha first
            if alpha < 128:
                row.append(None)
                continue
            
            # Check color distance from background
            dist = color_distance(rgb, bg_color)
            if dist < BG_THRESHOLD:
                row.append(None)
            else:
                row.append(tuple(int(c) for c in rgb))
        grid.append(row)
    
    return grid


def downscale_grid(grid, target_width):
    """Downscale an RGB grid to target width, preserving aspect ratio."""
    if not grid or not grid[0]:
        return grid
    
    src_h = len(grid)
    src_w = len(grid[0])
    
    scale = target_width / src_w
    target_height = max(2, int(src_h * scale))
    
    # Make height even for ▀ block rendering (2 rows = 1 terminal line)
    if target_height % 2 != 0:
        target_height += 1
    
    new_grid = []
    for y in range(target_height):
        row = []
        src_y = int(y / scale)
        src_y = min(src_y, src_h - 1)
        
        for x in range(target_width):
            src_x = int(x / scale)
            src_x = min(src_x, src_w - 1)
            
            pixel = grid[src_y][src_x]
            row.append(pixel)
        new_grid.append(row)
    
    return new_grid


def compress_grid(grid):
    """Compress an RGB grid to a compact string representation."""
    # Encode as JSON, compress with zlib, then base64
    # Format: list of rows, each row is list of [r,g,b] or null
    data = []
    for row in grid:
        encoded_row = []
        for pixel in row:
            if pixel is None:
                encoded_row.append(None)
            else:
                encoded_row.append(list(pixel))
        data.append(encoded_row)
    
    json_str = json.dumps(data, separators=(',', ':'))
    compressed = zlib.compress(json_str.encode('utf-8'), 9)
    b64 = base64.b64encode(compressed).decode('ascii')
    return b64


def decompress_grid(b64_str):
    """Decompress a base64+zlib compressed grid back to RGB tuples."""
    compressed = base64.b64decode(b64_str)
    json_str = zlib.decompress(compressed).decode('utf-8')
    data = json.loads(json_str)
    
    grid = []
    for row in data:
        decoded_row = []
        for pixel in row:
            if pixel is None:
                decoded_row.append(None)
            else:
                decoded_row.append(tuple(pixel))
        decoded_row.append(decoded_row)
    
    return grid


def process_sprite_sheet(filepath, state_name):
    """Process a single sprite sheet PNG into animation frames."""
    print(f"\n{'='*60}")
    print(f"  Processing: {os.path.basename(filepath)} → {state_name}")
    print(f"{'='*60}")
    
    img = Image.open(filepath).convert("RGBA")
    img_array = np.array(img)
    print(f"  Image size: {img.size}")
    
    # Detect background
    bg_color = detect_background_color(img_array)
    print(f"  Background color: RGB({bg_color[0]}, {bg_color[1]}, {bg_color[2]})")
    
    # Split into frames
    frame_bounds = split_frames(img_array, bg_color)
    print(f"  Detected {len(frame_bounds)} frames: {frame_bounds}")
    
    if not frame_bounds:
        # Single frame (e.g., Violin.png) — use entire image
        print(f"  No separators found, treating as single frame")
        frame_bounds = [(0, img_array.shape[1])]
    
    frames = []
    for i, (x_start, x_end) in enumerate(frame_bounds):
        print(f"  Frame {i+1}: x=[{x_start}:{x_end}] ({x_end - x_start}px wide)")
        
        # Crop and trim
        cropped = crop_frame(img_array, x_start, x_end, bg_color)
        print(f"    Cropped: {cropped.shape[1]}x{cropped.shape[0]}")
        
        # Remove background
        grid = remove_background(cropped, bg_color)
        print(f"    Grid: {len(grid[0])}x{len(grid)} pixels")
        
        # Downscale
        scaled = downscale_grid(grid, TARGET_WIDTH)
        print(f"    Scaled: {len(scaled[0])}x{len(scaled)} → {len(scaled)//2} terminal lines")
        
        frames.append(scaled)
    
    return frames


def generate_sprite_data(all_frames):
    """Generate the sprite_data.py file with compressed frame data."""
    
    lines = [
        '"""',
        'Mr. Krabs Sprite Data — Auto-generated by extract_sprites.py',
        'High-fidelity pixel grids extracted from PNG sprite sheets.',
        'Each frame is a compressed list of RGB tuples (or None for transparent).',
        '"""',
        '',
        'import zlib',
        'import base64',
        'import json',
        '',
        '',
        'def _decompress(b64_str):',
        '    """Decompress a base64+zlib compressed grid to RGB tuples."""',
        '    compressed = base64.b64decode(b64_str)',
        '    json_str = zlib.decompress(compressed).decode("utf-8")',
        '    data = json.loads(json_str)',
        '    grid = []',
        '    for row in data:',
        '        decoded_row = []',
        '        for pixel in row:',
        '            if pixel is None:',
        '                decoded_row.append(None)',
        '            else:',
        '                decoded_row.append(tuple(pixel))',
        '        grid.append(decoded_row)',
        '    return grid',
        '',
        '',
        '# --- COMPRESSED FRAME DATA ---',
        '',
    ]
    
    # Write compressed data for each state
    frame_var_names = {}  # state → [var_name1, var_name2, ...]
    
    for state_name, frames in all_frames.items():
        frame_var_names[state_name] = []
        for i, frame in enumerate(frames):
            var_name = f"_{state_name}_{i+1}"
            b64_data = compress_grid(frame)
            
            # Split long base64 strings into multi-line for readability
            chunk_size = 100
            chunks = [b64_data[j:j+chunk_size] for j in range(0, len(b64_data), chunk_size)]
            
            lines.append(f'{var_name} = (')
            for chunk in chunks:
                lines.append(f'    "{chunk}"')
            lines.append(')')
            lines.append('')
            
            frame_var_names[state_name].append(var_name)
    
    # Write the SPRITE_FRAMES export
    lines.append('')
    lines.append('# --- EXPORTED SPRITE DICTIONARY ---')
    lines.append('')
    lines.append('SPRITE_FRAMES = {')
    
    for state_name, var_names in frame_var_names.items():
        decomp_list = ", ".join([f"_decompress({v})" for v in var_names])
        lines.append(f'    "{state_name}": [{decomp_list}],')
    
    lines.append('}')
    lines.append('')
    
    return "\n".join(lines)


def main():
    print("=" * 60)
    print("  🦀 MR. KRABS SPRITE EXTRACTION PIPELINE 🦀")
    print("=" * 60)
    print(f"  Assets:  {ASSETS_DIR}")
    print(f"  Output:  {OUTPUT_PATH}")
    print(f"  Target:  {TARGET_WIDTH}px wide")
    
    all_frames = {}
    
    for filename, state_name in SPRITE_SHEETS.items():
        filepath = os.path.join(ASSETS_DIR, filename)
        
        if not os.path.exists(filepath):
            print(f"\n  ⚠️  MISSING: {filepath}")
            continue
        
        frames = process_sprite_sheet(filepath, state_name)
        all_frames[state_name] = frames
    
    # Generate output
    print(f"\n{'='*60}")
    print(f"  Generating sprite_data.py...")
    
    output = generate_sprite_data(all_frames)
    
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        f.write(output)
    
    # Stats
    total_frames = sum(len(f) for f in all_frames.values())
    file_size = os.path.getsize(OUTPUT_PATH)
    
    print(f"  ✅ Written: {OUTPUT_PATH}")
    print(f"  📊 States: {len(all_frames)}, Total Frames: {total_frames}")
    print(f"  📦 File size: {file_size:,} bytes")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
