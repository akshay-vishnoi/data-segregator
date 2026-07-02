"""Current-pair sidecar-copy validation notes.

The next copier revision will permit a safe sidecar only when the current source
media and its destination media match by SHA-256, even if a historical plan hash
is stale. The 959 non-one-to-one sidecars remain excluded.
"""
