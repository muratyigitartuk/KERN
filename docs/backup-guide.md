# KERN Backup Guide

Use this guide for the current pilot shape: one controlled Windows machine, one active KERN profile, and one local backup destination.

## What gets backed up

Encrypted profile backups include:

- local profile data
- indexed document metadata
- KERN state needed for restore

By default they do **not** expose raw company documents in plain text.

## Where backups go

Default local path:

- `.kern\backups\`

The current backup root is also visible in the KERN settings panel.

## Create a backup

In the product:

- open **Settings**
- enter a backup password
- use **Create encrypted backup**

From the operator flow:

```powershell
python .\scripts\preflight-kern.py --json
```

Then create the backup from the UI so the active profile and audit trail stay aligned.

## Operator expectations

- keep the password separate from the machine
- create a backup before updates
- verify the backup destination is writable in the readiness view
- treat backup files as sensitive local artifacts
