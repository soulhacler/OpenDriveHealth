# Changelog

## 0.1.0 (first public test release)

- Health status (Good / Caution / Bad) using CrystalDiskInfo's rules, and an
  HDD Sentinel-style health percentage with every deduction explained.
- Supports SATA/SAS hard drives, SATA SSDs, NVMe and most USB enclosures via
  smartmontools. Doesn't wake sleeping disks.
- SSD endurance left and total bytes read/written.
- History of every check, with alerts when bad sectors, status, health or
  temperature get worse, and health/temperature charts.
- Dark dashboard-style desktop app (`OpenDriveHealth.exe` / `odh-gui`).
- Command-line tool (`odh`) with JSON output, Nagios-style exit codes and
  `odh dump` for sharing anonymised drive data.
- Windows download with smartctl bundled; no Python needed.
