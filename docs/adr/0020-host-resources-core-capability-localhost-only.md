# Host hardware is a core capability (data); runnability is consumer policy; localhost-only

Host resources — total/available RAM, GPU presence, per-GPU VRAM — are exposed through a
`HostResourcesProvider` capability (`interfaces/`) backed by a **vendor-free** `integrations/` probe
(`psutil` for memory + best-effort GPU detection via system tools, no heavy deps), rather than a
plugin-local probe or the `doctor` machinery. The capability returns **raw data**; turning it into a
*runnability verdict* (size × overhead vs RAM/VRAM → fits-VRAM / fits-RAM / won't-fit) is **policy**
that lives in the consuming local-model manager, not in core. The probe is **localhost-only** and
best-effort: it describes the Gilbert host, which equals the model-serving host only when the runtime
is local — a remote Ollama yields *unknown* fit, never a wrong answer.

## Considered options

- **Plugin-local probe** — rejected: not reusable. `whisper`/`kokoro`'s `device=auto` and any future
  local-compute backend want the same "do I have a GPU, how much VRAM" data; a capability lets them
  all consume it.
- **`doctor`-based** — rejected: `doctor` is on-demand PASS/FAIL diagnostics, not a live structured
  data source a UI queries per-model. `doctor` keeps the *daemon-reachable* check; it is the wrong
  shape for the fit filter.

## Consequences

- New core dependency: `psutil` (small, cross-platform). GPU detection is best-effort and may report
  *unknown* on exotic setups (non-NVIDIA, containers without device passthrough) — the fit filter
  must render *unknown* as "can't tell," never as "won't fit."
- Keeping the verdict out of core means the heuristic (overhead factor, fast/slow thresholds) can
  evolve in the plugin without an interface change.
