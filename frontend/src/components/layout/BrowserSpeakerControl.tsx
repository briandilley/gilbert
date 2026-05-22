import {
  Volume2Icon,
  VolumeXIcon,
  PlayIcon,
  PauseIcon,
  SquareIcon,
  XIcon,
} from "lucide-react";
import { Popover as PopoverPrimitive } from "@base-ui/react/popover";
import { useBrowserSpeaker } from "@/hooks/useBrowserSpeaker";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

/**
 * BrowserSpeakerControl — header icon button that opens a panel for
 * controlling browser-side audio playback.
 *
 * - Toggle to enable/disable receiving audio on this tab.
 * - Scrollable history list of recently played clips with replay buttons.
 * - "Clear history" action when history is non-empty.
 *
 * Uses @base-ui/react/popover directly since the project does not yet
 * have a /ui/popover wrapper.
 */
export function BrowserSpeakerControl() {
  const {
    enabled,
    setEnabled,
    history,
    currentItem,
    isPlaying,
    isPaused,
    replay,
    clearHistory,
    pause,
    resume,
    stop,
  } = useBrowserSpeaker();

  const Icon = enabled ? Volume2Icon : VolumeXIcon;

  return (
    <PopoverPrimitive.Root>
      <PopoverPrimitive.Trigger
        render={
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Browser speaker"
            title={enabled ? "Browser speaker on" : "Browser speaker off"}
          />
        }
      >
        <Icon
          className={cn(
            "size-4",
            enabled ? "text-foreground" : "text-muted-foreground",
            isPlaying && "animate-pulse",
          )}
        />
      </PopoverPrimitive.Trigger>

      <PopoverPrimitive.Portal>
        <PopoverPrimitive.Positioner
          className="isolate z-50 outline-none"
          align="end"
          sideOffset={6}
        >
          <PopoverPrimitive.Popup
            className={cn(
              "w-80 rounded-md border border-border bg-popover text-popover-foreground",
              "shadow-[0_4px_16px_-4px_rgb(0_0_0_/_0.35)]",
              "origin-(--transform-origin) outline-none",
              "data-[side=bottom]:slide-in-from-top-2",
              "data-[side=top]:slide-in-from-bottom-2",
              "data-[side=left]:slide-in-from-right-2",
              "data-[side=right]:slide-in-from-left-2",
              "data-open:animate-in data-open:fade-in-0 data-open:zoom-in-[0.98]",
              "data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-[0.98]",
              "duration-100",
            )}
          >
            {/* Toggle row */}
            <div className="flex items-center justify-between p-3 border-b border-border">
              <Label htmlFor="browser-speaker-switch" className="text-sm cursor-pointer">
                Receive audio on this tab
              </Label>
              <Switch
                id="browser-speaker-switch"
                checked={enabled}
                onCheckedChange={setEnabled}
              />
            </div>

            {/* Now-Playing row — only when a clip / stream is loaded.
                Streams have no native ``ended`` event, so this is the
                only way to release the connection. */}
            {currentItem && (
              <div className="flex items-center gap-2 p-3 border-b border-border">
                <div className="flex-1 min-w-0">
                  <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                    {isPlaying ? "Playing" : isPaused ? "Paused" : "Stopped"}
                  </div>
                  <div className="text-sm truncate">
                    {currentItem.title || "Audio clip"}
                  </div>
                </div>
                {isPlaying ? (
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    onClick={pause}
                    aria-label="Pause"
                    title="Pause"
                  >
                    <PauseIcon className="size-3.5" />
                  </Button>
                ) : (
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    onClick={resume}
                    disabled={!isPaused}
                    aria-label="Resume"
                    title="Resume"
                  >
                    <PlayIcon className="size-3.5" />
                  </Button>
                )}
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={stop}
                  aria-label="Stop"
                  title="Stop"
                >
                  <SquareIcon className="size-3.5" />
                </Button>
              </div>
            )}

            {/* History list */}
            <div className="max-h-64 overflow-y-auto">
              {history.length === 0 ? (
                <div className="p-4 text-center text-xs text-muted-foreground">
                  Nothing has played yet.
                </div>
              ) : (
                <ul className="divide-y divide-border">
                  {history.map((item) => (
                    <li
                      key={item.id}
                      className="flex items-center gap-2 px-3 py-2"
                    >
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        onClick={() => replay(item.id)}
                        aria-label={`Replay ${item.title || "audio"}`}
                      >
                        <PlayIcon className="size-3.5" />
                      </Button>
                      <div className="flex-1 min-w-0">
                        <div className="text-xs truncate">
                          {item.title || "Audio clip"}
                        </div>
                        <div className="text-[10px] text-muted-foreground">
                          {timeAgo(item.receivedAt)}
                        </div>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {/* Clear history footer */}
            {history.length > 0 && (
              <div className="p-2 border-t border-border">
                <Button
                  variant="ghost"
                  size="sm"
                  className="w-full"
                  onClick={clearHistory}
                >
                  <XIcon className="size-3.5" />
                  Clear history
                </Button>
              </div>
            )}
          </PopoverPrimitive.Popup>
        </PopoverPrimitive.Positioner>
      </PopoverPrimitive.Portal>
    </PopoverPrimitive.Root>
  );
}

function timeAgo(ms: number): string {
  const diff = Date.now() - ms;
  const sec = Math.floor(diff / 1000);
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  return `${hr}h ago`;
}
