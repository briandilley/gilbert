"use client"

import * as React from "react"
import { Dialog as SheetPrimitive } from "@base-ui/react/dialog"

import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { XIcon } from "lucide-react"

/**
 * Sheet — modal drawer that slides in from an edge. Use for
 * navigation drawers (mobile nav, contextual settings) and for any
 * "this is a related-but-separate context" panel that needs the
 * whole vertical (or horizontal) extent.
 *
 * Hairline border on the inner edge, no shadow stack — the sheet is
 * a side panel, not a popover. Header padding and title styling
 * match Dialog so the two read as siblings.
 *
 * Anatomy:
 *
 *   <Sheet open={…} onOpenChange={…}>
 *     <SheetContent side="left" className="w-72 p-0">
 *       <SheetHeader>
 *         <SheetTitle>Conversations</SheetTitle>
 *       </SheetHeader>
 *       …body…
 *     </SheetContent>
 *   </Sheet>
 */

function Sheet({ ...props }: SheetPrimitive.Root.Props) {
  return <SheetPrimitive.Root data-slot="sheet" {...props} />
}

function SheetTrigger({ ...props }: SheetPrimitive.Trigger.Props) {
  return <SheetPrimitive.Trigger data-slot="sheet-trigger" {...props} />
}

function SheetClose({ ...props }: SheetPrimitive.Close.Props) {
  return <SheetPrimitive.Close data-slot="sheet-close" {...props} />
}

function SheetPortal({ ...props }: SheetPrimitive.Portal.Props) {
  return <SheetPrimitive.Portal data-slot="sheet-portal" {...props} />
}

function SheetOverlay({ className, ...props }: SheetPrimitive.Backdrop.Props) {
  return (
    <SheetPrimitive.Backdrop
      data-slot="sheet-overlay"
      className={cn(
        "fixed inset-0 z-50 bg-black/30 transition-opacity duration-150",
        "supports-backdrop-filter:backdrop-blur-[2px]",
        "data-ending-style:opacity-0 data-starting-style:opacity-0",
        className
      )}
      {...props}
    />
  )
}

function SheetContent({
  className,
  children,
  side = "right",
  showCloseButton = true,
  ...props
}: SheetPrimitive.Popup.Props & {
  side?: "top" | "right" | "bottom" | "left"
  showCloseButton?: boolean
}) {
  return (
    <SheetPortal>
      <SheetOverlay />
      <SheetPrimitive.Popup
        data-slot="sheet-content"
        data-side={side}
        className={cn(
          // Surface
          "fixed z-50 flex flex-col gap-0 bg-popover bg-clip-padding text-sm text-popover-foreground",
          // Subtle directional shadow — only on the inner edge so the
          // panel reads as having been pulled out of the canvas, not
          // floated above it.
          "shadow-[0_0_24px_-8px_rgb(0_0_0_/_0.5)]",
          // Motion
          "transition duration-200 ease-(--ease-out)",
          "data-ending-style:opacity-0 data-starting-style:opacity-0",
          // ── Side: bottom ────────────────────────────────────────
          "data-[side=bottom]:inset-x-0 data-[side=bottom]:bottom-0",
          "data-[side=bottom]:h-auto data-[side=bottom]:border-t data-[side=bottom]:border-border",
          "data-[side=bottom]:data-ending-style:translate-y-[2.5rem]",
          "data-[side=bottom]:data-starting-style:translate-y-[2.5rem]",
          // ── Side: left ──────────────────────────────────────────
          "data-[side=left]:inset-y-0 data-[side=left]:left-0",
          "data-[side=left]:h-full data-[side=left]:w-3/4 data-[side=left]:sm:max-w-sm",
          "data-[side=left]:border-r data-[side=left]:border-border",
          "data-[side=left]:data-ending-style:translate-x-[-2.5rem]",
          "data-[side=left]:data-starting-style:translate-x-[-2.5rem]",
          // ── Side: right ─────────────────────────────────────────
          "data-[side=right]:inset-y-0 data-[side=right]:right-0",
          "data-[side=right]:h-full data-[side=right]:w-3/4 data-[side=right]:sm:max-w-sm",
          "data-[side=right]:border-l data-[side=right]:border-border",
          "data-[side=right]:data-ending-style:translate-x-[2.5rem]",
          "data-[side=right]:data-starting-style:translate-x-[2.5rem]",
          // ── Side: top ───────────────────────────────────────────
          "data-[side=top]:inset-x-0 data-[side=top]:top-0",
          "data-[side=top]:h-auto data-[side=top]:border-b data-[side=top]:border-border",
          "data-[side=top]:data-ending-style:translate-y-[-2.5rem]",
          "data-[side=top]:data-starting-style:translate-y-[-2.5rem]",
          className
        )}
        {...props}
      >
        {children}
        {showCloseButton && (
          <SheetPrimitive.Close
            data-slot="sheet-close"
            render={
              <Button
                variant="ghost"
                className="absolute top-2 right-2"
                size="icon-sm"
                aria-label="Close"
              />
            }
          >
            <XIcon />
          </SheetPrimitive.Close>
        )}
      </SheetPrimitive.Popup>
    </SheetPortal>
  )
}

function SheetHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sheet-header"
      className={cn(
        // Hairline-divided from body; matches the page-header pattern.
        "flex flex-col gap-1 px-4 py-3 border-b border-border",
        className
      )}
      {...props}
    />
  )
}

function SheetFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sheet-footer"
      className={cn(
        "mt-auto flex flex-col gap-2 px-4 py-3 border-t border-border",
        "sm:flex-row sm:justify-end",
        className
      )}
      {...props}
    />
  )
}

function SheetTitle({ className, ...props }: SheetPrimitive.Title.Props) {
  return (
    <SheetPrimitive.Title
      data-slot="sheet-title"
      className={cn(
        "text-[15px] font-semibold leading-tight tracking-[-0.01em] text-foreground",
        className
      )}
      {...props}
    />
  )
}

function SheetDescription({
  className,
  ...props
}: SheetPrimitive.Description.Props) {
  return (
    <SheetPrimitive.Description
      data-slot="sheet-description"
      className={cn("text-xs text-muted-foreground leading-relaxed", className)}
      {...props}
    />
  )
}

export {
  Sheet,
  SheetTrigger,
  SheetClose,
  SheetContent,
  SheetHeader,
  SheetFooter,
  SheetTitle,
  SheetDescription,
}
