# Mute circuit

This document isolates the mute and soft-start circuitry. Use the extraction
script to produce a small netlist that can be rendered standalone.

Suggested parts to extract (from `docs/netlist-...json`):

- `Q1`, `Q2`, `Q3` (JFETs)
- `R31`, `R32`, `R33` (series resistors)
- `R34`, `R35`, `R36` (gate pulldowns)
- `D1`, `D2`, `D3`, `D4` (steering diodes)
- `R40`, `C16` (soft-start RC)
- `R37`, `R38`, `R39` (LED series resistors)
- `J6` (panel header)

Extract command example:

```bash
python scripts/extract_subcircuit.py docs/netlist-esp-p148-3way-state-variable-crossover.json \
  --parts Q1,Q2,Q3,R31,R32,R33,R34,R35,R36,D1,D2,D3,D4,R40,C16,R37,R38,R39,J6 \
  --out docs/subcircuits/mute_netlist.json
```

Notes:
- The extracted netlist contains only the listed parts and the nets they connect to.
- Import `mute_netlist.json` into your schematic tool to render the isolated diagram.
- If you want annotations (labels, short description) edit this file or the netlist
  JSON before rendering.

## Diagram

Embedded simplified diagram (SVG):

![Mute circuit diagram](images/mute.svg)
