import { Switch as SwitchPrimitive } from "@base-ui/react/switch"

import { cn } from "@/lib/utils"

// `Switch` from base-ui is a namespace (Root / Thumb), unlike
// `Button` which is the direct primitive. Pull Root out as the
// surface element.

/**
 * Switch — boolean toggle primitive.
 *
 * Use for any "on/off" choice where the state takes effect
 * immediately (or as part of a saved batch). Not for transient
 * filters — those should use chip-style or buttons.
 *
 * The track is hairline-bordered when off (so the off-state reads
 * as a real container, not just absence) and signal-filled when on.
 * Thumb is a 12px circle that travels h-5 width — fits inside the
 * compact admin density.
 */

function Switch({
  className,
  ...props
}: SwitchPrimitive.Root.Props) {
  return (
    <SwitchPrimitive.Root
      data-slot="switch"
      className={cn(
        // Track
        "group/switch peer inline-flex shrink-0 cursor-pointer items-center",
        "h-4 w-7 rounded-full border border-border bg-transparent",
        "transition-[background-color,border-color] duration-(--duration-fast) ease-(--ease-out)",
        // Focus
        "outline-none focus-visible:outline-2 focus-visible:outline-(--signal) focus-visible:outline-offset-2",
        // On state
        "data-checked:bg-(--signal) data-checked:border-(--signal)",
        // Disabled
        "disabled:pointer-events-none disabled:opacity-50",
        // Off-hover suggests interactivity
        "hover:border-border-strong data-checked:hover:bg-(--signal)/90",
        className
      )}
      {...props}
    >
      <SwitchPrimitive.Thumb
        data-slot="switch-thumb"
        className={cn(
          "pointer-events-none block size-3 rounded-full bg-foreground",
          "translate-x-[2px]",
          "transition-transform duration-(--duration-fast) ease-(--ease-out)",
          // On state — thumb travels, becomes near-black against the
          // signal-amber track.
          "data-checked:translate-x-[14px] data-checked:bg-(--signal-foreground)"
        )}
      />
    </SwitchPrimitive.Root>
  )
}

export { Switch }
