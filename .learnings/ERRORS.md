## [ERR-20260816-001] powershell-rg-quote-parsing

**Logged**: 2026-08-16T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
PowerShell parsed alternation text containing embedded quote characters as a command rather than a single `rg` pattern.

### Error
```text
The term '/v1/auth/verify' is not recognized as a name of a cmdlet.
```

### Context
- A post-change residual-secret scan used a double-quoted regular expression with `['\"]`.
- PowerShell split the command before it reached ripgrep.

### Suggested Fix
Use single-quoted PowerShell strings for literal ripgrep patterns, and avoid embedding quote escapes in a double-quoted command argument.

### Metadata
- Reproducible: yes
- Related Files: static/index.html, app.py, README.md

### Resolution
- **Resolved**: 2026-08-16T00:00:00+08:00
- **Notes**: Subsequent scans use a single-quoted pattern.

---

## [ERR-20260816-002] local-python-missing-project-dependency

**Logged**: 2026-08-16T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
The host Python interpreter cannot import `requests`, so a direct prompt-protection unit test could not start.

### Error
```text
ModuleNotFoundError: No module named 'requests'
```

### Context
- The repository declares `requests` in `requirements.txt`.
- The current host interpreter is not the project's installed runtime.

### Suggested Fix
Use the bundled `.vendor` runtime if it includes the dependency, otherwise retain the successful compile check and report that full runtime tests require `pip install -r requirements.txt`.

### Metadata
- Reproducible: yes
- Related Files: requirements.txt, prompt_enhance.py

### Resolution
- **Resolved**: 2026-08-16T00:00:00+08:00
- **Notes**: Used a minimal in-memory `requests` test double for code paths that do not require a live provider; prompt-leak and moderation-output containment checks passed.

---

## [ERR-20260816-003] bundled-runtime-incomplete

**Logged**: 2026-08-16T00:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: tests

### Summary
The checked-in `.vendor` directory contains FastAPI but omits its required `annotated_doc` dependency, so it cannot run the API test suite in isolation.

### Error
```text
ModuleNotFoundError: No module named 'annotated_doc'
```

### Context
- `PYTHONPATH=.vendor python -c "import fastapi"` failed before loading the application.
- The project runtime declares its dependencies in `requirements.txt`.

### Suggested Fix
Install `requirements.txt` into the ignored `.pip-tmp` test directory and execute endpoint smoke tests using that directory.

### Metadata
- Reproducible: yes
- Related Files: .vendor, requirements.txt

### Resolution
- **Resolved**: 2026-08-16T00:00:00+08:00
- **Notes**: Installed declared dependencies into ignored `.pip-tmp`; it supplies the complete test runtime.

---

## [ERR-20260816-004] sandboxed-pip-network-denied

**Logged**: 2026-08-16T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
Installing declared test dependencies into the ignored workspace directory failed because the sandbox blocked outbound package-index connections.

### Error
```text
WinError 10013: An attempt was made to access a socket in a way forbidden by its access permissions.
```

### Context
- Command: `python -m pip install --target .pip-tmp -r requirements.txt`
- The command is needed only to run local endpoint smoke tests; it does not modify system Python.

### Suggested Fix
Request scoped network escalation for this exact dependency-install command.

### Metadata
- Reproducible: yes
- Related Files: requirements.txt, .pip-tmp

### Resolution
- **Resolved**: 2026-08-16T00:00:00+08:00
- **Notes**: Scoped escalation completed the installation in the ignored test directory.

---

## [ERR-20260816-005] isolated-pip-install-incomplete

**Logged**: 2026-08-16T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
The escalated pip output ended during downloads and the target directory still cannot import FastAPI.

### Error
```text
ModuleNotFoundError: No module named 'fastapi'
```

### Context
- A dependency-install command completed without a final success line in its captured output.
- The next isolated import used `PYTHONPATH=.pip-tmp` and failed.

### Suggested Fix
Inspect the target directory and rerun the exact scoped install if packages were not installed.

### Metadata
- Reproducible: yes
- Related Files: .pip-tmp, requirements.txt

### Resolution
- **Resolved**: 2026-08-16T00:00:00+08:00
- **Notes**: Packages were present; the failed import used a relative `PYTHONPATH`. The final isolated API smoke test passed with the resolved workspace path.

---

## [ERR-20260816-008] route-function-test-bypassed-dependency

**Logged**: 2026-08-16T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
A direct Python call to an admin route passed a user dictionary straight into its parameter and therefore bypassed FastAPI's `Depends(require_admin)` injection.

### Error
```text
AssertionError: ordinary user reached admin endpoint
```

### Context
- This was a test-harness artifact, not an HTTP authorization result.
- The route is protected when FastAPI resolves its declared dependency.

### Suggested Fix
Test `require_admin` explicitly in the no-HTTP-client harness, or use an ASGI client when one is available.

### Metadata
- Reproducible: yes
- Related Files: app.py

---

## [ERR-20260816-009] readme-patch-context-stale

**Logged**: 2026-08-16T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: docs

### Summary
A documentation patch mixed an outdated deployment comment with current README context, so the patch engine correctly rejected it.

### Error
```text
apply_patch verification failed: Failed to find expected lines
```

### Context
- Authentication documentation had already changed earlier in this task.

### Suggested Fix
Read the specific README section immediately before applying a narrow patch.

### Metadata
- Reproducible: yes
- Related Files: README.md

---

## [ERR-20260816-010] powershell-rg-regex-escaping

**Logged**: 2026-08-16T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
A combined ripgrep regular expression was malformed after PowerShell processed its quote escapes.

### Error
```text
rg: regex parse error: unclosed group
```

### Context
- This was a post-change residual scan only; compilation and the other scan completed.

### Suggested Fix
Use separate fixed-string `rg -F` expressions for exact security-sensitive literals instead of composing an escaped alternation in PowerShell.

### Metadata
- Reproducible: yes
- Related Files: app.py, key_registry.py




### Resolution
- **Resolved**: 2026-08-16T00:00:00+08:00
- **Notes**: Packages were present; the failed import used a relative `PYTHONPATH`. An absolute workspace path imports FastAPI correctly.

---

## [ERR-20260816-006] fastapi-version-attribute-assumption

**Logged**: 2026-08-16T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
The isolated FastAPI import succeeded, but the smoke-check command incorrectly assumed that the package exports `fastapi.__version__`.

### Error
```text
AttributeError: module 'fastapi' has no attribute '__version__'
```

### Context
- The package had imported successfully from the isolated target directory.

### Suggested Fix
Use a direct import assertion rather than querying a non-guaranteed package attribute.

### Metadata
- Reproducible: yes
- Related Files: requirements.txt

---

## [ERR-20260816-007] isolated-fastapi-import-incomplete

**Logged**: 2026-08-16T00:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: tests

### Summary
The isolated API smoke test could import a `fastapi` module but it did not export `HTTPException`, indicating an incomplete or conflicted package installation.

### Error
```text
ImportError: cannot import name 'HTTPException' from 'fastapi' (unknown location)
```

### Context
- The test used the ignored `.pip-tmp` dependency directory.
- A prior import also showed no package version attribute.

### Suggested Fix
Inspect `.pip-tmp/fastapi` for its initializer and repair the isolated install before retrying endpoint tests.

### Metadata
- Reproducible: yes
- Related Files: .pip-tmp, requirements.txt

### Resolution
- **Resolved**: 2026-08-16T00:00:00+08:00
- **Notes**: The package was complete. It was unreadable from the sandbox because the dependency install ran with elevated permissions; the isolated API smoke test passed when run in that same scoped context.

---

## [ERR-20260817-001] comfyui-object-info-optional-inputs

**Logged**: 2026-08-17T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: backend

### Summary
The first read-only ComfyUI node-directory filter assumed every node exposed `input.required` and `input.optional` arrays.

### Error
```text
InvalidOperation: Cannot index into a null array.
```

### Context
- The remote ComfyUI node directory includes third-party nodes with partial or non-standard input schemas.
- The error occurred only while formatting inspection output; no remote state was changed.

### Suggested Fix
Filter class names first and serialize only explicitly requested fields with null-safe access.

### Metadata
- Reproducible: yes
- Related Files: ComfyUI `/object_info` inspection

### Resolution
- **Resolved**: 2026-08-17T00:00:00+08:00
- **Notes**: Subsequent queries use class-specific endpoints or null-safe projections.

---

## [ERR-20260817-002] apply-patch-duplicate-file-operation

**Logged**: 2026-08-17T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: frontend

### Summary
A patch attempted two separate update operations against the same HTML file.

### Error
```text
apply_patch verification failed: invalid patch: multiple operations target static/index.html
```

### Context
- The tool rejected the patch before applying any change.

### Suggested Fix
Combine all hunks for one file into a single update operation, then patch unrelated files separately.

### Metadata
- Reproducible: yes
- Related Files: static/index.html

### Resolution
- **Resolved**: 2026-08-17T00:00:00+08:00
- **Notes**: The frontend change was reapplied as one static/index.html update.

---

## [ERR-20260817-003] powershell-node-inline-escaping

**Logged**: 2026-08-17T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
PowerShell altered an inline Node.js regular expression used for a static JavaScript syntax check.

### Error
```text
SyntaxError: Invalid regular expression flags
```

### Context
- The error was in the test command argument, not in `static/index.html`.

### Suggested Fix
For PowerShell inline Node checks, locate script tags with string indexes instead of an escaped regular expression.

### Metadata
- Reproducible: yes
- Related Files: static/index.html

### Resolution
- **Resolved**: 2026-08-17T00:00:00+08:00
- **Notes**: The replacement test avoids regular-expression shell escaping.

---

## [ERR-20260817-004] local-preview-server-scope

**Logged**: 2026-08-17T00:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: tests

### Summary
The first local preview command attempted to expose the repository root through an HTTP server.

### Error
```text
Rejected: local HTTP service exposed the whole project directory, including possible config, keys, and storage data.
```

### Context
- This was for visual frontend inspection only.
- The server was not started.

### Suggested Fix
Serve only the public `static/` directory when a local visual preview is required.

### Metadata
- Reproducible: yes
- Related Files: static/index.html

### Resolution
- **Resolved**: 2026-08-17T00:00:00+08:00
- **Notes**: The preview uses a static-directory-only server on a separate localhost port.

---

## [ERR-20260817-005] browser-runtime-import-surface

**Logged**: 2026-08-17T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
Browser runtime initialization was first attempted in the orchestration surface, which does not permit module imports.

### Error
```text
unsupported import in exec
```

### Context
- The browser skill requires its runtime setup to run in the persistent Node kernel.

### Suggested Fix
Invoke the Node-kernel `js` tool from the orchestration call and place the runtime import there.

### Metadata
- Reproducible: yes
- Related Files: static/index.html

### Resolution
- **Resolved**: 2026-08-17T00:00:00+08:00
- **Notes**: Browser setup was moved to the Node-kernel tool as required by the skill.

---

## [ERR-20260817-006] responsive-probe-absent-gallery

**Logged**: 2026-08-17T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
The responsive layout probe assumed that a runtime-only gallery element already existed in the static login page.

### Error
```text
TypeError: getComputedStyle expects an Element
```

### Context
- The gallery is rendered only after a completed generation job.

### Suggested Fix
Probe only static elements or treat runtime-created elements as optional in visual smoke tests.

### Metadata
- Reproducible: yes
- Related Files: static/index.html

### Resolution
- **Resolved**: 2026-08-17T00:00:00+08:00
- **Notes**: The follow-up probe checks the static layout elements and verifies gallery CSS from the stylesheet separately.

---

## [ERR-20260817-007] remote-comfyui-browser-timeout

**Logged**: 2026-08-17T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
Opening the remote ComfyUI canvas with a full DOM snapshot exceeded the browser-kernel timeout.

### Error
```text
js execution timed out; kernel reset
```

### Context
- Target: the user-provided ComfyUI workflow URL with fragment ID `eb5b0bef-bd0d-4eb3-a650-6b99c7c508b1`.
- No page interaction or remote mutation was performed.

### Suggested Fix
Follow the browser recovery guidance and use a short page-state probe before requesting a full DOM snapshot.

### Metadata
- Reproducible: unknown
- Related Files: remote ComfyUI canvas

### Resolution
- **Resolved**: 2026-08-17T00:00:00+08:00
- **Notes**: The canvas loaded and confirmed the Stable Audio output history. Screenshot capture remained unavailable, so the verified ComfyTV configuration API was used to identify and update the binding.

---

## [ERR-20260818-001] local-python-missing-project-dependencies

**Logged**: 2026-08-18T00:00:00+08:00
**Priority**: low
**Status**: blocked
**Area**: tests

### Summary
An SSE regression check invoked the system Python directly, which does not have FastAPI installed.

### Error
```text
ModuleNotFoundError: No module named 'fastapi'
```

### Context
- The project starts with the bundled `.vendor` runtime when it is present.
- The failed command did not set `PYTHONPATH` to that directory.

### Suggested Fix
Repair the project dependency bundle before running API smoke tests: `.vendor` lacks `annotated_doc`, while `.pip-tmp` exposes an incomplete `fastapi` module. Until then, use static compilation and JavaScript syntax checks for this change.

### Metadata
- Reproducible: yes
- Related Files: start.bat, .vendor, .pip-tmp, requirements.txt
- See Also: ERR-20260816-003, ERR-20260816-007

### Resolution
- **Blocked**: 2026-08-18T00:00:00+08:00
- **Notes**: Both repository-provided dependency directories failed before the endpoint could be imported. The implementation still passed Python compilation, JavaScript syntax validation, and whitespace validation.
