"""
Generate the referme.help brand logo — "Directed Pathways" philosophy.

Mark: two nodes connected by a smooth directed curve — one filled (origin),
one ring (destination), with a subtle chevron indicating direction of referral.
Wordmark: "refer" light, "me" green, "." green accent, "help" light.
"""

import math

GREEN = "#68d391"
TEXT_LIGHT = "#e2e8f0"
BG_DARK = "#111827"

TOTAL_HEIGHT = 28
NODE_R = 2.8
PATH_STROKE = 1.5


def generate_svg():
    # ── Mark geometry ────────────────────────────────────────────────
    # Two nodes: origin (bottom-left), destination (top-right)
    # Connected by a clean bezier curve
    mark_w = 24
    cx1, cy1 = 4.0, 18.5   # origin node
    cx2, cy2 = 20.0, 9.5   # destination node

    # Single smooth cubic bezier (gentle arc, not S-curve)
    cp1x, cp1y = 9.0, 8.0
    cp2x, cp2y = 14.0, 9.0

    # Chevron at destination — angle of final approach
    dx = cx2 - cp2x
    dy = cy2 - cp2y
    angle = math.atan2(dy, dx)
    tick_len = 3.8
    tick_spread = 0.5
    tx1 = cx2 - tick_len * math.cos(angle - tick_spread)
    ty1 = cy2 - tick_len * math.sin(angle - tick_spread)
    tx2 = cx2 - tick_len * math.cos(angle + tick_spread)
    ty2 = cy2 - tick_len * math.sin(angle + tick_spread)

    # ── Wordmark ─────────────────────────────────────────────────────
    text_x = mark_w + 7
    text_y = 19.5
    font_size = 15
    dot_size = font_size * 1.15

    # Estimate width
    char_w = font_size * 0.56
    total_w = text_x + char_w * 12 + 6

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total_w:.0f} {TOTAL_HEIGHT}" height="{TOTAL_HEIGHT}" fill="none">
  <g>
    <!-- Directed path -->
    <path d="M {cx1:.1f} {cy1:.1f} C {cp1x:.1f} {cp1y:.1f}, {cp2x:.1f} {cp2y:.1f}, {cx2:.1f} {cy2:.1f}"
          stroke="{GREEN}" stroke-width="{PATH_STROKE}" stroke-linecap="round" fill="none" opacity="0.8"/>
    <!-- Direction chevron -->
    <path d="M {tx1:.1f} {ty1:.1f} L {cx2:.1f} {cy2:.1f} L {tx2:.1f} {ty2:.1f}"
          stroke="{GREEN}" stroke-width="{PATH_STROKE}" stroke-linecap="round" stroke-linejoin="round" fill="none" opacity="0.8"/>
    <!-- Origin node (filled) -->
    <circle cx="{cx1:.1f}" cy="{cy1:.1f}" r="{NODE_R}" fill="{GREEN}" opacity="0.9"/>
    <!-- Destination node (ring) -->
    <circle cx="{cx2:.1f}" cy="{cy2:.1f}" r="{NODE_R}" stroke="{GREEN}" stroke-width="{PATH_STROKE}" fill="none" opacity="0.9"/>
  </g>
  <text x="{text_x:.1f}" y="{text_y:.1f}"
        font-family="-apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI', sans-serif"
        font-size="{font_size}" font-weight="600" letter-spacing="-0.3px">
    <tspan fill="{TEXT_LIGHT}">refer</tspan><tspan fill="{GREEN}" font-weight="700">me</tspan><tspan fill="{GREEN}" font-size="{dot_size:.1f}" font-weight="700">.</tspan><tspan fill="{TEXT_LIGHT}" font-weight="500">help</tspan>
  </text>
</svg>'''
    return svg


def generate_mark_only():
    """Standalone mark for favicon / small contexts."""
    NODE_R_F = 3.2
    PS = 1.8
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32" fill="none">
  <rect width="32" height="32" rx="6" fill="{BG_DARK}"/>
  <g transform="translate(4, 3)">
    <path d="M 3.5 18 C 8 7.5, 13 8.5, 20 8"
          stroke="{GREEN}" stroke-width="{PS}" stroke-linecap="round" fill="none"/>
    <path d="M 17.5 11.2 L 20 8 L 16.2 7.5"
          stroke="{GREEN}" stroke-width="{PS}" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
    <circle cx="3.5" cy="18" r="{NODE_R_F}" fill="{GREEN}"/>
    <circle cx="20" cy="8" r="{NODE_R_F}" stroke="{GREEN}" stroke-width="{PS}" fill="none"/>
  </g>
</svg>'''


if __name__ == "__main__":
    import os
    import subprocess

    out_dir = os.path.dirname(os.path.abspath(__file__))

    # Main logo
    svg_path = os.path.join(out_dir, "logo.svg")
    with open(svg_path, "w") as f:
        f.write(generate_svg())
    print("  logo.svg")

    # Favicon mark
    fav_path = os.path.join(out_dir, "favicon.svg")
    with open(fav_path, "w") as f:
        f.write(generate_mark_only())
    print("  favicon.svg")

    # PNG previews
    for name, src in [("logo.png", svg_path), ("favicon.png", fav_path)]:
        png_path = os.path.join(out_dir, name)
        try:
            subprocess.run(
                ["rsvg-convert", "-z", "4", src, "-o", png_path],
                check=True, capture_output=True
            )
            print(f"  {name}")
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass

    # Dark-background preview
    preview_html = f'''<!DOCTYPE html>
<html><head><style>
body {{ background: {BG_DARK}; display: flex; align-items: center; justify-content: center; height: 100vh; gap: 40px; }}
.box {{ padding: 20px 30px; border: 1px solid #2d3748; border-radius: 8px; }}
.label {{ color: #718096; font: 11px/1 -apple-system, sans-serif; margin-bottom: 12px; }}
</style></head><body>
<div class="box"><div class="label">navbar size</div><img src="logo.svg" height="28"></div>
<div class="box"><div class="label">2x</div><img src="logo.svg" height="56"></div>
<div class="box"><div class="label">favicon</div><img src="favicon.svg" width="32" height="32"> <img src="favicon.svg" width="16" height="16"></div>
</body></html>'''

    preview_path = os.path.join(out_dir, "preview.html")
    with open(preview_path, "w") as f:
        f.write(preview_html)
    print("  preview.html (open in browser to review)")
