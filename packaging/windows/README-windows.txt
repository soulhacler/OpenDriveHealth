OpenDriveHealth for Windows
===========================

OpenDriveHealth.exe   Desktop app. Double-click it. Windows will ask for
                      Administrator rights, which are needed to read drive
                      health (S.M.A.R.T.) data.

odh.exe               Command-line tool. Run it from an Administrator
                      Command Prompt or PowerShell, e.g.
                          odh            summary of all drives
                          odh show 0     full details of the first drive
                          odh --demo     sample drives, no hardware needed
                          odh --help

Try the app without touching real drives:  OpenDriveHealth.exe --demo

Windows SmartScreen may warn about an unrecognised app because test builds
are not code-signed. Choose "More info" -> "Run anyway".

History is stored in %APPDATA%\OpenDriveHealth.

Licences
--------
OpenDriveHealth is MIT licensed (LICENSE).
It includes smartctl from smartmontools (https://www.smartmontools.org),
GPL-2.0 licensed (smartmontools-COPYING.txt), unmodified. Source code:
https://github.com/smartmontools/smartmontools

Project page and bug reports: https://github.com/soulhacler/OpenDriveHealth
