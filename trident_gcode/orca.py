"""Simple basic filament settings for PLA, PETG, ABS, TPU."""
from __future__ import annotations
from dataclasses import dataclass, field

BASIC_FILAMENTS = {
    "PLA": {
        "filament_type": "PLA",
        "nozzle_temp": 210.0,
        "bed_temp": 60.0,
        "max_volumetric_speed": 15.0,
        "retraction_length": 1.0,
        "pressure_advance": 0.04,
        "fan_min_speed": 1.0,
        "fan_max_speed": 1.0,
        "flow_ratio": 1.0,
    },
    "PETG": {
        "filament_type": "PETG",
        "nozzle_temp": 240.0,
        "bed_temp": 70.0,
        "max_volumetric_speed": 12.0,
        "retraction_length": 1.2,
        "pressure_advance": 0.05,
        "fan_min_speed": 0.2,
        "fan_max_speed": 0.5,
        "flow_ratio": 0.95,
    },
    "ABS": {
        "filament_type": "ABS",
        "nozzle_temp": 255.0,
        "bed_temp": 100.0,
        "max_volumetric_speed": 18.0,
        "retraction_length": 0.8,
        "pressure_advance": 0.03,
        "fan_min_speed": 0.0,
        "fan_max_speed": 0.2,
        "flow_ratio": 0.95,
    },
    "TPU": {
        "filament_type": "TPU",
        "nozzle_temp": 230.0,
        "bed_temp": 50.0,
        "max_volumetric_speed": 4.0,
        "retraction_length": 0.0,
        "pressure_advance": 0.0,
        "fan_min_speed": 0.8,
        "fan_max_speed": 0.8,
        "flow_ratio": 1.05,
    }
}

def list_filaments() -> list[str]:
    """User-facing filament names."""
    return list(BASIC_FILAMENTS.keys())

@dataclass
class FilamentSettings:
    name: str = "Unknown"
    filament_type: str = "PLA"
    vendor: str | None = None

    nozzle_temp: float | None = None
    nozzle_temp_first_layer: float | None = None
    bed_temp: float | None = None
    bed_temp_first_layer: float | None = None

    flow_ratio: float | None = None
    max_volumetric_speed: float | None = None   # mm^3/s
    retraction_length: float | None = None
    z_hop: float | None = None

    fan_min_speed: float | None = None           # 0..1
    fan_max_speed: float | None = None           # 0..1
    pressure_advance: float | None = None
    exhaust_during_print: float | None = None     # 0..1
    exhaust_after_print: float | None = None      # 0..1

    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_orca(cls, name: str) -> "FilamentSettings":
        key = name.upper() if name.upper() in BASIC_FILAMENTS else name
        if key not in BASIC_FILAMENTS:
            raise KeyError(f"Filament '{name}' not found.")
        d = BASIC_FILAMENTS[key]
        return cls(
            name=name,
            filament_type=d["filament_type"],
            nozzle_temp=d["nozzle_temp"],
            nozzle_temp_first_layer=d["nozzle_temp"],
            bed_temp=d["bed_temp"],
            bed_temp_first_layer=d["bed_temp"],
            flow_ratio=d["flow_ratio"],
            max_volumetric_speed=d["max_volumetric_speed"],
            retraction_length=d["retraction_length"],
            pressure_advance=d["pressure_advance"],
            fan_min_speed=d["fan_min_speed"],
            fan_max_speed=d["fan_max_speed"],
            raw=d
        )

    def writer_kwargs(self) -> dict:
        kw: dict = {"material": (self.filament_type or "PLA").upper()}
        if self.nozzle_temp is not None:
            kw["nozzle_temp"] = self.nozzle_temp
        if self.bed_temp is not None:
            kw["bed_temp"] = self.bed_temp
        if self.flow_ratio is not None:
            kw["flow_multiplier"] = self.flow_ratio
        if self.max_volumetric_speed:
            kw["max_volumetric_speed"] = self.max_volumetric_speed
        if self.retraction_length is not None:
            kw["retraction_length"] = self.retraction_length
        if self.pressure_advance is not None:
            kw["pressure_advance"] = self.pressure_advance
        if self.fan_max_speed is not None:
            kw["fan_speed"] = self.fan_max_speed
        return kw

    def summary(self) -> str:
        def f(v, unit=""):
            return f"{v:g}{unit}" if v is not None else "-"
        return (
            f"{self.name}  ({self.filament_type})\n"
            f"  nozzle      : {f(self.nozzle_temp,'C')}\n"
            f"  bed         : {f(self.bed_temp,'C')}\n"
            f"  flow ratio  : {f(self.flow_ratio)}\n"
            f"  max vol.    : {f(self.max_volumetric_speed,' mm^3/s')}\n"
            f"  retraction  : {f(self.retraction_length,' mm')}  z-hop {f(self.z_hop,' mm')}\n"
            f"  fan         : {f(self.fan_min_speed)}..{f(self.fan_max_speed)}  "
            f"PA {f(self.pressure_advance)}"
        )

def _main(argv: list[str] | None = None) -> int:
        for n in list_filaments():
            print(f"  {n}")
        return 0

if __name__ == "__main__":
    raise SystemExit(_main())
