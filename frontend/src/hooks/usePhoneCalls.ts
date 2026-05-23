// Phone-call API hook — wraps the ``phone.call.*`` WS RPCs in a
// React-friendly surface. Lives at the app level (not a per-component
// hook) because both the calls page and the header indicator consume
// it.
//
// We don't subscribe to ``phone.call.transcript_delta`` here — that's
// per-call traffic best handled inside the component watching the
// active call. This hook deals in the list + per-call fetch shape.

import { useCallback, useEffect, useState } from "react";
import { useWebSocket } from "./useWebSocket";
import type {
  PhoneCallDetail,
  PhoneCallSummary,
} from "@/types/phoneCall";
import type { GilbertEvent } from "@/types/events";

interface ListResult {
  calls?: PhoneCallSummary[];
}

interface GetResult {
  call?: PhoneCallDetail;
}

interface TestResult {
  call_id?: string;
}

export interface UsePhoneCallsApi {
  /** Current list of calls (newest first). Refreshed on
   *  ``phone.call.started`` / ``phone.call.ended`` events. */
  calls: PhoneCallSummary[];
  /** True while the initial list is loading. */
  loading: boolean;
  /** Last error from a list / get / test call. Cleared on success. */
  error: string | null;

  /** Fetch the full record for one call (transcript + interventions). */
  fetchCall: (callId: string) => Promise<PhoneCallDetail | null>;

  /** Inject a directive into an active call. */
  intervene: (callId: string, directive: string) => Promise<void>;

  /** Force-hang-up an active call. */
  hangUp: (callId: string) => Promise<void>;

  /** Place a test call from the Settings page test button. */
  placeTestCall: (toNumber: string) => Promise<string | null>;
}

export function usePhoneCalls(): UsePhoneCallsApi {
  const { connected, rpc, subscribe } = useWebSocket();
  const [calls, setCalls] = useState<PhoneCallSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Refresh the list on WS connect + whenever a call edge-transitions.
  const refresh = useCallback(async () => {
    if (!connected) return;
    try {
      const res = await rpc<ListResult>({ type: "phone.call.list" });
      setCalls(res.calls ?? []);
      setError(null);
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
    } finally {
      setLoading(false);
    }
  }, [connected, rpc]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Live-refresh on lifecycle events. We trigger a fresh ``list``
  // rather than mutate the cache in place because the summary derived
  // by the backend (status, duration, outcome) is easier to re-fetch
  // than to recompute on the frontend.
  useEffect(() => {
    const handler = (_event: GilbertEvent) => {
      void refresh();
    };
    const unsubs = [
      subscribe("phone.call.started", handler),
      subscribe("phone.call.ended", handler),
      subscribe("phone.call.status_changed", handler),
    ];
    return () => {
      unsubs.forEach((u) => u());
    };
  }, [subscribe, refresh]);

  const fetchCall = useCallback(
    async (callId: string): Promise<PhoneCallDetail | null> => {
      try {
        const res = await rpc<GetResult>({
          type: "phone.call.get",
          call_id: callId,
        });
        return res.call ?? null;
      } catch (e) {
        setError(String((e as Error)?.message ?? e));
        return null;
      }
    },
    [rpc],
  );

  const intervene = useCallback(
    async (callId: string, directive: string): Promise<void> => {
      await rpc({
        type: "phone.call.intervene_text",
        call_id: callId,
        directive,
      });
    },
    [rpc],
  );

  const hangUp = useCallback(
    async (callId: string): Promise<void> => {
      await rpc({ type: "phone.call.hang_up", call_id: callId });
    },
    [rpc],
  );

  const placeTestCall = useCallback(
    async (toNumber: string): Promise<string | null> => {
      try {
        const res = await rpc<TestResult>({
          type: "phone.call.test",
          to_number: toNumber,
        });
        await refresh();
        return res.call_id ?? null;
      } catch (e) {
        setError(String((e as Error)?.message ?? e));
        return null;
      }
    },
    [rpc, refresh],
  );

  return {
    calls,
    loading,
    error,
    fetchCall,
    intervene,
    hangUp,
    placeTestCall,
  };
}
