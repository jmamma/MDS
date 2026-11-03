# MDS

Machine Dump Standard documentation and examples for Machinedrum OS X.20.

MDS lets you add new machines to the Machinedrum: synths, drums and effects
that run on its DSP, installed over Sysex into the USR Machine slots.

- [Machine Dump Standard](docs/MDX_MachineDumpStandard.txt)
- [Machine Dump Protocol](docs/MDX_MachineDumpProtocol.txt)
- [Changelog](docs/Changelog)
- [Examples](machines/examples/)

## Developer tools

[mds-tools](https://github.com/jmamma/mds-tools) provides tools for building and certifying
USR Machines for the Machinedrum, following the MDS specification. It also
supports MIDI SysEx conversion. Its README describes building and
certifying a machine; see “Cycles per track” in the Machine Dump Standard
for the cycle-count requirements.

The bundled example packages use MDS 1.1.
They carry certified counts for MKI DRAM, MKI SRAM and MKII. Rebuild and
recertify a machine after changing its source.

Only install MDS machines from developers you trust. Machine code runs on the DSP
and may cause instability or unexpected audio.
