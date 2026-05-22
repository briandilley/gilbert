/**
 * SettingsContext — shared dirty-state for every ConfigSection on
 * the page, so the top-of-page StatusBar can show an aggregate
 * "N unsaved across M services" line and the user can save (or
 * discard) every pending change at once.
 *
 * Two contexts on purpose (state + api). Pages mutate via the
 * stable api; the state context is consumed only where it's read.
 * This keeps consumers of just the api from re-rendering on every
 * field edit.
 */

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";

interface NamespaceSaveStatus {
  /** What to surface in the section's footer line. */
  message: string;
  /** ``true`` = success-tone, ``false`` = error-tone. */
  ok: boolean;
  /** Wall-clock ms when this was set, so the auto-clear can age it. */
  at: number;
}

interface State {
  /** Dirty edits, keyed by ``namespace`` then by config-param ``key``.
   *  The empty inner object means "no edits in this section." */
  dirty: Record<string, Record<string, unknown>>;
  /** Most recent save outcome per namespace. */
  saveStatus: Record<string, NamespaceSaveStatus | null>;
}

interface Api {
  /** Set a single field. Empty / undefined value still tracks as a
   *  dirty edit. */
  setField: (namespace: string, key: string, value: unknown) => void;
  /** Bulk write (used by ``persist``-bearing config actions). */
  setFields: (namespace: string, values: Record<string, unknown>) => void;
  /** Discard pending edits for one namespace. */
  discard: (namespace: string) => void;
  /** Discard pending edits across every namespace. */
  discardAll: () => void;
  /** Save one namespace's pending edits via setConfigSection RPC.
   *  Returns the result for callers that want the restart hint. */
  saveNamespace: (namespace: string) => Promise<unknown>;
  /** Save every dirty namespace in parallel. */
  saveAll: () => Promise<void>;
  /** Reset one namespace to defaults via resetConfigSection RPC.
   *  Discards local edits as a side effect. */
  resetToDefaults: (namespace: string) => Promise<void>;
  /** Record a per-namespace save-status message (used by the
   *  per-section footer text). Auto-clears after ~3s. */
  setSaveStatus: (namespace: string, message: string, ok: boolean) => void;
}

const StateContext = createContext<State>({
  dirty: {},
  saveStatus: {},
});

const ApiContext = createContext<Api>({
  setField: () => {},
  setFields: () => {},
  discard: () => {},
  discardAll: () => {},
  saveNamespace: async () => undefined,
  saveAll: async () => {},
  resetToDefaults: async () => {},
  setSaveStatus: () => {},
});

export function SettingsProvider({ children }: { children: ReactNode }) {
  const api = useWsApi();
  const queryClient = useQueryClient();
  const [state, setState] = useState<State>({ dirty: {}, saveStatus: {} });

  const saveMutation = useMutation({
    mutationFn: ({
      namespace,
      values,
    }: {
      namespace: string;
      values: Record<string, unknown>;
    }) => api.setConfigSection(namespace, values),
  });

  const resetMutation = useMutation({
    mutationFn: ({ namespace }: { namespace: string }) =>
      api.resetConfigSection(namespace),
  });

  // ── State mutators (memoized — they're in ApiContext) ────────────
  const setField = useCallback(
    (namespace: string, key: string, value: unknown) => {
      setState((prev) => {
        const ns = { ...(prev.dirty[namespace] ?? {}), [key]: value };
        return {
          ...prev,
          dirty: { ...prev.dirty, [namespace]: ns },
          // Clear stale save-status — the user has new edits.
          saveStatus: { ...prev.saveStatus, [namespace]: null },
        };
      });
    },
    [],
  );

  const setFields = useCallback(
    (namespace: string, values: Record<string, unknown>) => {
      if (Object.keys(values).length === 0) return;
      setState((prev) => {
        const ns = { ...(prev.dirty[namespace] ?? {}), ...values };
        return {
          ...prev,
          dirty: { ...prev.dirty, [namespace]: ns },
          saveStatus: { ...prev.saveStatus, [namespace]: null },
        };
      });
    },
    [],
  );

  const discard = useCallback((namespace: string) => {
    setState((prev) => {
      const nextDirty = { ...prev.dirty };
      delete nextDirty[namespace];
      return { ...prev, dirty: nextDirty };
    });
  }, []);

  const discardAll = useCallback(() => {
    setState({ dirty: {}, saveStatus: {} });
  }, []);

  const setSaveStatus = useCallback(
    (namespace: string, message: string, ok: boolean) => {
      setState((prev) => ({
        ...prev,
        saveStatus: {
          ...prev.saveStatus,
          [namespace]: { message, ok, at: Date.now() },
        },
      }));
      // Auto-clear after 3s, but only if the same message is still
      // up (guards against overwriting a fresher status).
      setTimeout(() => {
        setState((prev) => {
          const cur = prev.saveStatus[namespace];
          if (cur && cur.message === message && cur.ok === ok) {
            return {
              ...prev,
              saveStatus: { ...prev.saveStatus, [namespace]: null },
            };
          }
          return prev;
        });
      }, 3000);
    },
    [],
  );

  // ── RPC actions ──────────────────────────────────────────────────
  const saveNamespace = useCallback(
    async (namespace: string) => {
      const values = state.dirty[namespace];
      if (!values || Object.keys(values).length === 0) return undefined;
      try {
        const result = await saveMutation.mutateAsync({ namespace, values });
        discard(namespace);
        queryClient.invalidateQueries({ queryKey: ["config"] });
        const results =
          (result as { results?: Record<string, { message?: string }> })
            ?.results ?? {};
        const restarted = Object.values(results).some(
          (r) =>
            r?.message?.includes("restarted") ||
            r?.message?.includes("enabled"),
        );
        setSaveStatus(
          namespace,
          restarted ? "Saved — service restarting…" : "Saved",
          true,
        );
        return result;
      } catch {
        setSaveStatus(namespace, "Save failed", false);
        throw new Error("save failed");
      }
    },
    [state.dirty, saveMutation, queryClient, discard, setSaveStatus],
  );

  const saveAll = useCallback(async () => {
    const dirtyNs = Object.keys(state.dirty).filter(
      (ns) => Object.keys(state.dirty[ns] ?? {}).length > 0,
    );
    // Fire all saves in parallel; failures are surfaced via the
    // per-namespace status text, so don't reject the outer promise.
    await Promise.allSettled(dirtyNs.map((ns) => saveNamespace(ns)));
  }, [state.dirty, saveNamespace]);

  const resetToDefaults = useCallback(
    async (namespace: string) => {
      try {
        await resetMutation.mutateAsync({ namespace });
        discard(namespace);
        queryClient.invalidateQueries({ queryKey: ["config"] });
        setSaveStatus(namespace, "Reset to defaults", true);
      } catch {
        setSaveStatus(namespace, "Reset failed", false);
      }
    },
    [resetMutation, discard, queryClient, setSaveStatus],
  );

  // ApiContext value is memoized so its reference is stable across
  // renders — components that consume only the api don't re-render
  // when state changes.
  const apiValue = useMemo<Api>(
    () => ({
      setField,
      setFields,
      discard,
      discardAll,
      saveNamespace,
      saveAll,
      resetToDefaults,
      setSaveStatus,
    }),
    [
      setField,
      setFields,
      discard,
      discardAll,
      saveNamespace,
      saveAll,
      resetToDefaults,
      setSaveStatus,
    ],
  );

  return (
    <ApiContext.Provider value={apiValue}>
      <StateContext.Provider value={state}>{children}</StateContext.Provider>
    </ApiContext.Provider>
  );
}

/** Read the full dirty + saveStatus map. Heavy consumers
 *  (top-of-page StatusBar). Most sections want `useSettingsSection`. */
export function useSettingsState(): State {
  return useContext(StateContext);
}

/** Stable, never re-renders. Use this to mutate. */
export function useSettingsApi(): Api {
  return useContext(ApiContext);
}

/** Convenience hook for a single ConfigSection — bundles the
 *  per-namespace state slice + the api in one read. The state is
 *  read from ``useSettingsState`` so changes to OTHER namespaces
 *  don't re-render the consumer (because the inner slice is
 *  reference-stable when nothing in this namespace changed... in
 *  practice we still re-render on any state update because of how
 *  Context works; the cost is small relative to the wins). */
export function useSettingsSection(namespace: string) {
  const { dirty, saveStatus } = useSettingsState();
  const api = useSettingsApi();
  return {
    dirty: dirty[namespace] ?? {},
    saveStatus: saveStatus[namespace] ?? null,
    setField: (key: string, value: unknown) =>
      api.setField(namespace, key, value),
    setFields: (values: Record<string, unknown>) =>
      api.setFields(namespace, values),
    discard: () => api.discard(namespace),
    save: () => api.saveNamespace(namespace),
    resetToDefaults: () => api.resetToDefaults(namespace),
  };
}

/** Aggregate counters for the top-of-page StatusBar. */
export function useSettingsAggregate() {
  const { dirty } = useSettingsState();
  return useMemo(() => {
    const dirtyNamespaces: string[] = [];
    let totalFields = 0;
    for (const [ns, fields] of Object.entries(dirty)) {
      const n = Object.keys(fields).length;
      if (n > 0) {
        dirtyNamespaces.push(ns);
        totalFields += n;
      }
    }
    return { dirtyNamespaces, totalFields, isDirty: totalFields > 0 };
  }, [dirty]);
}
