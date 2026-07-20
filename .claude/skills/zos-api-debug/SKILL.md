---
name: zos-api-debug
description: Use when the OptiVibe MCP can't reach OpticStudio — a tool returns an engine/connection/license error (session_connect, engine_connect_timeout, session_closed), OpticStudio won't connect, or the ZOS-API license is rejected. Install / connection / license troubleshooting.
---

# ZOS-API Debug — Install / Connection / License Troubleshooting

## Overview

The OptiVibe harness drives OpticStudio through the ZOS-API (a typed .NET API reached
via pythonnet). When a design-touching tool can't reach the engine it returns a typed
error envelope rather than raising — this skill maps those failures to what the USER
can fix on their machine (install, architecture, license, a stuck engine). The harness
owns the .NET / pythonnet plumbing and reaps its own engine; you do not write
connection code.

## When to Use

- A design tool returns `error_family: "session_connect"` (couldn't acquire the engine)
- `error_family: "engine_connect_timeout"` (the engine open wedged)
- `error_family: "session_closed"` (the engine transport was lost mid-session)
- OpticStudio won't launch, or the ZOS-API license is rejected
- The MCP seems stuck on the first design-touching call after a restart

## What the user sees

The engine opens LAZILY — the first tool that touches a design takes the single
OpticStudio seat. So install, registration, and reference lookups all succeed
regardless of license tier; a connection or license failure surfaces only on that
first design-touching call, as a typed envelope:

| error_family | Meaning | What to check |
|---|---|---|
| `session_connect` | The engine couldn't be acquired / validated | OpticStudio installed and licensed for the ZOS-API; 64-bit Python ↔ 64-bit OpticStudio; install path resolvable |
| `engine_connect_timeout` | The open WEDGED (a hung handshake or a stuck orphan) | A prior OpticStudio server may be holding the seat — see "stuck engine" below; then retry |
| `session_closed` | The engine transport was lost mid-session | The OpticStudio process died or was closed; retry the call to re-open |

## Triage

### 1. Is OpticStudio licensed for the ZOS-API?

A viewer / design-only seat without API entitlement can connect but fails the API
license check (`IsValidLicenseForAPI`). This is a license/seat problem, not a bug — no
code change fixes it. Confirm the install is licensed for the ZOS-API tier the API
requires.

### 2. Architecture + install path

- 64-bit Python must match 64-bit OpticStudio (an arch mismatch fails the connection).
- The harness resolves the install directory automatically (env override → registry →
  Program Files scan). If resolution fails, set `ZEMAX_DIR` to the install folder.
- The engine and the ZOS-API .NET surface are Windows-only.

### 3. A stuck / wedged engine (`engine_connect_timeout`)

The single seat (N=1) can be held by an orphaned OpticStudio server from an earlier run
— e.g. after Claude Code hard-killed the MCP. The harness reclaims such an orphan on the
next boot (it never touches a concurrent / foreign OpticStudio you are using
interactively). If a first call times out, retry it; if it persists, close any stray
headless `OpticStudio.exe` that no interactive session owns, then retry.

## Environment Checklist

- [ ] 64-bit Python matches 64-bit OpticStudio
- [ ] OpticStudio installed and licensed for the ZOS-API (`IsValidLicenseForAPI`)
- [ ] Install path resolvable (else set `ZEMAX_DIR`)
- [ ] No orphaned headless OpticStudio server holding the single seat
- [ ] Windows (the ZOS-API .NET surface is Windows-only)
