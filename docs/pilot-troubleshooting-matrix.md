# KERN Pilot Troubleshooting Matrix

Use this matrix for the most common pilot-visible problems.

| Symptom | Likely cause | First operator action | Next artifact |
| --- | --- | --- | --- |
| License shows invalid | wrong file, tampered signature, or wrong public key | import the signed license again and rerun license check | support bundle + license file metadata |
| License shows expired | pilot license window ended | replace the offline license file and recheck settings | support bundle if the state does not refresh |
| Model path invalid | local GGUF path missing or moved | rerun readiness, confirm the configured model path, fix the local file path | readiness JSON |
| Runtime unreachable | local app process not running or port blocked | relaunch `run-kern.ps1`, then check `/health` and `/health/ready` | runtime log + readiness JSON |
| Upload blocked | no valid license or profile is locked | unlock the profile or import a valid license first | support bundle if the gate is unexpected |
| Sample workspace did not seed | sample assets missing or sample state is inconsistent | restart the sample workspace from onboarding and rerun readiness | support bundle |
| Update failed | package or dependency drift during manual update | review update state, validate restore bundle, then restore if needed | update-state + restore report |
| Restore failed | invalid password, damaged bundle, or bad target root | run `restore-kern.py --validate-only`, then retry with the correct target | restore JSON + support bundle |

## Support handoff

Send:

- support bundle zip
- package smoke report if the issue came from a packaged handoff
- update/restore or uninstall smoke report when relevant

Do not send:

- raw company documents
- generated business drafts
- direct database copies unless explicitly requested for local operator debugging
