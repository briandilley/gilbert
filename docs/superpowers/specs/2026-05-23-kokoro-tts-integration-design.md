# Kokoro Local TTS Integration

**Status:** Approved design, ready for implementation plan
**Date:** 2026-05-23
**Author:** Brian Dilley (with Claude)

## Context

Gilbert today has only one TTS backend: ElevenLabs (cloud, paid). Local STT is already covered by `LocalWhisperBackend` (faster-whisper, in-process, bundled in `integrations/`) plus cloud STT plugins (OpenAI Whisper, Groq Whisper, Deepgram, ElevenLabs Scribe).

The motivating reference is [mbailey/voicemode](https://github.com/mbailey/voicemode), which runs Kokoro and whisper.cpp as subprocess HTTP servers exposing OpenAI-compatible APIs. Per the user's project convention (`reference codebase is for concepts only`), this spec adopts the *idea* of a local-first voice path but uses Gilbert's own in-process backend pattern instead of voicemode's HTTP-subprocess architecture.

**Scope of this spec:** add a single new TTS backend — Kokoro — as a Gilbert std-plugin. Whisper is unchanged.

## Goals

- Add a local, open-weights TTS option to Gilbert so the `audio_output` tool and any other `TTSProvider` consumer can synthesize without a cloud round-trip or API key.
- Keep the heavy dependencies (PyTorch, the 327 MB Kokoro-82M model) opt-in so users who don't enable the plugin don't pay for them.
- Conform to existing patterns: standard `TTSBackend` ABC, side-effect plugin registration, `runtime_dependencies()` health probe, full `list_voices()` catalog with labels for filtering.

## Non-Goals

- **No subprocess HTTP server orchestration.** Voicemode's pattern is rejected here. The plugin runs Kokoro in-process via the `kokoro` Python package.
- **No streaming TTS.** The existing `TTSBackend.synthesize` is one-shot. Adding a streaming protocol would be a separate spec.
- **No `AICapableTTSBackend` (audio-tag injection).** Kokoro has no native analog to ElevenLabs v3 audio tags. Adding LLM-driven expressive markup for Kokoro is a separate feature.
- **No changes to `LocalWhisperBackend` or any existing STT backend.** Whisper is already integrated and works.
- **No swap of `audio_output` default format.** It will keep requesting MP3; the plugin must produce MP3.

## Architecture

### Plugin layout

```
std-plugins/kokoro/
    plugin.yaml              # name: kokoro, provides: [kokoro_tts]
    plugin.py                # KokoroPlugin: setup() imports kokoro_tts; runtime_dependencies()
    pyproject.toml           # deps: kokoro>=0.9, torch>=2.4, av>=12, soundfile>=0.12
    __init__.py
    kokoro_tts.py            # KokoroTTSBackend(TTSBackend), backend_name="kokoro"
    tests/
        conftest.py          # standard gilbert_plugin_kokoro shim
        test_kokoro_tts.py   # unit tests with mocked KPipeline; one @pytest.mark.slow integration test
```

Plugin is **default-disabled**, matching the recent project policy (commit `4d0f9e5`). Heavy deps (`torch`, `kokoro`) are declared in the plugin's own `pyproject.toml`, so `uv sync` only resolves them when the plugin is enabled in `gilbert.yaml`.

### `KokoroTTSBackend(TTSBackend)`

Subclasses `gilbert.interfaces.tts.TTSBackend`. `backend_name = "kokoro"` triggers `__init_subclass__` registration. Implements every abstract method on the ABC: `initialize`, `close`, `synthesize`, `list_voices`, `get_voice`.

#### Configuration

`backend_config_params()` returns four `ConfigParam` entries:

| Key             | Type    | Default     | `restart_required` | Notes |
|-----------------|---------|-------------|--------------------|-------|
| `device`        | string  | `cpu`       | yes                | choices: `cpu`, `cuda`, `mps`, `auto` |
| `default_voice` | string  | `af_heart`  | no                 | `choices_from="kokoro.voices"` populated from `list_voices()` (dropdown UX per `feedback_dropdowns_for_known_choices` memory) |
| `speed`         | number  | `1.0`       | no                 | inference speed multiplier, recommended range 0.5–2.0 |
| `preload`       | boolean | `false`     | yes                | when true, build the default-language `KPipeline` in `initialize()`; otherwise lazy on first `synthesize()` |

No `ai_prompt` config — Kokoro is not prompt-driven.

#### Lifecycle

- **`initialize(config)`** — store device, default_voice, speed, preload. If `preload=True`, instantiate `KPipeline(lang_code=<first char of default_voice>)` now and cache it in `self._pipelines[lang_code]`. Otherwise stay cold.
- **`synthesize(request)`** — resolve voice → language code from the first character of `voice_id` (see Voice catalog below). Get-or-create the `KPipeline` for that language and cache. Run inference in `loop.run_in_executor` (kokoro is sync/blocking). Concatenate the 24 kHz float32 chunks the pipeline yields. Encode to `request.output_format` via PyAV. Return `SynthesisResult(audio=..., format=request.output_format, duration_seconds=..., characters_used=len(request.text))`.
- **`close()`** — drop `self._pipelines`, allow GC to release torch memory.

#### Voice catalog

`list_voices()` returns the static Kokoro v1.0 catalog (~54 voices) as `Voice` dataclasses. Each entry carries `labels` with:
- `language` — e.g. `en-US`, `en-GB`, `ja`, `zh`, `es`, `fr`, `hi`, `it`, `pt`
- `region` — e.g. `American`, `British`
- `gender` — `female` / `male`

so the Settings dropdown can be filterable. Voice ID first character → language pipeline:

| Prefix | Lang code | Language |
|--------|-----------|----------|
| `a`    | `a`       | American English |
| `b`    | `b`       | British English  |
| `j`    | `j`       | Japanese         |
| `z`    | `z`       | Mandarin Chinese |
| `e`    | `e`       | Spanish          |
| `f`    | `f`       | French           |
| `h`    | `h`       | Hindi            |
| `i`    | `i`       | Italian          |
| `p`    | `p`       | Portuguese       |

The catalog lives in a single `_VOICES: list[Voice]` constant inside `kokoro_tts.py`, derived once from the kokoro package's voice manifest (not loaded at import time; defined statically so `list_voices()` works without the model loaded).

`get_voice(voice_id)` is a dict lookup against the catalog; returns `None` for unknown IDs (per the ABC contract).

#### Format encoding

A single helper `_encode(samples: np.ndarray, fmt: AudioFormat) -> bytes` handles all output formats via PyAV (the chosen encoder, see "MP3 encoding" decision below):

- `MP3` — libmp3lame container, 128 kbps, 44.1 kHz mono int16 (after resample from 24 kHz).
- `WAV` — PCM 16 bit LE, 44.1 kHz mono.
- `OGG` — libvorbis, 44.1 kHz mono.
- `PCM` — raw 16-bit LE bytes at 44.1 kHz mono.

All outputs are resampled to **44.1 kHz** to match the silence-padding constants in `interfaces/tts.py` (`_PCM_SAMPLE_RATE = 44100`). Not exposing Kokoro's native 24 kHz keeps callers simple and consistent with ElevenLabs output.

#### Unknown-voice handling

If `request.voice_id` is not in the catalog, `synthesize()` raises `ValueError("Unknown Kokoro voice: <id>")` rather than a bare `KeyError`. This matches the explicitness pattern in other backends.

### Plugin metadata and `runtime_dependencies()`

`plugin.py` exposes the standard `KokoroPlugin(Plugin)`:

```python
class KokoroPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="kokoro",
            version="1.0.0",
            description="Kokoro local TTS backend (open-weights, in-process).",
            provides=["kokoro_tts"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import kokoro_tts  # noqa: F401 — triggers backend registration

    async def teardown(self) -> None:
        pass

    def runtime_dependencies(self) -> list[RuntimeDependency]:
        return [
            RuntimeDependency(
                name="kokoro-stack",
                description="torch + kokoro + PyAV must import and synthesize a tiny clip.",
                check_cmd=(
                    'python -c "'
                    "import av, kokoro; "
                    "p = kokoro.KPipeline(lang_code='a'); "
                    "list(p('hi', voice='af_heart'))"
                    '"'
                ),
                install_hint="Enable the kokoro plugin so uv sync installs torch + kokoro + av",
                auto_install_cmd="",
            ),
        ]
```

The probe does a real end-to-end synth of `"hi"` so a torch-installed-but-libgomp-missing setup fails loudly with a clear hint, not on the first user TTS request. The one-liner imports `av` and `kokoro`, builds a `KPipeline`, exhausts it for one phoneme, and exits 0 on success (non-zero on any import or runtime failure).

## Data Flow

```
audio_output tool
   │
   ▼
TTSService.synthesize(request)        # backend = "kokoro" (configured)
   │
   ▼
KokoroTTSBackend.synthesize(request)
   │
   ├─► lang = request.voice_id[0]
   ├─► pipeline = self._pipelines.setdefault(lang, KPipeline(lang_code=lang))
   ├─► loop.run_in_executor(None, pipeline, text, voice=voice_id, speed=...)
   │       └─► yields list of float32 24 kHz numpy chunks
   ├─► samples = np.concatenate(chunks)
   ├─► audio_bytes = _encode(samples, request.output_format)
   └─► return SynthesisResult(audio=audio_bytes, format=..., duration_seconds=...)
```

## Testing

**Unit tests** (`test_kokoro_tts.py`, default-on)
- `KPipeline` is mocked to yield deterministic float32 chunks.
- `synthesize` honors each `AudioFormat` (MP3 / WAV / PCM / OGG): assert magic bytes for MP3/WAV/OGG, expected length for PCM.
- Voice-ID → language-pipeline routing: `af_*` builds `lang_code="a"`, `jf_*` builds `"j"`, `zm_*` builds `"z"`. Calls cache and re-use.
- `list_voices()` returns the catalog; every entry has the three labels populated.
- `get_voice("af_heart")` returns the entry; `get_voice("nope")` returns `None`.
- `synthesize` with an unknown voice raises `ValueError`, not `KeyError`.
- `close()` clears `self._pipelines` (verify post-close `synthesize` rebuilds).

**Slow integration test** (`@pytest.mark.slow`, opt-in, skipped in default CI)
- Loads the real Kokoro model on CPU, synthesizes `"Hello."`, asserts ~500 ms of audio comes back at 44.1 kHz.

No fakes for the TTS service itself — tests exercise the backend in isolation, per the existing pattern in `local_whisper`/`elevenlabs` tests.

## Documentation

Per the project's hard rule on README freshness:

- **`std-plugins/README.md`** — add a row to the plugin table and a per-plugin detail section under `## Available plugins` covering: provides `kokoro_tts`, deps (`kokoro`, `torch`, `av`, `soundfile`), config keys (`device`, `default_voice`, `speed`, `preload`), `runtime_dependencies()` probe behavior, default-disabled flag.
- **Root `README.md`** — if it enumerates TTS backends or plugin counts, add Kokoro there too.
- No new `docs/architecture/` entry. The plugin is straightforward enough that the README section + this spec are sufficient.

## Validation Against Project Rules

This design passes the `validate-architecture` checks:

- **Layer imports** — Plugin imports only from `gilbert.interfaces.*` (TTSBackend, ConfigParam, RuntimeDependency, Plugin). ✓
- **Backend registry** — `backend_name = "kokoro"` set on the subclass; `__init_subclass__` registers it; plugin `setup()` triggers the side-effect import. ✓
- **No business logic in routes** — N/A. ✓
- **Configurable AI prompts** — N/A (no AI prompts). ✓
- **RBAC** — N/A. TTS is invoked via the existing `audio_output` tool, whose `required_role` is already set. ✓
- **Multi-user isolation** — N/A (no per-user state in the backend). ✓
- **Plugin shape** — std-plugin, vendor-free, `pyproject.toml` present, side-effect setup, `runtime_dependencies()` declared with a real-execution probe. ✓
- **README freshness** — covered in the Documentation section. ✓

## Key Decisions (resolved during brainstorming)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Goal scope | Add Kokoro local TTS only (in-process). Whisper unchanged. | Smallest useful gap; Whisper already works. |
| Placement | std-plugin `kokoro`, default-disabled | Heavy deps (torch ~700 MB, model ~327 MB) stay opt-in. Matches recent default-disabled policy. |
| MP3 encoder | `pyav` (PyAV) | Self-contained ffmpeg wheels (no system ffmpeg), covers MP3 / OGG / future formats with one dep. |
| Output sample rate | Resample to 44.1 kHz | Match `interfaces/tts.py` constants and ElevenLabs output. |
| Streaming | Out of scope | One-shot synthesis fits the existing ABC; streaming is a separate spec. |
| Audio tags / `AICapableTTSBackend` | Out of scope | Kokoro has no native equivalent. |
| Subprocess HTTP server | Out of scope | Voicemode's pattern rejected; in-process is the Gilbert pattern. |

## Open Questions

None remaining at design time. Implementation plan can proceed.
