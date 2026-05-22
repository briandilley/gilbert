import * as React from "react"

import { cn } from "@/lib/utils"

/**
 * Card — the second surface in the hierarchy.
 *
 *   canvas (page bg)  →  Card (hairline-bordered region)  →  inline
 *
 * Cards are mostly transparent. A 1px border + a subtle background
 * shift defines them. No drop-shadow, no large radius, no
 * heavy fill — that's consumer-app vocabulary. The card's job is to
 * isolate a coherent group of controls/data without shouting.
 *
 * Anatomy (composable; pick what you need):
 *
 *   <Card>
 *     <CardHeader>
 *       <CardEyebrow>Inbox</CardEyebrow>     // small uppercase mono
 *       <CardTitle>Mailbox</CardTitle>
 *       <CardDescription>…</CardDescription>
 *       <CardAction>                          // right-aligned actions
 *         <Button variant="ghost" size="icon-sm">…</Button>
 *       </CardAction>
 *     </CardHeader>
 *     <CardContent>…</CardContent>
 *     <CardFooter>                            // hairline-divided
 *       <span className="text-xs text-muted-foreground font-mono">3 unsaved</span>
 *       <Button>Save</Button>
 *     </CardFooter>
 *   </Card>
 *
 * Density:
 *   - default: 16px horizontal padding, 16px vertical with 12px gaps
 *   - sm: 12px padding, 8px gaps — for cards inside cards, dense lists
 */

function Card({
  className,
  size = "default",
  ...props
}: React.ComponentProps<"div"> & { size?: "default" | "sm" }) {
  return (
    <div
      data-slot="card"
      data-size={size}
      className={cn(
        // Hairline-bordered, subtle bg-shift, sharp corners.
        "group/card relative flex flex-col overflow-hidden",
        "rounded-md border border-border bg-card text-card-foreground",
        "text-sm",
        // Default density.
        "gap-3 py-4",
        // Compact density.
        "data-[size=sm]:gap-2 data-[size=sm]:py-3",
        // Images touch the edges (cards are precise rectangles).
        "*:[img:first-child]:rounded-t-md *:[img:last-child]:rounded-b-md has-[>img:first-child]:pt-0",
        // Footer separation handled by CardFooter itself.
        "has-data-[slot=card-footer]:pb-0",
        className
      )}
      {...props}
    />
  )
}

/** Optional small uppercase mono label that prefixes a card title.
 *  Use it to encode context — "INBOX", "MCP", "SECURITY" — when the
 *  card is on a page that contains several similar-looking cards. */
function CardEyebrow({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-eyebrow"
      className={cn(
        "font-mono text-[11px] uppercase tracking-[0.08em] font-medium text-muted-foreground leading-none",
        className
      )}
      {...props}
    />
  )
}

function CardHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-header"
      className={cn(
        "group/card-header @container/card-header grid auto-rows-min items-start gap-1.5",
        "px-4 group-data-[size=sm]/card:px-3",
        "has-data-[slot=card-action]:grid-cols-[1fr_auto]",
        // When followed by a body separator, give it room.
        "[.border-b]:pb-3 group-data-[size=sm]/card:[.border-b]:pb-2.5",
        className
      )}
      {...props}
    />
  )
}

function CardTitle({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-title"
      className={cn(
        "text-[15px] font-semibold leading-tight tracking-[-0.01em]",
        "group-data-[size=sm]/card:text-sm",
        className
      )}
      {...props}
    />
  )
}

function CardDescription({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-description"
      className={cn("text-xs text-muted-foreground leading-relaxed", className)}
      {...props}
    />
  )
}

function CardAction({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-action"
      className={cn(
        "col-start-2 row-span-2 row-start-1 self-start justify-self-end",
        "flex items-center gap-1",
        className
      )}
      {...props}
    />
  )
}

function CardContent({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-content"
      className={cn(
        "px-4 group-data-[size=sm]/card:px-3",
        // First content after a header gets a hairline above it when
        // the header carries a description (heavier visual block).
        className
      )}
      {...props}
    />
  )
}

/** Footer — typically used as the action-bar terminus of a card.
 *  Hairline above; tighter vertical rhythm than the header so it
 *  reads as resolution, not introduction. */
function CardFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-footer"
      className={cn(
        "flex items-center justify-between gap-2",
        "border-t border-border",
        "px-4 py-3 group-data-[size=sm]/card:px-3 group-data-[size=sm]/card:py-2.5",
        "text-xs text-muted-foreground",
        className
      )}
      {...props}
    />
  )
}

export {
  Card,
  CardHeader,
  CardEyebrow,
  CardFooter,
  CardTitle,
  CardAction,
  CardDescription,
  CardContent,
}
