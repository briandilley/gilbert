import * as React from "react"
import { Input as InputPrimitive } from "@base-ui/react/input"

import { cn } from "@/lib/utils"

/**
 * Input — the text-entry primitive.
 *
 * Hairline-bordered, transparent fill. Focus is communicated by an
 * outline + a border-color shift rather than by a heavy ring — the
 * ring style is for buttons. Inputs are dense (h-7 default) to match
 * the rest of the system.
 *
 * Pass ``mono`` to render in monospace. Use it for any field whose
 * value is technical content — IDs, paths, hostnames, semver, ports,
 * API keys. The monospace marker is part of the design language;
 * don't reach for it casually.
 *
 * Sensitive secrets should pair this with a reveal toggle rendered
 * outside the primitive (e.g., in `ConfigField`). Inputs themselves
 * don't carry the visibility-toggle UI — that's policy, not styling.
 */
interface InputProps extends Omit<React.ComponentProps<"input">, "size"> {
  /** Render the field in the monospace family. Use for technical
   *  content (IDs, paths, hostnames, version strings). */
  mono?: boolean
  /** Visual size. ``lg`` is reserved for search inputs and the
   *  single hero field on an auth screen — most fields are
   *  ``default``. (The HTML ``size`` attribute is omitted; if you
   *  need it for character-count autosizing, pass via
   *  ``inputMode``/``maxLength`` or wrap.) */
  size?: "default" | "lg"
}

function Input({ className, type, mono, size = "default", ...props }: InputProps) {
  return (
    <InputPrimitive
      type={type}
      data-slot="input"
      data-mono={mono ? "true" : undefined}
      data-size={size}
      className={cn(
        // Box
        "w-full min-w-0 rounded-md border border-input bg-transparent",
        "px-2.5 py-1",
        // Size
        size === "default" ? "h-7 text-sm" : "h-8 text-sm",
        // Typography
        mono
          ? "font-mono text-[12.5px] tracking-tight"
          : "font-sans",
        // Behavior
        "transition-[border-color,background-color] duration-(--duration-fast) ease-(--ease-out)",
        "outline-none",
        // File input (rarely used here but supported).
        "file:inline-flex file:h-5 file:border-0 file:bg-transparent file:text-xs file:font-medium file:text-foreground",
        // Placeholder
        "placeholder:text-muted-foreground placeholder:font-sans",
        // Hover — subtle border darken to invite focus.
        "hover:border-border-strong",
        // Focus — outline ring (system rule), border tightens.
        "focus-visible:border-(--signal)/40 focus-visible:outline-2 focus-visible:outline-(--signal) focus-visible:outline-offset-1",
        // Disabled
        "disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50",
        // Invalid
        "aria-invalid:border-destructive/60 aria-invalid:focus-visible:outline-destructive",
        className
      )}
      {...props}
    />
  )
}

export { Input }
export type { InputProps }
