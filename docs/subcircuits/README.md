# Subcircuit documentation

This folder contains documentation and tools to extract and document individual
functional blocks (subcircuits) from the full design.

Use `scripts/extract_subcircuit.py` to produce a small netlist JSON containing only
parts and nets relevant to a subcircuit. Import that JSON into your schematic
viewer (EasyEDA/your CAD) or feed it into the project's schematic generator to
render an isolated diagram.

Example:

```bash
python scripts/extract_subcircuit.py docs/netlist-esp-p148-3way-state-variable-crossover.json \
  --parts Q1,Q2,Q3,R31,R32,R33,R34,R35,R36,D1,D2,D3,R40,C16,D4,R37,R38,R39,J6 \
  --out docs/subcircuits/mute_netlist.json
```

Commands to generate the mute diagram (suggested):

- Open `docs/subcircuits/mute_netlist.json` in EasyEDA or your schematic viewer.
- Or use the repository's `tools/gen_schematic.py` if you have a flow for converting
  the netlist JSON into an SVG image (local build required).
