# MDS

Machine Dump Standard documentation and examples for MDX 2.0.

MDS lets you add new machines to the Machinedrum: synths, drums and effects
that run on its DSP, installed over Sysex into the USR Machine slots.

- [Machine Dump Standard](docs/MDX_MachineDumpStandard.txt)
- [Machine Dump Protocol](docs/MDX_MachineDumpProtocol.txt)
- [Examples](machines/examples/)

## Tools

- [Build an MDS package](tools/mds_pack.py) from its JSON definition and assembled CLD, LOD or raw binary.
- [Convert MDS to Sysex](tools/mds_to_syx.py).

Only install MDS machines from developers you trust. Machine code runs on the DSP
and may cause instability or unexpected audio.
