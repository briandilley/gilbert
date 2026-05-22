"use client"

import { Tooltip as TooltipPrimitive } from "@base-ui/react/tooltip"

import { cn } from "@/lib/utils"

/**
 * Tooltip — small floating caption.
 *
 * In line with the system, tooltips read as a small popover (same
 * vocabulary as Select / DropdownMenu): hairline-bordered, dark
 * popover bg, dense padding, mono caption. No inverted-foreground
 * speech-bubble look. Arrow is omitted by default — a small hairline
 * tooltip doesn't need a wedge, and the wedge is the part that reads
 * most like consumer UI.
 *
 * Default delay is 0. Wrap a region in <TooltipProvider delay={300}>
 * to throttle eager hover for dense rows.
 */

function TooltipProvider({
  delay = 0,
  ...props
}: TooltipPrimitive.Provider.Props) {
  return (
    <TooltipPrimitive.Provider
      data-slot="tooltip-provider"
      delay={delay}
      {...props}
    />
  )
}

function Tooltip({ ...props }: TooltipPrimitive.Root.Props) {
  return <TooltipPrimitive.Root data-slot="tooltip" {...props} />
}

function TooltipTrigger({ ...props }: TooltipPrimitive.Trigger.Props) {
  return <TooltipPrimitive.Trigger data-slot="tooltip-trigger" {...props} />
}

function TooltipContent({
  className,
  side = "top",
  sideOffset = 6,
  align = "center",
  alignOffset = 0,
  children,
  ...props
}: TooltipPrimitive.Popup.Props &
  Pick<
    TooltipPrimitive.Positioner.Props,
    "align" | "alignOffset" | "side" | "sideOffset"
  >) {
  return (
    <TooltipPrimitive.Portal>
      <TooltipPrimitive.Positioner
        align={align}
        alignOffset={alignOffset}
        side={side}
        sideOffset={sideOffset}
        className="isolate z-50"
      >
        <TooltipPrimitive.Popup
          data-slot="tooltip-content"
          className={cn(
            "z-50 inline-flex w-fit max-w-[18rem] items-center gap-1.5",
            "rounded-sm border border-border bg-popover px-2 py-1",
            "font-mono text-[11px] tracking-tight leading-snug text-popover-foreground",
            "origin-(--transform-origin) shadow-[0_4px_12px_-4px_rgb(0_0_0_/_0.4)]",
            "has-data-[slot=kbd]:pr-1.5",
            // Enter animations
            "data-[state=delayed-open]:animate-in data-[state=delayed-open]:fade-in-0 data-[state=delayed-open]:zoom-in-[0.98]",
            "data-open:animate-in data-open:fade-in-0 data-open:zoom-in-[0.98]",
            // Exit animations
            "data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-[0.98]",
            // Slide-in directional cues
            "data-[side=bottom]:slide-in-from-top-1",
            "data-[side=top]:slide-in-from-bottom-1",
            "data-[side=left]:slide-in-from-right-1",
            "data-[side=right]:slide-in-from-left-1",
            "data-[side=inline-end]:slide-in-from-left-1",
            "data-[side=inline-start]:slide-in-from-right-1",
            // Kbd hints inside tooltips
            "**:data-[slot=kbd]:relative **:data-[slot=kbd]:isolate **:data-[slot=kbd]:z-50 **:data-[slot=kbd]:rounded-sm",
            className
          )}
          {...props}
        >
          {children}
        </TooltipPrimitive.Popup>
      </TooltipPrimitive.Positioner>
    </TooltipPrimitive.Portal>
  )
}

export { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider }
