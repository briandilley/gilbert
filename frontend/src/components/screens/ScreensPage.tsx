import { useState, useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "@/hooks/useAuth";
import { fetchScreensInfo, type ScreensInfo } from "@/api/screens";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { MarkdownContent } from "@/components/ui/MarkdownContent";

type ScreenState = "idle" | "loading" | "display" | "error";

interface ScreenImage {
  url: string;
  caption?: string;
}

type DisplayContent =
  | {
      kind: "document";
      title: string;
      contentType: "pdf" | "image" | "other";
      serveUrl: string;
    }
  | { kind: "text"; title: string; content: string }
  | { kind: "images"; title: string; images: ScreenImage[] };

export function ScreensPage() {
  const navigate = useNavigate();
  const { user, loading: authLoading } = useAuth();
  const [info, setInfo] = useState<ScreensInfo | null>(null);
  const [infoLoading, setInfoLoading] = useState(true);

  const [screenName, setScreenName] = useState("");
  const [connected, setConnected] = useState(false);
  const [state, setState] = useState<ScreenState>("idle");
  const [content, setContent] = useState<DisplayContent | null>(null);
  const [loadingMessage, setLoadingMessage] = useState("");
  const [errorMessage, setErrorMessage] = useState("");
  const [reconnecting, setReconnecting] = useState(false);
  const eventSourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    fetchScreensInfo()
      .then(setInfo)
      .catch(() => setInfo({ enabled: false, allow_guest_screens: false }))
      .finally(() => setInfoLoading(false));
  }, []);

  useEffect(() => {
    return () => {
      eventSourceRef.current?.close();
    };
  }, []);

  function connect() {
    const name = screenName.trim();
    if (!name) return;

    setConnected(true);
    setState("idle");
    setContent(null);

    const params = new URLSearchParams({ name });
    const es = new EventSource(`/screens/stream?${params}`);
    eventSourceRef.current = es;

    es.onopen = () => setReconnecting(false);

    es.addEventListener("show_document", (e) => {
      const data = JSON.parse((e as MessageEvent).data);
      setContent({
        kind: "document",
        title: data.title,
        contentType: data.content_type,
        serveUrl: data.serve_url,
      });
      setState("display");
    });

    es.addEventListener("show_text", (e) => {
      const data = JSON.parse((e as MessageEvent).data);
      setContent({ kind: "text", title: data.title, content: data.content });
      setState("display");
    });

    es.addEventListener("show_images", (e) => {
      const data = JSON.parse((e as MessageEvent).data);
      setContent({ kind: "images", title: data.title, images: data.images });
      setState("display");
    });

    es.addEventListener("clear", () => {
      setContent(null);
      setState("idle");
    });

    es.addEventListener("loading", (e) => {
      const data = JSON.parse((e as MessageEvent).data);
      setLoadingMessage(data.message || "Loading…");
      setState("loading");
    });

    es.addEventListener("show_error", (e) => {
      const data = JSON.parse((e as MessageEvent).data);
      setErrorMessage(data.message || "Unknown error");
      setState("error");
    });

    // Native transport error (connection dropped). EventSource auto-retries;
    // surface a reconnecting hint without clobbering displayed content.
    es.onerror = () => setReconnecting(true);
  }

  // ── Pre-connect: gating ────────────────────────────────────
  if (!connected) {
    if (authLoading || infoLoading) {
      return (
        <div className="flex min-h-screen items-center justify-center">
          <LoadingSpinner />
        </div>
      );
    }

    if (!info?.enabled) {
      return (
        <CenteredMessage title="Screens are disabled." />
      );
    }

    const canSetup = !!user || info.allow_guest_screens;
    if (!canSetup) {
      return (
        <div className="flex min-h-screen items-center justify-center p-6">
          <Card className="w-full max-w-sm">
            <CardHeader>
              <CardTitle>Set up a screen</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <p className="text-sm text-muted-foreground">
                Sign in to set up a screen.
              </p>
              <Button
                className="w-full"
                onClick={() => navigate("/auth/login")}
              >
                Sign in
              </Button>
            </CardContent>
          </Card>
        </div>
      );
    }

    return (
      <div className="flex min-h-screen items-center justify-center p-6">
        <Card className="w-full max-w-sm">
          <CardHeader>
            <CardTitle>Screen Setup</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <Input
              value={screenName}
              onChange={(e) => setScreenName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && connect()}
              placeholder="Screen name"
            />
            <Button onClick={connect} className="w-full">
              Connect
            </Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  // ── Connected: slim title bar + content area ───────────────
  const titleText = state === "display" && content ? content.title : screenName;

  return (
    <div className="flex h-screen flex-col">
      <div className="flex h-10 shrink-0 items-center gap-3 border-b px-4">
        <span className="truncate text-sm font-medium">{titleText}</span>
        {reconnecting && (
          <span className="shrink-0 text-xs text-muted-foreground">
            Reconnecting…
          </span>
        )}
      </div>
      <div className="min-h-0 flex-1">{renderContent()}</div>
    </div>
  );

  function renderContent() {
    if (state === "idle") {
      return (
        <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
          Waiting for content…
        </div>
      );
    }

    if (state === "loading") {
      return (
        <div className="flex h-full items-center justify-center">
          <LoadingSpinner text={loadingMessage} />
        </div>
      );
    }

    if (state === "error") {
      return (
        <div className="flex h-full items-center justify-center">
          <div className="text-center text-destructive">
            <div className="text-lg font-medium">Error</div>
            <div className="text-sm">{errorMessage}</div>
          </div>
        </div>
      );
    }

    if (!content) return null;

    switch (content.kind) {
      case "document":
        if (content.contentType === "image") {
          return (
            <div className="flex h-full items-center justify-center p-4">
              <img
                src={content.serveUrl}
                alt={content.title}
                className="max-h-full max-w-full object-contain"
              />
            </div>
          );
        }
        return (
          <iframe
            src={content.serveUrl}
            className="h-full w-full border-0"
            title={content.title}
          />
        );
      case "text":
        return (
          <div className="mx-auto h-full max-w-3xl overflow-auto p-8">
            <MarkdownContent content={content.content} />
          </div>
        );
      case "images":
        return (
          <div className="h-full overflow-auto p-4">
            <div className="grid grid-cols-2 gap-4 md:grid-cols-3">
              {content.images.map((img, i) => (
                <figure key={i} className="space-y-1">
                  <img
                    src={img.url}
                    alt={img.caption || ""}
                    className="w-full rounded object-contain"
                  />
                  {img.caption && (
                    <figcaption className="text-center text-sm text-muted-foreground">
                      {img.caption}
                    </figcaption>
                  )}
                </figure>
              ))}
            </div>
          </div>
        );
      default:
        return null;
    }
  }
}

function CenteredMessage({ title }: { title: string }) {
  return (
    <div className="flex min-h-screen items-center justify-center p-6">
      <div className="text-center text-muted-foreground">{title}</div>
    </div>
  );
}
