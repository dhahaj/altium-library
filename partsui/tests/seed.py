"""A small fake Celestial for the integration tests (run as sa after 01_init.sql)."""
from __future__ import annotations

LONG_FP_PATH = ("footprints/Capacitor - Ceramic/Some Very Long Vendor Directory Name That Keeps Going/"
                "And Another Nested Directory For Good Measure/PCB - CAPACITOR - CERAMIC - "
                "MURATA GRM188R71H104KA93D 0603_1608 EXTRA LONG NAME.PcbLib")
assert len(LONG_FP_PATH) > 200

SEED_SQL = [
    # Raw upstream tables: lowercase columns, ignored by everything.
    """CREATE TABLE dbo.z_CapacitorsCeramic (partid int, partnumber varchar(200), symbolref varchar(200),
                                              footprintpath varchar(500))""",
    "INSERT INTO dbo.z_CapacitorsCeramic VALUES (1, 'RAW-ONLY-0001', 'Capacitor', 'x.PcbLib')",
    "CREATE TABLE dbo.z_Memory (partid int, partnumber varchar(200))",
    "INSERT INTO dbo.z_Memory VALUES (1, 'RAW-ONLY-0002')",

    # Category tables (materialized views upstream): spaces in names, differing columns.
    """CREATE TABLE dbo.[Capacitors - Ceramic - 0603] (
        [Part Number] varchar(900) NOT NULL, [Comment] varchar(max), [Description] varchar(max),
        [Library Ref] varchar(max), [Library Path] varchar(max),
        [Footprint Ref] varchar(max), [Footprint Path] varchar(max),
        [Footprint Ref 2] varchar(max), [Footprint Path 2] varchar(max),
        [Capacitance] varchar(max), [Voltage] varchar(max), [Tolerance] varchar(max),
        [Manufacturer 1] varchar(max), [Manufacturer Part Number 1] varchar(max),
        [ComponentLink1Description] varchar(max), [ComponentLink1URL] varchar(max))""",
    "CREATE INDEX IX_PartNumber ON dbo.[Capacitors - Ceramic - 0603] ([Part Number])",
    f"""INSERT INTO dbo.[Capacitors - Ceramic - 0603] VALUES
        ('GRM188R71H104KA93D', '100nF', '0.1uF 50V X7R 0603 ceramic capacitor', 'Capacitor',
         'symbols/Passives/SCH - PASSIVES - CAPACITOR.SCHLIB', 'CAP 0603_1608', '{LONG_FP_PATH}',
         NULL, NULL, '100nF', '50V', '10%', 'Murata', 'GRM188R71H104KA93D',
         'Datasheet', 'https://www.murata.com/en-us/products/productdetail?partno=GRM188R71H104KA93D'),
        ('CL10A106KP8NNNC', '10uF', '10uF 10V X5R 0603 ceramic capacitor', 'Capacitor',
         'symbols/Passives/SCH - PASSIVES - CAPACITOR.SCHLIB', 'CAP 0603_1608',
         'footprints/Capacitor - MLCC/PCB - CAPACITOR - MLCC - CAP 0603_1608.PCBLIB',
         NULL, NULL, '10uF', '10V', '10%', 'Samsung', 'CL10A106KP8NNNC', NULL, 'javascript:alert(1)'),
        ('C0603C104K5RAC', '100nF', 'KEMET 100n 50V X7R', 'Capacitor',
         'symbols/Passives/SCH - PASSIVES - CAPACITOR.SCHLIB', 'CAP 0603_1608',
         'footprints/Capacitor - MLCC/PCB - CAPACITOR - MLCC - CAP 0603_1608.PCBLIB',
         NULL, NULL, '100nF', '50V', '10%', 'KEMET', 'C0603C104K5RAC', NULL, NULL)""",

    """CREATE TABLE dbo.[Resistors - Surface Mount - 0402] (
        [Part Number] varchar(900) NOT NULL, [Description] varchar(max),
        [Library Ref] varchar(max), [Library Path] varchar(max),
        [Footprint Ref] varchar(max), [Footprint Path] varchar(max),
        [Value] varchar(max), [Resistance] varchar(max), [Power] varchar(max),
        [Manufacturer 1] varchar(max), [Manufacturer Part Number 1] varchar(max))""",
    "CREATE INDEX IX_PartNumber ON dbo.[Resistors - Surface Mount - 0402] ([Part Number])",
    """INSERT INTO dbo.[Resistors - Surface Mount - 0402] VALUES
        ('RC0402FR-0710KL', '10k 1% 1/16W 0402 thick film resistor', 'Resistor', 'symbols/Passives/R.SchLib',
         'RES 0402_1005', 'footprints/Resistor - Chip/R0402.PcbLib', '10k', '10k', '0.0625W', 'Yageo', 'RC0402FR-0710KL'),
        ('ERJ-2RKF1002X', '10k 1% 0402 Panasonic', 'Resistor', 'symbols/Passives/R.SchLib',
         'RES 0402_1005', 'footprints/Resistor - Chip/R0402.PcbLib', '10k', '10k', '0.1W', 'Panasonic', 'ERJ-2RKF1002X'),
        ('100NF-LOOKALIKE', 'not a capacitor; part number starts with 100', 'Resistor', 'symbols/Passives/R.SchLib',
         'RES 0402_1005', 'footprints/Resistor - Chip/R0402.PcbLib', '0', '0', '0.1W', 'Nobody', '100NF-LOOKALIKE')""",

    # No Comment / Description / Footprint Ref columns at all.
    """CREATE TABLE dbo.[Memory] (
        [Part Number] varchar(900) NOT NULL, [Library Ref] varchar(max), [Library Path] varchar(max),
        [Footprint Path] varchar(max), [Memory Size] varchar(max), [Interface] varchar(max),
        [Pins] int NULL, [Released] date NULL, [Price] decimal(9,4) NULL)""",
    """INSERT INTO dbo.[Memory] VALUES
        ('W25Q128JVSIQ', 'Flash SPI', 'symbols/Memory/W25Q.SchLib', 'footprints/SOIC/SOIC-8.PcbLib', '128Mbit', 'SPI',
         8, '2016-05-01', 0.4200),
        ('100NF-IN-MEMORY?', 'x', 'y', 'z', 'n/a', 'n/a', NULL, NULL, NULL)""",
]


def seed(conn) -> None:
    cur = conn.cursor()
    for stmt in SEED_SQL:
        cur.execute(stmt)
    conn.commit()
