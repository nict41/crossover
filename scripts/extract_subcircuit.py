"""Extract a sub-netlist from a full netlist JSON by selecting parts.

Usage:
  python scripts/extract_subcircuit.py <netlist.json> --parts Q1,Q2,R31,R32 --out docs/subcircuits/mute_netlist.json

The script writes a small JSON with `parts` and `nets` that reference only the selected parts
and nets they connect to. It also includes `ranges` if present.
"""
import json
import sys
import os
import argparse


def main():
    p = argparse.ArgumentParser()
    p.add_argument('netlist')
    p.add_argument('--parts', help='Comma-separated part refs to include')
    p.add_argument('--nets', help='Comma-separated net names to include')
    p.add_argument('--out', required=True, help='Output JSON file')
    args = p.parse_args()

    with open(args.netlist, 'r', encoding='utf-8') as f:
        nl = json.load(f)

    parts_sel = set()
    if args.parts:
        parts_sel.update([s.strip() for s in args.parts.split(',') if s.strip()])
    nets_sel = set()
    if args.nets:
        nets_sel.update([s.strip() for s in args.nets.split(',') if s.strip()])

    # If no explicit parts/nets given, error
    if not parts_sel and not nets_sel:
        print('No parts or nets specified', file=sys.stderr)
        sys.exit(2)

    parts = nl.get('parts', {})
    nets = nl.get('nets', {})

    # If parts specified, collect nets they touch
    for pref in list(parts_sel):
        if pref not in parts:
            print(f'Warning: part {pref} not found in netlist', file=sys.stderr)
    for netname, pins in nets.items():
        for pin in pins:
            # pin like 'U1A.3' or 'R31.2'
            ref = pin.split('.')[0]
            if ref in parts_sel:
                nets_sel.add(netname)
                break

    # If nets specified, collect parts connected to them
    for net in list(nets_sel):
        if net not in nets:
            print(f'Warning: net {net} not found', file=sys.stderr)
    for net in list(nets_sel):
        for pin in nets.get(net, []):
            ref = pin.split('.')[0]
            parts_sel.add(ref)

    # Now gather subset
    out = {'variant': nl.get('variant', os.path.basename(args.out)), 'parts': {}, 'nets': {}}
    for pref in sorted(parts_sel):
        if pref in parts:
            out['parts'][pref] = parts[pref]
    for net in sorted(nets_sel):
        if net in nets:
            out['nets'][net] = nets[net]

    # Include ranges if present
    if 'ranges' in nl:
        out['ranges'] = nl['ranges']

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=2)
    print('Wrote', args.out)

if __name__ == '__main__':
    main()
