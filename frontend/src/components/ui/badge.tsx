import { mergeProps } from "@base-ui/react/merge-props"
import { useRender } from "@base-ui/react/use-render"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

/**
 * Badge — small, dense, mono-typeset status indicator.
 *
 * Badges have two jobs in this design system:
 *
 *   1. STATE — "running", "queued", "error", "off". The dot variant
 *      is purpose-built for this: a small semantic dot + mono label.
 *      Variants ``active`` / ``pending`` / ``success`` / ``warning``
 *      / ``error`` / ``off`` map to status colors.
 *
 *   2. COUNT / META — "3 conversations", "v1.0.0", "admin". The
 *      ``neutral`` and ``outline`` variants carry this.
 *
 * They are NOT decorative. If a badge isn't communicating one of the
 * above, it shouldn't be a badge — it should be inline text.
 *
 * Color is functional. ``active`` (signal-amber) is reserved for
 * "this is the live/selected state right now"; ``success`` for
 * resolved-OK; ``warning`` and ``error`` for elevations the user
 * should care about. Never use them decoratively.
 */
const badgeVariants = cva(
  [
    // Base: layout + typography common to every variant. Mono is
    // the system's "this is meta/data, not prose" signal — every
    // badge inherits it. Case is up to the caller: write
    // ``RUNNING`` if you want shout-y state, ``3 messages`` if you
    // want a quiet count. Don't bake uppercase into the base — most
    // existing call sites pass mixed-case labels and would render
    // wrong.
    "group/badge inline-flex h-[18px] w-fit shrink-0 items-center gap-1.5",
    "rounded-sm px-1.5",
    "font-mono text-[11px] font-medium tracking-tight leading-none",
    "whitespace-nowrap",
    "border border-transparent",
    "transition-colors duration-(--duration-fast) ease-(--ease-out)",
    "focus-visible:outline-2 focus-visible:outline-(--signal) focus-visible:outline-offset-1",
    "[&_svg]:pointer-events-none [&>svg]:size-2.5!",
  ].join(" "),
  {
    variants: {
      variant: {
        // ── State variants — pair with `dot` for the canonical look ──
        active: "border-(--signal)/30 bg-(--signal)/12 text-(--signal)",
        pending: "border-info/30 bg-info/10 text-info",
        success: "border-success/30 bg-success/10 text-success",
        warning: "border-warning/40 bg-warning/12 text-warning",
        error: "border-destructive/40 bg-destructive/12 text-destructive",
        off: "border-border bg-transparent text-muted-foreground",

        // ── Meta variants — count, version, label ──
        neutral: "border-transparent bg-foreground/8 text-foreground/85",
        outline: "border-border bg-transparent text-foreground/85",

        // ── Legacy aliases — preserved so existing call sites keep
        //   compiling. Map onto the new vocabulary. New code should
        //   pick one of the variants above. ──
        default: "border-transparent bg-foreground text-background",
        secondary: "border-transparent bg-foreground/8 text-foreground/85",
        destructive: "border-destructive/40 bg-destructive/12 text-destructive",
        ghost: "border-transparent bg-transparent text-muted-foreground hover:bg-foreground/5",
        link: "border-transparent bg-transparent text-(--signal) underline-offset-2 hover:underline",
      },
      tone: {
        solid: "",
        // ``tone="quiet"`` collapses the bg fill, leaving only the
        // border + text color. Useful in dense tables where multiple
        // badges in adjacent cells would otherwise stack visually.
        quiet: "bg-transparent",
      },
    },
    defaultVariants: {
      variant: "neutral",
      tone: "solid",
    },
  }
)

interface BadgeProps
  extends useRender.ComponentProps<"span">,
    VariantProps<typeof badgeVariants> {
  /** Show a small semantic dot before the label. Color is inherited
   *  from the variant. Recommended for state variants
   *  (active/pending/success/warning/error/off). */
  dot?: boolean
}

function Badge({
  className,
  variant,
  tone,
  dot,
  render,
  children,
  ...props
}: BadgeProps) {
  return useRender({
    defaultTagName: "span",
    props: mergeProps<"span">(
      {
        className: cn(badgeVariants({ variant, tone }), className),
        children: (
          <>
            {dot && (
              <span
                aria-hidden
                className="inline-block size-1.5 rounded-full bg-current shrink-0"
              />
            )}
            {children}
          </>
        ),
      },
      props
    ),
    render,
    state: {
      slot: "badge",
      variant,
      tone,
    },
  })
}

export { Badge, badgeVariants }
