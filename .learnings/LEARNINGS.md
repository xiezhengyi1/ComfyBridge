## [LRN-20260817-001] correction

**Logged**: 2026-08-17T00:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: config

### Summary
The user clarified that the text-to-audio workflow must remain bound to T2A, but the previously selected internal target node was wrong.

### Details
Before changing a ComfyTV workflow binding, inspect the current workflow graph and its existing bindings. Change only the prompt-input binding after matching it to the T2A user-input node; leave sampler, duration, and other settings unchanged.

### Suggested Action
Use the read-only workflow-config endpoint first, then make a narrowly scoped binding update and verify it with a second read.

### Metadata
- Source: user_feedback
- Related Files: workflows/comfytv_t2a.manifest.json
- Tags: comfytv, t2a, binding

### Resolution
- **Resolved**: 2026-08-17T00:00:00+08:00
- **Notes**: The current graph will be inspected before the targeted rebind.

---

## [LRN-20260817-002] correction

**Logged**: 2026-08-17T00:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: config

### Summary
The “文生音频” reference is a ComfyUI canvas workflow ID in the URL fragment, not a ComfyTV workflow label.

### Details
Do not derive a ComfyTV config lookup label from a user-facing canvas title. Resolve the workflow ID from the supplied ComfyUI URL and inspect that graph before selecting a binding target.

### Suggested Action
Locate the server-side endpoint or client state that resolves workflow ID `eb5b0bef-bd0d-4eb3-a650-6b99c7c508b1`, then perform a narrow verified update.

### Metadata
- Source: user_feedback
- Related Files: workflows/comfytv_t2a.manifest.json
- Tags: comfyui, workflow-id, t2a
- See Also: LRN-20260817-001

### Resolution
- **Resolved**: 2026-08-17T00:00:00+08:00
- **Notes**: The canvas was confirmed as the Stable Audio T2A graph. Its persisted ComfyTV record is workflow ID 55; `main_prompt` was re-bound to the correct `PrimitiveStringMultiline` node `68.value` and verified by readback.
