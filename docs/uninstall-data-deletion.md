# KERN Uninstall And Data Deletion

KERN uninstall is split into two modes:

- remove runtime artifacts only
- remove runtime artifacts **and** local profile data

## Default uninstall

```powershell
.\scripts\uninstall-kern.ps1
```

Default behavior:

- stops local runtime processes if found
- removes the scheduled task if it exists
- removes `.venv`
- removes runtime logs
- preserves `.kern` profile data and backups

Use this when you want to remove the runtime without deleting company data.

## Full uninstall with data removal

```powershell
.\scripts\uninstall-kern.ps1 -RemoveData
```

This additionally removes:

- `.kern`
- `.tokens`

Use it only when the operator explicitly intends to delete the local KERN data footprint.

## Practical rule

If the operator is unsure, use the default uninstall first and verify backups before deleting any preserved data.
