"""Crop an existing schematic SVG around given part references.

Usage:
  python scripts/crop_svg_by_parts.py \ 
    --svg schematic/esp-p148-3way-crossover-retuned-quad-smd.svg \
    --parts Q1,Q2,Q3,R31,R32,R33,R40,C16,R37,R38,R39 \
    --out docs/subcircuits/images/mute-crop.svg

The script finds <text> elements whose text content matches any of the part
refs, computes a bounding box around them, expands by a margin, and writes a
new SVG with an adjusted viewBox so the cropped area is shown.
"""
import argparse
import re
import xml.etree.ElementTree as ET


def find_text_positions(root):
    # find all <text> elements with numeric x,y attributes
    texts = []
    for t in root.findall('.//{http://www.w3.org/2000/svg}text'):
        x = t.get('x')
        y = t.get('y')
        if x is None or y is None:
            continue
        try:
            xv = float(x)
            yv = float(y)
        except Exception:
            continue
        txt = ''.join(t.itertext()).strip()
        texts.append((txt, xv, yv))
    return texts


def crop(svg_in, parts, out_path, margin=120):
    tree = ET.parse(svg_in)
    root = tree.getroot()
    texts = find_text_positions(root)
    matches = [p for p in texts if p[0] in parts]
    if not matches:
        raise SystemExit('No matching parts found in SVG: ' + ','.join(parts))
    xs = [m[1] for m in matches]
    ys = [m[2] for m in matches]
    minx = max(0, min(xs) - margin)
    miny = max(0, min(ys) - margin)
    maxx = max(xs) + margin
    maxy = max(ys) + margin
    w = maxx - minx
    h = maxy - miny

    # Read original file text and extract inner content between <svg ...> and </svg>
    with open(svg_in, 'r', encoding='utf-8') as f:
        text = f.read()
    m = re.search(r"<svg[^>]*>(.*)</svg>\s*$", text, flags=re.S)
    if not m:
        raise SystemExit('Invalid SVG content')
    inner = m.group(1)

    out_svg = []
    out_svg.append('<?xml version="1.0" encoding="UTF-8"?>')
    out_svg.append('<svg xmlns="http://www.w3.org/2000/svg" '
                   'width="%d" height="%d" viewBox="%g %g %g %g">' % (w, h, minx, miny, w, h))
    out_svg.append(inner)
    out_svg.append('</svg>')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out_svg))
    print('wrote', out_path)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--svg', required=True)
    p.add_argument('--parts', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--margin', type=int, default=120)
    args = p.parse_args()
    parts = [s.strip() for s in args.parts.split(',') if s.strip()]
    crop(args.svg, parts, args.out, args.margin)

if __name__ == '__main__':
    main()
