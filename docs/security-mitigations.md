# Security Mitigations

## diskcache transitive exposure

KERN does not import or instantiate `diskcache` directly. The package can appear in resolved environments through optional local-model dependencies, but KERN keeps runtime artifacts, profile data, and model files under application-managed roots with owner-only storage expectations and does not use `diskcache` for trusted object deserialization.

Operational requirements:

- Do not configure shared or world-writable cache directories for local-model runtimes.
- Keep model/cache directories owned by the KERN runtime user.
- Treat future direct `diskcache` use as security-sensitive and require a non-pickle or explicitly locked-down serializer before merging.
