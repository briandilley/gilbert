import * as React from "react"

import { cn } from "@/lib/utils"

/**
 * StatusBar — the canonical "unsaved changes / Save / Cancel" action
 * row. Pinned to a containing surface's edge with a hairline.
 *
 * Used at the page level (sticky top, below a PageHeader) when a
 * form's edits are deferred until the user explicitly saves; and at
 * the card level (via <CardFooter>, which has its own version of
 * this layout) for save-bound regions inside a panel.
 *
 *   <StatusBar
 *     status={<>3 unsaved <span className="font-mono text-(--signal)">changes</span></>}
 *     actions={<>
 *       <Button variant="outline" size="sm">Discard</Button>
 *       <Button size="sm">Save all</Button>
 *     </>}
 *   />
 *
 * Variants:
 *   - ``position="top"``    — sticky top, hairline below.
 *                             Use this for page-level save bars
 *                             that should track scrolling.
 *   - ``position="bottom"`` — sticky bottom, hairline above.
 *                             Use when actions belong at the end of
 *                             a long form (less common in admin
 *                             tools; usually you want the save
 *                             always visible at top).
 *   - ``position="static"`` — non-sticky inline action row. Use
 *                             at the bottom of a single section.
 *
 * Tone:
 *   - ``tone="default"`` — neutral. The bar is present but quiet.
 *   - ``tone="dirty"``   — signal-tinted left edge to draw the eye.
 *                          Set when there are unsaved changes the
 *                          user should resolve.
 */

interface StatusBarProps extends React.ComponentProps<"div"> {
  /** Left side — typically a count + label ("3 unsaved changes")
   *  or a status pill. */
  status?: React.ReactNode
  /** Right side — buttons. Order primary action last. */
  actions?: React.ReactNode
  position?: "top" | "bottom" | "static"
  tone?: "default" | "dirty"
}

export function StatusBar({
  status,
  actions,
  position = "top",
  tone = "default",
  className,
  ...props
}: StatusBarProps) {
  return (
    <div
      data-slot="status-bar"
      data-position={position}
      data-tone={tone}
      className={cn(
        "flex items-center justify-between gap-3 px-6 py-2.5",
        "bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/75",
        // Position handling
        position === "top" && "sticky top-0 z-30 border-b border-border",
        position === "bottom" && "sticky bottom-0 z-30 border-t border-border",
        position === "static" && "border-t border-border",
        // Tone — when dirty, a 2px signal-color rail on the left
        // edge silently announces "this needs resolution."
        "relative",
        tone === "dirty" && [
          "before:absolute before:left-0 before:top-0 before:bottom-0",
          "before:w-[2px] before:bg-(--signal)",
        ].join(" "),
        className
      )}
      {...props}
    >
      <div data-slot="status-bar-status" className="text-xs text-foreground/85 min-w-0 flex-1">
        {status}
      </div>
      {actions ? (
        <div
          data-slot="status-bar-actions"
          className="flex shrink-0 items-center gap-1.5"
        >
          {actions}
        </div>
      ) : null}
    </div>
  )
}
