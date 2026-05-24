// PhoneCallsPage — the SPA surface for outbound phone calls.
//
// Layout: two-pane.
//   left  = sidebar list of recent calls, newest first
//   right = detail pane for the selected call (transcript + intervene)
//
// Subscribes to ``phone.call.transcript_delta`` for live transcript
// updates and ``phone.call.status_changed`` for status badges, both
// scoped to whichever call is currently open. The list itself
// refreshes from ``phone.call.list`` on the lifecycle events the hook
// already listens to.
//
// This is the MVP UI — focused on the dogfooding flow: "make a call,
// watch it happen, type a directive if Gilbert gets stuck." A nicer
// dedicated detail page with recording playback, structured outcome
// display, etc. is a later iteration.

import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  PhoneIcon,
  PhoneOffIcon,
  PhoneOutgoingIcon,
  SendIcon,
  XCircleIcon,
} from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/textarea";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { useEventBus } from "@/hooks/useEventBus";
import { usePhoneCalls } from "@/hooks/usePhoneCalls";
import type {
  CallStatus,
  PhoneCallDetail,
  PhoneCallSummary,
  PhoneCallTranscriptTurn,
} from "@/types/phoneCall";
import type { GilbertEvent } from "@/types/events";
import { cn } from "@/lib/utils";

const STATUS_TONE: Record<CallStatus, string> = {
  initiated: "bg-muted text-muted-foreground",
  ringing: "bg-info/15 text-info",
  connected: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  hung_up: "bg-muted text-muted-foreground",
  failed: "bg-destructive/15 text-destructive",
};

function StatusBadge({ status }: { status: CallStatus }) {
  return (
    <Badge variant="outline" className={cn("text-[10px]", STATUS_TONE[status])}>
      {status.replace("_", " ")}
    </Badge>
  );
}

function formatDuration(seconds: number): string {
  if (!seconds || seconds < 1) return "—";
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return mins ? `${mins}m ${secs}s` : `${secs}s`;
}

function timeAgo(iso: string): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  const seconds = Math.floor((Date.now() - then) / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function PhoneCallsPage() {
  const navigate = useNavigate();
  const { callId: routeCallId } = useParams();
  const { calls, loading, error, fetchCall, intervene, hangUp } =
    usePhoneCalls();
  const [selected, setSelected] = useState<PhoneCallDetail | null>(null);

  // Resolve "selected" from the URL when the user lands on /calls/:id
  // directly, then keep it in sync when they pick from the list.
  useEffect(() => {
    if (!routeCallId) {
      setSelected(null);
      return;
    }
    let cancelled = false;
    void fetchCall(routeCallId).then((detail) => {
      if (!cancelled) setSelected(detail);
    });
    return () => {
      cancelled = true;
    };
  }, [routeCallId, fetchCall]);

  // Live-append transcript turns for the currently-open call. We
  // ignore deltas for any other call so flipping between calls doesn't
  // get cross-talk. Each delta is just one turn; we push it onto the
  // existing transcript list locally rather than re-fetching the whole
  // record.
  useEventBus("phone.call.transcript_delta", (event: GilbertEvent) => {
    const data = event.data as {
      call_id?: string;
      who?: PhoneCallTranscriptTurn["who"];
      text?: string;
      ts?: number;
    };
    if (!selected || data.call_id !== selected.call_id) return;
    setSelected((prev) =>
      prev
        ? {
            ...prev,
            transcript: [
              ...prev.transcript,
              {
                who: data.who ?? "system",
                text: data.text ?? "",
                ts: data.ts ?? 0,
              },
            ],
          }
        : prev,
    );
  });

  // Status updates for the open call. Refetch on transition rather than
  // mutating in place — the backend also fills in duration / outcome
  // on the ended-status edge, so re-grabbing the full record keeps the
  // detail pane honest.
  useEventBus("phone.call.status_changed", (event: GilbertEvent) => {
    const data = event.data as { call_id?: string };
    if (!selected || data.call_id !== selected.call_id) return;
    void fetchCall(selected.call_id).then((detail) => {
      if (detail) setSelected(detail);
    });
  });

  // Sort calls newest-first. The hook already returns them in that
  // order from the backend, but defensive in case storage ordering
  // changes.
  const sortedCalls = useMemo(
    () =>
      [...calls].sort((a, b) =>
        (b.started_at || "").localeCompare(a.started_at || ""),
      ),
    [calls],
  );

  const handleSelect = useCallback(
    (call: PhoneCallSummary) => {
      navigate(`/calls/${call.call_id}`);
    },
    [navigate],
  );

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        eyebrow="Phone"
        title="Calls"
        description={
          "Outbound calls Gilbert has placed on your behalf. Click one " +
          "to see the live transcript and intervene if needed."
        }
      />
      {error && (
        <div className="px-6 py-2 text-xs text-destructive">{error}</div>
      )}
      <div className="flex min-h-0 flex-1">
        {/* List pane */}
        <div className="w-80 shrink-0 overflow-y-auto border-r border-border">
          {loading ? (
            <div className="p-8 flex justify-center">
              <LoadingSpinner />
            </div>
          ) : sortedCalls.length === 0 ? (
            <div className="p-8 text-center text-xs text-muted-foreground">
              No calls yet. Ask Gilbert to call someone — e.g.{" "}
              <em>"Hey Gilbert, call (303) 555-0100 and ask if they're open"</em>.
            </div>
          ) : (
            <div className="divide-y divide-border">
              {sortedCalls.map((c) => (
                <button
                  key={c.call_id}
                  type="button"
                  onClick={() => handleSelect(c)}
                  className={cn(
                    "w-full px-3 py-2.5 text-left hover:bg-accent transition-colors",
                    selected?.call_id === c.call_id && "bg-accent",
                  )}
                >
                  <div className="flex items-center gap-2 mb-1">
                    <PhoneOutgoingIcon className="size-3.5 text-muted-foreground shrink-0" />
                    <span className="text-sm font-medium truncate">
                      {c.to_number}
                    </span>
                    <StatusBadge status={c.status} />
                  </div>
                  <div className="text-[11px] text-muted-foreground truncate">
                    {c.brief_preview || "(no brief)"}
                  </div>
                  <div className="text-[10px] text-muted-foreground/70 mt-0.5 flex items-center gap-1.5">
                    <span>{timeAgo(c.started_at)}</span>
                    {c.duration_seconds > 0 && (
                      <>
                        <span>·</span>
                        <span>{formatDuration(c.duration_seconds)}</span>
                      </>
                    )}
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Detail pane */}
        <div className="flex min-h-0 flex-1 flex-col">
          {selected ? (
            <CallDetail
              call={selected}
              onIntervene={(d) => intervene(selected.call_id, d)}
              onHangUp={() => hangUp(selected.call_id)}
            />
          ) : (
            <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
              <div className="text-center max-w-sm">
                <PhoneIcon className="mx-auto size-8 text-muted-foreground/30 mb-3" />
                <p>Pick a call on the left to see its transcript.</p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function CallDetail({
  call,
  onIntervene,
  onHangUp,
}: {
  call: PhoneCallDetail;
  onIntervene: (directive: string) => Promise<void>;
  onHangUp: () => Promise<void>;
}) {
  const isActive = call.status === "connected" || call.status === "ringing"
    || call.status === "initiated";
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="border-b border-border px-5 py-4">
        <div className="flex items-center gap-2 mb-1">
          <PhoneOutgoingIcon className="size-4 text-muted-foreground" />
          <h2 className="text-base font-semibold">{call.to_number}</h2>
          <StatusBadge status={call.status} />
          {isActive && (
            <Button
              size="sm"
              variant="ghost"
              className="ml-auto text-destructive hover:text-destructive hover:bg-destructive/10"
              onClick={() => void onHangUp()}
            >
              <PhoneOffIcon className="size-3.5 mr-1" />
              Hang up
            </Button>
          )}
        </div>
        <div className="text-[11px] text-muted-foreground">
          {call.from_number} →&nbsp;
          {call.to_number}
          {call.duration_seconds > 0 && (
            <span> · {formatDuration(call.duration_seconds)}</span>
          )}
          {call.callback_number && (
            <span> · callback: {call.callback_number}</span>
          )}
        </div>
        {call.brief && (
          <details className="mt-2">
            <summary className="text-[11px] text-muted-foreground cursor-pointer">
              Brief
            </summary>
            <p className="mt-1 text-xs whitespace-pre-wrap text-muted-foreground/90">
              {call.brief}
            </p>
          </details>
        )}
        {Object.keys(call.outcome).length > 0 && (
          <details open className="mt-2">
            <summary className="text-[11px] text-muted-foreground cursor-pointer">
              Outcome
            </summary>
            <pre className="mt-1 text-xs whitespace-pre-wrap bg-muted/40 rounded p-2 overflow-x-auto">
              {JSON.stringify(call.outcome, null, 2)}
            </pre>
          </details>
        )}
        {call.failure_reason && (
          <div className="mt-2 flex items-start gap-1.5 text-xs text-destructive">
            <XCircleIcon className="size-3.5 shrink-0 mt-0.5" />
            <span>{call.failure_reason}</span>
          </div>
        )}
      </div>

      {/* Transcript */}
      <div className="flex-1 overflow-y-auto px-5 py-4 space-y-2.5">
        {call.transcript.length === 0 ? (
          <p className="text-xs text-muted-foreground italic">
            (no transcript yet — call hasn't connected)
          </p>
        ) : (
          call.transcript.map((turn, idx) => (
            <TranscriptRow key={`${turn.ts}-${idx}`} turn={turn} />
          ))
        )}
      </div>

      {isActive && (
        <InterveneBox onSubmit={onIntervene} />
      )}
    </div>
  );
}

function TranscriptRow({ turn }: { turn: PhoneCallTranscriptTurn }) {
  // Three visual lanes — Gilbert (signal color), remote (foreground),
  // system notes (muted). Mirrors the chat transcript vocabulary so
  // anyone reading both pages reads them the same way.
  //
  // Layout note: each speaker line is its own block element so
  // copy-paste preserves line breaks between turns (flex sibling
  // boundaries don't add whitespace to the clipboard, but block
  // boundaries do). The speaker label includes a literal colon +
  // space so a paste reads "Them: Hello?" instead of "ThemHello?".
  const isUs = turn.who === "us";
  const isSystem = turn.who === "system" || turn.who === "user_intervention";
  const label = isUs ? "Gilbert" : isSystem ? "system" : "Them";
  return (
    <div className="flex gap-2 items-start">
      <span
        className={cn(
          "text-[10px] uppercase tracking-wider font-medium w-16 shrink-0 mt-0.5",
          isUs
            ? "text-(--signal)"
            : isSystem
              ? "text-muted-foreground/60 italic"
              : "text-foreground",
        )}
      >
        {/* Explicit colon AND space — without the space the paste
            comes out as "Them:Hello?". JSX whitespace inside a
            tag's text node is preserved as-is, but the visual
            ``flex gap-2`` between the two spans only affects
            layout; the clipboard doesn't see it. The string-
            literal form ensures the trailing space survives JSX
            tokenization. */}
        {`${label}: `}
      </span>
      <span
        className={cn(
          "text-sm flex-1 whitespace-pre-wrap break-words leading-relaxed",
          isSystem && "text-xs text-muted-foreground/80 italic",
        )}
      >
        {turn.text}
      </span>
    </div>
  );
}

function InterveneBox({
  onSubmit,
}: {
  onSubmit: (directive: string) => Promise<void>;
}) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);

  const handleSubmit = async () => {
    const trimmed = text.trim();
    if (!trimmed || sending) return;
    setSending(true);
    try {
      await onSubmit(trimmed);
      setText("");
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="border-t border-border px-4 py-3">
      <div className="flex gap-2 items-end">
        <Textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void handleSubmit();
            }
          }}
          placeholder={
            'Direct Gilbert mid-call ("ask about the loaner again", ' +
            '"don\'t agree to Tuesday")…'
          }
          className="min-h-[44px] max-h-32 text-sm"
          disabled={sending}
        />
        <Button
          type="button"
          size="sm"
          onClick={() => void handleSubmit()}
          disabled={!text.trim() || sending}
        >
          <SendIcon className="size-3.5 mr-1" />
          Send
        </Button>
      </div>
      <p className="text-[10px] text-muted-foreground mt-1.5">
        Your directive lands as a system note on Gilbert's next turn.
        Enter to send, Shift+Enter for a newline.
      </p>
    </div>
  );
}
