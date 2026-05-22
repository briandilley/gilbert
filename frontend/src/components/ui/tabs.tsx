"use client"

import { Tabs as TabsPrimitive } from "@base-ui/react/tabs"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

/**
 * Tabs — section switcher.
 *
 * Two variants — pick by context:
 *
 *   - ``line`` (default): underline-only active state with
 *     signal-color indicator. The canonical admin-tools tabs look —
 *     minimal, hairline-divider below the strip, encodes the active
 *     state through a single colored bar rather than a filled chip.
 *     Best for page-level tab strips (Security: Users / Roles / Tools
 *     / AI Profiles / …).
 *
 *   - ``pill``: rounded chip group with subtly filled active tab.
 *     Use only when you really want tab-as-control (e.g. a "view
 *     mode" toggle between Table / Card). Avoids dominating the
 *     surface like the old default did.
 *
 * The trigger derives its appearance from the parent list's
 * data-variant attribute, so callers don't pass variant on every
 * trigger — only on the list.
 */

function Tabs({
  className,
  orientation = "horizontal",
  ...props
}: TabsPrimitive.Root.Props) {
  return (
    <TabsPrimitive.Root
      data-slot="tabs"
      data-orientation={orientation}
      className={cn(
        "group/tabs flex gap-3 data-horizontal:flex-col",
        className
      )}
      {...props}
    />
  )
}

const tabsListVariants = cva(
  [
    "group/tabs-list inline-flex w-fit items-center justify-center text-muted-foreground",
    "group-data-horizontal/tabs:h-9 group-data-vertical/tabs:h-fit group-data-vertical/tabs:flex-col",
  ].join(" "),
  {
    variants: {
      variant: {
        line: [
          "gap-0 bg-transparent border-b border-border",
          "group-data-vertical/tabs:border-b-0 group-data-vertical/tabs:border-r",
        ].join(" "),
        pill: "gap-1 rounded-md bg-foreground/5 p-1",
      },
    },
    defaultVariants: {
      variant: "line",
    },
  }
)

function TabsList({
  className,
  variant = "line",
  ...props
}: TabsPrimitive.List.Props & VariantProps<typeof tabsListVariants>) {
  return (
    <TabsPrimitive.List
      data-slot="tabs-list"
      data-variant={variant}
      className={cn(tabsListVariants({ variant }), className)}
      {...props}
    />
  )
}

function TabsTrigger({ className, ...props }: TabsPrimitive.Tab.Props) {
  return (
    <TabsPrimitive.Tab
      data-slot="tabs-trigger"
      className={cn(
        // Base — shared between variants.
        "relative inline-flex items-center justify-center gap-1.5",
        "px-3 text-sm font-medium leading-none whitespace-nowrap",
        "text-muted-foreground transition-[color,background-color] duration-(--duration-fast) ease-(--ease-out)",
        "outline-none select-none",
        "hover:text-foreground",
        "focus-visible:outline-2 focus-visible:outline-(--signal) focus-visible:outline-offset-1",
        "disabled:pointer-events-none disabled:opacity-50",
        "aria-disabled:pointer-events-none aria-disabled:opacity-50",
        "[&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-3.5",
        "group-data-vertical/tabs:w-full group-data-vertical/tabs:justify-start",
        "data-active:text-foreground",
        // Line — underline indicator via ::after, overlapping the
        // list's bottom hairline.
        "h-9",
        "after:absolute after:bg-(--signal) after:opacity-0 after:transition-opacity",
        "data-active:after:opacity-100",
        "group-data-[variant=line]/tabs-list:after:inset-x-0",
        "group-data-[variant=line]/tabs-list:after:-bottom-px",
        "group-data-[variant=line]/tabs-list:after:h-[2px]",
        // Pill — chip-style active with bg fill; hide the underline.
        "group-data-[variant=pill]/tabs-list:h-7",
        "group-data-[variant=pill]/tabs-list:rounded-md",
        "group-data-[variant=pill]/tabs-list:after:hidden",
        "group-data-[variant=pill]/tabs-list:data-active:bg-card",
        "group-data-[variant=pill]/tabs-list:data-active:text-foreground",
        // Vertical line — bar moves to the right edge.
        "group-data-vertical/tabs:group-data-[variant=line]/tabs-list:after:inset-y-0",
        "group-data-vertical/tabs:group-data-[variant=line]/tabs-list:after:-right-px",
        "group-data-vertical/tabs:group-data-[variant=line]/tabs-list:after:w-[2px]",
        "group-data-vertical/tabs:group-data-[variant=line]/tabs-list:after:h-auto",
        "group-data-vertical/tabs:group-data-[variant=line]/tabs-list:after:bottom-auto",
        className
      )}
      {...props}
    />
  )
}

function TabsContent({ className, ...props }: TabsPrimitive.Panel.Props) {
  return (
    <TabsPrimitive.Panel
      data-slot="tabs-content"
      className={cn("flex-1 text-sm outline-none", className)}
      {...props}
    />
  )
}

export { Tabs, TabsList, TabsTrigger, TabsContent, tabsListVariants }
