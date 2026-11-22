# Test fixtures

`libs/celestial/` holds four real library files from the Celestial Altium Library
(https://github.com/issus/altium-library), copied unchanged at their upstream paths so the
scanner tests run against genuine Altium OLE files:

- `symbols/Passives/SCH - PASSIVES - CAPACITOR.SCHLIB`
- `symbols/ADC/SCH - ADC - TI ADS1115.SchLib`
- `footprints/Capacitor - MLCC/PCB - CAPACITOR - MLCC - CAP 0402_1005.PCBLIB`
- `footprints/Leadless - DFN/PCB - LEADLESS - DFN - NEXPERIA DFN2020M-6 SOT1220-2 P-CH.PcbLib`
  (its footprint name is longer than an OLE storage name, so it tests the truncation fallback)

`libs/daniel/Daniel.SchLib` is another Celestial symbol file (the inductor library) under a
different name, standing in for the owner's own library. `libs/daniel/Broken.PcbLib` is a plain
text file that the scanner must skip without failing.

These files are the work of the Celestial Altium Library contributors and are used here under
its license: free to use, never to be sold, with attribution back to the source above.
