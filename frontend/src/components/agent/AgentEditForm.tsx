/**
 * AgentEditForm — multi-section form for creating and editing an Agent.
 *
 * Used in two modes:
 *  - ``mode: "create"`` — empty form pre-filled from
 *    ``useAgentDefaults()``. On submit, calls ``useCreateAgent``; on
 *    success, calls ``onSuccess`` (if provided) or navigates to
 *    ``/agents/<new id>``.
 *  - ``mode: "edit"`` — form pre-filled from the ``agent`` prop. On
 *    submit, calls ``useUpdateAgent`` with the patch; on success,
 *    calls ``onSuccess`` (if provided).
 *
 * The ``name`` field is disabled in edit mode because the backend's
 * ``update_agent`` allowlist (see ``_allowed_patch_fields`` in
 * ``src/gilbert/core/services/agent.py``) does not include ``name``
 * — the slug is fixed at creation time.
 *
 * The "Dreaming" section is rendered fully disabled with a "Phase 7
 * — coming soon" tooltip; the values still round-trip so the form is
 * already wired when dreaming lands.
 */

import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  useAgentDefaults,
  useCreateAgent,
  useUpdateAgent,
  uploadAgentAvatar,
} from "@/api/agents";
import { useWsApi } from "@/hooks/useWsApi";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { AgentAvatar } from "./AgentAvatar";
import { ToolPicker } from "./ToolPicker";
import type {
  Agent,
  AgentCreatePayload,
  AgentDefaults,
  AgentUpdatePayload,
  AvatarKind,
} from "@/types/agent";

// ── Props ──────────────────────────────────────────────────────────

interface CreateProps {
  mode: "create";
  onSuccess?: (agent: Agent) => void;
}

interface EditProps {
  mode: "edit";
  agent: Agent;
  onSuccess?: (agent: Agent) => void;
}

type Props = CreateProps | EditProps;

// ── Form state ────────────────────────────────────────────────────

interface FormState {
  name: string;
  role_label: string;
  persona: string;
  system_prompt: string;
  procedural_rules: string;
  avatar_kind: AvatarKind;
  avatar_value: string;
  heartbeat_enabled: boolean;
  heartbeat_interval_s: number;
  heartbeat_checklist: string;
  dream_enabled: boolean;
  dream_quiet_hours: string;
  dream_probability: number;
  dream_max_per_night: number;
  profile_id: string;
  cost_cap_usd: string; // text — empty string => null
  tools_allowed: string[] | null;
}

const NAME_RE = /^[a-z0-9][a-z0-9-]*$/;

const SECONDS = 1;
const MINUTES = 60;
const HOURS = 3600;

type IntervalUnit = "seconds" | "minutes" | "hours";

const UNIT_FACTOR: Record<IntervalUnit, number> = {
  seconds: SECONDS,
  minutes: MINUTES,
  hours: HOURS,
};

function pickInitialUnit(seconds: number): IntervalUnit {
  if (seconds > 0 && seconds % HOURS === 0) return "hours";
  if (seconds > 0 && seconds % MINUTES === 0) return "minutes";
  return "seconds";
}

function emptyState(): FormState {
  return {
    name: "",
    role_label: "",
    persona: "",
    system_prompt: "",
    procedural_rules: "",
    avatar_kind: "emoji",
    avatar_value: "🤖",
    heartbeat_enabled: false,
    heartbeat_interval_s: 3600,
    heartbeat_checklist: "",
    dream_enabled: false,
    dream_quiet_hours: "",
    dream_probability: 0,
    dream_max_per_night: 0,
    profile_id: "",
    cost_cap_usd: "",
    tools_allowed: null,
  };
}

function stateFromDefaults(defaults: AgentDefaults): FormState {
  const base = emptyState();
  return {
    ...base,
    persona: defaults.default_persona ?? base.persona,
    system_prompt: defaults.default_system_prompt ?? base.system_prompt,
    procedural_rules:
      defaults.default_procedural_rules ?? base.procedural_rules,
    avatar_kind: defaults.default_avatar_kind ?? base.avatar_kind,
    avatar_value: defaults.default_avatar_value ?? base.avatar_value,
    heartbeat_interval_s:
      defaults.default_heartbeat_interval_s ?? base.heartbeat_interval_s,
    heartbeat_checklist:
      defaults.default_heartbeat_checklist ?? base.heartbeat_checklist,
    dream_enabled: defaults.default_dream_enabled ?? base.dream_enabled,
    dream_quiet_hours:
      defaults.default_dream_quiet_hours ?? base.dream_quiet_hours,
    dream_probability:
      defaults.default_dream_probability ?? base.dream_probability,
    dream_max_per_night:
      defaults.default_dream_max_per_night ?? base.dream_max_per_night,
    profile_id: defaults.default_profile_id ?? base.profile_id,
    tools_allowed:
      defaults.default_tools_allowed === undefined
        ? base.tools_allowed
        : defaults.default_tools_allowed,
  };
}

function stateFromAgent(agent: Agent): FormState {
  return {
    name: agent.name,
    role_label: agent.role_label,
    persona: agent.persona,
    system_prompt: agent.system_prompt,
    procedural_rules: agent.procedural_rules,
    avatar_kind: agent.avatar_kind,
    avatar_value: agent.avatar_value,
    heartbeat_enabled: agent.heartbeat_enabled,
    heartbeat_interval_s: agent.heartbeat_interval_s,
    heartbeat_checklist: agent.heartbeat_checklist,
    dream_enabled: agent.dream_enabled,
    dream_quiet_hours: agent.dream_quiet_hours,
    dream_probability: agent.dream_probability,
    dream_max_per_night: agent.dream_max_per_night,
    profile_id: agent.profile_id,
    cost_cap_usd:
      agent.cost_cap_usd === null || agent.cost_cap_usd === undefined
        ? ""
        : String(agent.cost_cap_usd),
    tools_allowed: agent.tools_allowed,
  };
}

// ── Validation ────────────────────────────────────────────────────

interface ValidationErrors {
  name?: string;
  cost_cap_usd?: string;
  dream_probability?: string;
  heartbeat_interval_s?: string;
}

function validate(state: FormState): ValidationErrors {
  const errors: ValidationErrors = {};
  if (!state.name) {
    errors.name = "Required.";
  } else if (!NAME_RE.test(state.name)) {
    errors.name =
      "Use lowercase letters, digits, and hyphens. Must start with a letter or digit.";
  }
  if (state.cost_cap_usd.trim() !== "") {
    const n = Number(state.cost_cap_usd);
    if (!Number.isFinite(n) || n <= 0) {
      errors.cost_cap_usd = "Must be a positive number, or leave blank.";
    }
  }
  if (
    state.dream_probability !== undefined &&
    state.dream_probability !== null
  ) {
    const p = Number(state.dream_probability);
    if (Number.isFinite(p) && (p < 0 || p > 1)) {
      errors.dream_probability = "Must be between 0 and 1.";
    }
  }
  if (state.heartbeat_interval_s < 60) {
    errors.heartbeat_interval_s = "Must be at least 60 seconds.";
  }
  return errors;
}

// ── Component ─────────────────────────────────────────────────────

export function AgentEditForm(props: Props) {
  const navigate = useNavigate();
  const api = useWsApi();
  const qc = useQueryClient();
  const defaultsQuery = useAgentDefaults();
  const createAgent = useCreateAgent();
  const updateAgent = useUpdateAgent();

  // AI profiles for the profile_id dropdown.
  const profilesQuery = useQuery({
    queryKey: ["ai-profiles"],
    queryFn: () => api.listAiProfiles(),
    staleTime: 60_000,
  });

  const initialAgent = props.mode === "edit" ? props.agent : null;
  const initialState = useMemo<FormState | null>(() => {
    if (initialAgent) return stateFromAgent(initialAgent);
    if (defaultsQuery.data) return stateFromDefaults(defaultsQuery.data);
    return null;
  }, [initialAgent, defaultsQuery.data]);

  const [state, setState] = useState<FormState | null>(initialState);
  const [intervalUnit, setIntervalUnit] = useState<IntervalUnit>(() =>
    pickInitialUnit(initialState?.heartbeat_interval_s ?? 3600),
  );
  const [avatarUploadError, setAvatarUploadError] = useState<string | null>(
    null,
  );
  const [avatarUploading, setAvatarUploading] = useState(false);

  // When defaults arrive after mount in create mode, hydrate state.
  useEffect(() => {
    if (state !== null) return;
    if (initialState !== null) {
      setState(initialState);
      setIntervalUnit(pickInitialUnit(initialState.heartbeat_interval_s));
    }
  }, [state, initialState]);

  const errors = useMemo<ValidationErrors>(
    () => (state ? validate(state) : {}),
    [state],
  );
  const hasErrors = Object.keys(errors).length > 0;

  const isPending = createAgent.isPending || updateAgent.isPending;

  // ── Loading / error states for the prefill data ──────────────────

  if (props.mode === "create" && defaultsQuery.isPending) {
    return <LoadingSpinner text="Loading defaults…" />;
  }

  if (props.mode === "create" && defaultsQuery.isError) {
    return (
      <div
        role="alert"
        className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
      >
        Failed to load agent defaults.
      </div>
    );
  }

  if (state === null) {
    return <LoadingSpinner text="Loading…" />;
  }

  // ── Helpers ──────────────────────────────────────────────────────

  const update = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setState((s) => (s ? { ...s, [key]: value } : s));
  };

  const handleIntervalChange = (rawValue: string, unit: IntervalUnit) => {
    const n = Number(rawValue);
    if (!Number.isFinite(n)) return;
    update("heartbeat_interval_s", Math.round(n * UNIT_FACTOR[unit]));
  };

  const handleUnitChange = (unit: IntervalUnit) => {
    setIntervalUnit(unit);
  };

  const intervalDisplayValue = (() => {
    const factor = UNIT_FACTOR[intervalUnit];
    const v = state.heartbeat_interval_s / factor;
    // Show whole numbers cleanly.
    if (Number.isInteger(v)) return String(v);
    return v.toFixed(2);
  })();

  const handleAvatarUpload = async (file: File) => {
    if (props.mode !== "edit") return;
    setAvatarUploadError(null);
    setAvatarUploading(true);
    try {
      const updated = await uploadAgentAvatar(props.agent._id, file);
      // Reflect the new avatar in local state so it renders immediately.
      update("avatar_kind", updated.avatar_kind);
      update("avatar_value", updated.avatar_value);
      // Refresh global cache so other consumers (detail / list pages) see it.
      qc.setQueryData(["agents", "detail", props.agent._id], updated);
      qc.invalidateQueries({
        queryKey: ["agents", "detail", props.agent._id],
      });
      qc.invalidateQueries({ queryKey: ["agents", "list"] });
    } catch (e) {
      setAvatarUploadError(
        e instanceof Error ? e.message : "Upload failed.",
      );
    } finally {
      setAvatarUploading(false);
    }
  };

  // ── Submit handlers ─────────────────────────────────────────────

  const handleCreateSubmit = async () => {
    if (!state) return;
    const payload: AgentCreatePayload = {
      name: state.name,
      role_label: state.role_label,
      persona: state.persona,
      system_prompt: state.system_prompt,
      procedural_rules: state.procedural_rules,
      avatar_kind: state.avatar_kind,
      avatar_value: state.avatar_value,
      heartbeat_enabled: state.heartbeat_enabled,
      heartbeat_interval_s: state.heartbeat_interval_s,
      heartbeat_checklist: state.heartbeat_checklist,
      dream_enabled: state.dream_enabled,
      dream_quiet_hours: state.dream_quiet_hours,
      dream_probability: state.dream_probability,
      dream_max_per_night: state.dream_max_per_night,
      profile_id: state.profile_id,
      cost_cap_usd:
        state.cost_cap_usd.trim() === "" ? null : Number(state.cost_cap_usd),
      tools_allowed: state.tools_allowed,
    };
    const agent = await createAgent.mutateAsync(payload);
    if (props.mode === "create" && props.onSuccess) {
      props.onSuccess(agent);
    } else {
      navigate(`/agents/${agent._id}`);
    }
  };

  const handleEditSubmit = async () => {
    if (!state || props.mode !== "edit") return;
    // Build a patch with only the fields the backend allows. ``name`` is
    // intentionally excluded — see ``_allowed_patch_fields``.
    const patch: AgentUpdatePayload = {
      role_label: state.role_label,
      persona: state.persona,
      system_prompt: state.system_prompt,
      procedural_rules: state.procedural_rules,
      avatar_kind: state.avatar_kind,
      avatar_value: state.avatar_value,
      heartbeat_enabled: state.heartbeat_enabled,
      heartbeat_interval_s: state.heartbeat_interval_s,
      heartbeat_checklist: state.heartbeat_checklist,
      dream_enabled: state.dream_enabled,
      dream_quiet_hours: state.dream_quiet_hours,
      dream_probability: state.dream_probability,
      dream_max_per_night: state.dream_max_per_night,
      profile_id: state.profile_id,
      cost_cap_usd:
        state.cost_cap_usd.trim() === "" ? null : Number(state.cost_cap_usd),
      tools_allowed: state.tools_allowed,
    };
    const agent = await updateAgent.mutateAsync({
      agentId: props.agent._id,
      patch,
    });
    if (props.onSuccess) {
      props.onSuccess(agent);
    }
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (hasErrors || isPending) return;
    if (props.mode === "create") {
      void handleCreateSubmit();
    } else {
      void handleEditSubmit();
    }
  };

  const handleCancel = () => {
    if (props.mode === "create") {
      navigate("/agents");
    } else {
      // Reset to the loaded agent state.
      const reset = stateFromAgent(props.agent);
      setState(reset);
      setIntervalUnit(pickInitialUnit(reset.heartbeat_interval_s));
      setAvatarUploadError(null);
    }
  };

  const submissionError =
    (createAgent.isError && createAgent.error) ||
    (updateAgent.isError && updateAgent.error) ||
    null;

  // For the avatar preview ``<AgentAvatar />`` we need a Pick of Agent.
  // In create mode we pass a synthesized id so the image-kind branch
  // can build a URL — it's never used because the user can't upload
  // until after creation.
  const avatarPreviewAgent = {
    _id: props.mode === "edit" ? props.agent._id : "preview",
    avatar_kind: state.avatar_kind,
    avatar_value: state.avatar_value,
  };

  // ── Render ──────────────────────────────────────────────────────

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      {/* ── Identity ────────────────────────────────────────────── */}
      <details open className="rounded-md border">
        <summary className="cursor-pointer select-none px-3 py-2 text-sm font-semibold">
          Identity
        </summary>
        <section className="space-y-3 px-3 pb-3">
          <div className="space-y-1">
            <Label htmlFor="agent-name">Name (slug)</Label>
            <Input
              id="agent-name"
              value={state.name}
              onChange={(e) => update("name", e.target.value)}
              disabled={props.mode === "edit"}
              placeholder="e.g. concierge-bot"
              aria-invalid={errors.name ? true : undefined}
            />
            {errors.name && (
              <p className="text-xs text-destructive">{errors.name}</p>
            )}
            {props.mode === "edit" && (
              <p className="text-xs text-muted-foreground">
                Names cannot be changed after creation.
              </p>
            )}
          </div>

          <div className="space-y-1">
            <Label htmlFor="agent-role">Role label</Label>
            <Input
              id="agent-role"
              value={state.role_label}
              onChange={(e) => update("role_label", e.target.value)}
              placeholder="e.g. Concierge"
            />
          </div>

          <div className="space-y-2">
            <Label>Avatar</Label>
            <div className="flex items-start gap-3">
              <AgentAvatar agent={avatarPreviewAgent} size="lg" />
              <div className="flex-1 space-y-2">
                <div className="flex flex-wrap gap-3 text-sm">
                  <label className="flex items-center gap-1">
                    <input
                      type="radio"
                      name="avatar_kind"
                      value="emoji"
                      checked={state.avatar_kind === "emoji"}
                      onChange={() => update("avatar_kind", "emoji")}
                    />
                    Emoji
                  </label>
                  <label className="flex items-center gap-1">
                    <input
                      type="radio"
                      name="avatar_kind"
                      value="icon"
                      checked={state.avatar_kind === "icon"}
                      onChange={() => update("avatar_kind", "icon")}
                    />
                    Icon name
                  </label>
                  <label className="flex items-center gap-1">
                    <input
                      type="radio"
                      name="avatar_kind"
                      value="image"
                      checked={state.avatar_kind === "image"}
                      onChange={() => update("avatar_kind", "image")}
                    />
                    Upload image
                  </label>
                </div>

                {state.avatar_kind !== "image" && (
                  <Input
                    value={state.avatar_value}
                    onChange={(e) => update("avatar_value", e.target.value)}
                    placeholder={
                      state.avatar_kind === "emoji"
                        ? "🤖"
                        : "lucide icon name (e.g. bot)"
                    }
                  />
                )}

                {state.avatar_kind === "image" && (
                  <div className="space-y-1">
                    {props.mode === "edit" ? (
                      <input
                        type="file"
                        accept="image/png,image/jpeg,image/webp,image/gif"
                        disabled={avatarUploading}
                        onChange={(e) => {
                          const file = e.target.files?.[0];
                          if (file) void handleAvatarUpload(file);
                        }}
                        className="text-sm"
                      />
                    ) : (
                      <p
                        className="text-xs text-muted-foreground"
                        title="Upload available after creating the agent."
                      >
                        Upload available after creating the agent.
                      </p>
                    )}
                    {avatarUploading && (
                      <p className="text-xs text-muted-foreground">
                        Uploading…
                      </p>
                    )}
                    {avatarUploadError && (
                      <p className="text-xs text-destructive">
                        {avatarUploadError}
                      </p>
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>
        </section>
      </details>

      {/* ── Persona ─────────────────────────────────────────────── */}
      <details open className="rounded-md border">
        <summary className="cursor-pointer select-none px-3 py-2 text-sm font-semibold">
          Persona
        </summary>
        <section className="space-y-3 px-3 pb-3">
          <PersonaField
            id="agent-persona"
            label="Persona"
            value={state.persona}
            onChange={(v) => update("persona", v)}
            settingsHref="/settings#agent.default_persona"
          />
          <PersonaField
            id="agent-system-prompt"
            label="System prompt"
            value={state.system_prompt}
            onChange={(v) => update("system_prompt", v)}
            settingsHref="/settings#agent.default_system_prompt"
          />
          <PersonaField
            id="agent-procedural-rules"
            label="Procedural rules"
            value={state.procedural_rules}
            onChange={(v) => update("procedural_rules", v)}
            settingsHref="/settings#agent.default_procedural_rules"
          />
        </section>
      </details>

      {/* ── Heartbeat ───────────────────────────────────────────── */}
      <details open className="rounded-md border">
        <summary className="cursor-pointer select-none px-3 py-2 text-sm font-semibold">
          Heartbeat
        </summary>
        <section className="space-y-3 px-3 pb-3">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={state.heartbeat_enabled}
              onChange={(e) => update("heartbeat_enabled", e.target.checked)}
            />
            <span>Heartbeat enabled</span>
          </label>

          <div className="space-y-1">
            <Label htmlFor="agent-heartbeat-interval">Interval</Label>
            <div className="flex items-center gap-2">
              <Input
                id="agent-heartbeat-interval"
                type="number"
                min={1}
                step="any"
                className="w-32"
                value={intervalDisplayValue}
                onChange={(e) =>
                  handleIntervalChange(e.target.value, intervalUnit)
                }
                aria-invalid={
                  errors.heartbeat_interval_s ? true : undefined
                }
              />
              <select
                value={intervalUnit}
                onChange={(e) =>
                  handleUnitChange(e.target.value as IntervalUnit)
                }
                className="h-8 rounded-lg border border-input bg-transparent px-2 text-sm"
              >
                <option value="seconds">seconds</option>
                <option value="minutes">minutes</option>
                <option value="hours">hours</option>
              </select>
            </div>
            <p className="text-xs text-muted-foreground">
              Stored as {state.heartbeat_interval_s} seconds.
            </p>
            {errors.heartbeat_interval_s && (
              <p className="text-xs text-destructive">
                {errors.heartbeat_interval_s}
              </p>
            )}
          </div>

          <div className="space-y-1">
            <Label htmlFor="agent-heartbeat-checklist">
              Heartbeat checklist
            </Label>
            <Textarea
              id="agent-heartbeat-checklist"
              value={state.heartbeat_checklist}
              onChange={(e) => update("heartbeat_checklist", e.target.value)}
              rows={4}
            />
          </div>
        </section>
      </details>

      {/* ── Dreaming (Phase 7 — disabled) ────────────────────────── */}
      <details open className="rounded-md border">
        <summary className="cursor-pointer select-none px-3 py-2 text-sm font-semibold">
          Dreaming{" "}
          <span className="text-xs font-normal text-muted-foreground">
            (Phase 7 — coming soon)
          </span>
        </summary>
        <section
          className="space-y-3 px-3 pb-3"
          title="Phase 7 — coming soon"
        >
          <span id="dream-phase-note" className="sr-only">
            Dreaming launches in Phase 7 — these settings are saved but not yet
            active.
          </span>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={state.dream_enabled}
              onChange={(e) => update("dream_enabled", e.target.checked)}
              disabled
              aria-describedby="dream-phase-note"
            />
            <span>Dreaming enabled</span>
          </label>

          <div className="space-y-1">
            <Label htmlFor="agent-dream-quiet">Quiet hours</Label>
            <Input
              id="agent-dream-quiet"
              value={state.dream_quiet_hours}
              onChange={(e) => update("dream_quiet_hours", e.target.value)}
              placeholder="22:00-06:00"
              disabled
              aria-describedby="dream-phase-note"
            />
          </div>

          <div className="space-y-1">
            <Label htmlFor="agent-dream-prob">
              Dream probability (0–1)
            </Label>
            <Input
              id="agent-dream-prob"
              type="number"
              min={0}
              max={1}
              step="0.01"
              value={state.dream_probability}
              onChange={(e) =>
                update("dream_probability", Number(e.target.value))
              }
              disabled
              aria-describedby="dream-phase-note"
            />
            {errors.dream_probability && (
              <p className="text-xs text-destructive">
                {errors.dream_probability}
              </p>
            )}
          </div>

          <div className="space-y-1">
            <Label htmlFor="agent-dream-max">Max dreams per night</Label>
            <Input
              id="agent-dream-max"
              type="number"
              min={0}
              step="1"
              value={state.dream_max_per_night}
              onChange={(e) =>
                update("dream_max_per_night", Number(e.target.value))
              }
              disabled
              aria-describedby="dream-phase-note"
            />
          </div>
        </section>
      </details>

      {/* ── Profile & cost ──────────────────────────────────────── */}
      <details open className="rounded-md border">
        <summary className="cursor-pointer select-none px-3 py-2 text-sm font-semibold">
          Profile & cost
        </summary>
        <section className="space-y-3 px-3 pb-3">
          <div className="space-y-1">
            <Label htmlFor="agent-profile">AI profile</Label>
            <select
              id="agent-profile"
              value={state.profile_id}
              onChange={(e) => update("profile_id", e.target.value)}
              className="h-8 w-full rounded-lg border border-input bg-transparent px-2 text-sm"
            >
              <option value="">(use default)</option>
              {profilesQuery.data?.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name}
                  {p.description ? ` — ${p.description}` : ""}
                </option>
              ))}
            </select>
          </div>

          <div className="space-y-1">
            <Label htmlFor="agent-cost-cap">Cost cap (USD)</Label>
            <Input
              id="agent-cost-cap"
              type="number"
              min={0}
              step="0.01"
              value={state.cost_cap_usd}
              onChange={(e) => update("cost_cap_usd", e.target.value)}
              placeholder="(no cap)"
              aria-invalid={errors.cost_cap_usd ? true : undefined}
            />
            {errors.cost_cap_usd && (
              <p className="text-xs text-destructive">
                {errors.cost_cap_usd}
              </p>
            )}
          </div>
        </section>
      </details>

      {/* ── Tools ───────────────────────────────────────────────── */}
      <details open className="rounded-md border">
        <summary className="cursor-pointer select-none px-3 py-2 text-sm font-semibold">
          Tools
        </summary>
        <section className="space-y-3 px-3 pb-3">
          <ToolPicker
            value={state.tools_allowed}
            onChange={(next) => update("tools_allowed", next)}
          />
        </section>
      </details>

      {/* ── Submission ──────────────────────────────────────────── */}
      {submissionError && (
        <div
          role="alert"
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          {submissionError instanceof Error
            ? submissionError.message
            : "Failed to save."}
        </div>
      )}

      <div className="flex items-center gap-2">
        <Button
          type="submit"
          disabled={hasErrors || isPending}
        >
          {isPending
            ? props.mode === "create"
              ? "Creating…"
              : "Saving…"
            : props.mode === "create"
              ? "Create agent"
              : "Save changes"}
        </Button>
        <Button
          type="button"
          variant="outline"
          onClick={handleCancel}
          disabled={isPending}
        >
          Cancel
        </Button>
      </div>
    </form>
  );
}

// ── Sub-components ────────────────────────────────────────────────

interface PersonaFieldProps {
  id: string;
  label: string;
  value: string;
  onChange: (next: string) => void;
  settingsHref: string;
}

function PersonaField({
  id,
  label,
  value,
  onChange,
  settingsHref,
}: PersonaFieldProps) {
  return (
    <div className="space-y-1">
      <Label htmlFor={id}>{label}</Label>
      <Textarea
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        rows={6}
      />
      <a
        href={settingsHref}
        className="text-xs text-muted-foreground hover:underline"
      >
        ↗ open in Settings
      </a>
    </div>
  );
}
