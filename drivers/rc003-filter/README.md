# RC003 Filter Remap Core

This directory contains an experimental KMDF lower filter and its stateless
report-remapping core. Source Raw Input now decodes its eight translated keys,
but the released application has not been rebuilt with these changes.

The implementation is based on the RC003 report facts recorded in the
2026-09-10 investigation:

- report prefix `01 00 00`;
- three little-endian 16-bit usage slots at offsets `3..4`, `5..6`, `7..8`;
- eight RC003 usages translated to F13-F20 usages;
- report length and all bytes outside the three slots remain unchanged.

QL-4 reference files were retrieved using GitHub API/raw URLs after git clone
failed. They are kept separately in workspace scratch/2026-09-10/RemoteMapper-api.
Remo's code uses the documented hardware/report facts and standard KMDF APIs.
BLE, ATVV, UI and action execution continue to use existing application modules.

`RC003_HOST_TEST` builds the function without WDK headers for offline tests.
The driver build compiles the same source with WDK/KMDF headers.

## Current build status (2026-09-11)

- `build-tests.cmd` passes the host-only remap suite (196,608 usage/slot cases plus boundary and release tests).
- `build-driver.cmd` compiles and links an x64 KMDF SYS with the installed WDK libraries and generates a catalog with `Inf2Cat` (no warnings).
- `verify-package.cmd` checks the catalog signature and SYS/INF membership using kernel policy. It returns 2 on verification failure, including the current unsigned catalog; failure is never a successful installability check.
- The INF hardware match is limited to the RC003 keyboard child ID. The present machine's parent BTHLEDevice uses `mshidumdf` and `WUDFRd`; the driver must not replace that parent filter.
- A live RC003 test is still required for the exact report buffer, 13-key behavior, reconnect, and no-impact physical-key regression. Do not package this development SYS as a user installer yet.

Run `build-tests.cmd`, then `build-driver.cmd` from this directory. Outputs are
in the ignored `.build/host` and `.build/driver` directories. The driver script
pins MSVC 14.44, WDK 10.0.26100.0 and KMDF 1.35 (Windows 11 24H2+ x64).
It follows the installed WindowsDriver.KernelMode.KMDF.props link dependencies
directly because the WindowsKernelModeDriver10.0 VS toolset is unregistered.
`inspect-test.ps1` only reports TESTSIGNING, RC003 PnP devices and package
presence. `install-test.ps1` is check-only by default; `-Install` is required
to call pnputil, and `-EnableTestSigning` is required to request a boot setting
change. `uninstall-test.ps1` removes only packages whose published device name
matches this filter and can optionally request TESTSIGNING off. All commands
require an elevated PowerShell and a reboot may be required. Do not run the
install command on a machine where the RC003 child hardware ID has not been
verified.

## Limits before user-mode integration

- F13-F20 are not exclusive to this remote. Retain the exact Raw Input device
  identity, and consume key-up as well as key-down; RegisterHotKey alone is insufficient.
- Native direction/OK passthrough does not prevent duplicate actions when those
  five keys are customized. This eight-key baseline must not be advertised as
  a complete 13-key exclusive mapping solution.
- A maximum report size of 121 does not prove every completion contains 121
  bytes. Preserve Information, and verify the report descriptor/layout live.
- No claim of runtime cancellation, unplug, HVCI or keyboard isolation testing
  is made by host tests, link success, InfVerif or Inf2Cat.
