import { Button as ButtonPrimitive } from "@base-ui/react/button"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

/**
 * Button — the principal interactive primitive.
 *
 * Variants are picked by *semantic intent*, not by what they look
 * like. In a typical admin screen:
 *
 *   - One ``default`` button max — the "save / apply / confirm"
 *     action that resolves the screen. It's the loudest visual.
 *   - ``outline`` is the workhorse. Most buttons should be outline:
 *     hairline border, neutral interior, fills on hover.
 *   - ``ghost`` for compact icon-only actions inside a list row or
 *     table cell, where even an outlined button would feel heavy.
 *   - ``secondary`` for the "second-most-important" action that
 *     still wants weight — typically grouped left of a ``default``
 *     (e.g. a Cancel/Save pair where Cancel keeps user state).
 *   - ``destructive`` is intentionally restrained — destructive
 *     OUTLINE with destructive text, never a fully filled red.
 *     Confirm dialogs add a clearer warning around the destructive
 *     button rather than making the button itself shout.
 *   - ``link`` is inline-text-flavored. Use sparingly.
 *
 * Density: default height is 28px. Use ``sm`` (24px) inside dense
 * tables / list rows where stacking matters. Use ``lg`` (32px) only
 * for the single most prominent action on a page, paired with body
 * text large enough that the button doesn't tower over it.
 */
const buttonVariants = cva(
  [
    "group/button inline-flex shrink-0 items-center justify-center gap-1.5",
    "rounded-md border border-transparent whitespace-nowrap",
    "text-sm font-medium leading-none",
    "transition-[background-color,border-color,color,opacity] duration-(--duration-fast) ease-(--ease-out)",
    "outline-none select-none",
    "focus-visible:outline-2 focus-visible:outline-(--signal) focus-visible:outline-offset-1",
    "disabled:pointer-events-none disabled:opacity-40",
    "aria-invalid:border-destructive aria-invalid:ring-2 aria-invalid:ring-destructive/30",
    "[&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-3.5",
  ].join(" "),
  {
    variants: {
      variant: {
        default: [
          "bg-foreground text-background",
          "hover:bg-foreground/90 hover:text-background",
          "aria-expanded:bg-foreground/90",
        ].join(" "),
        outline: [
          "border-border bg-transparent text-foreground",
          "hover:bg-foreground/5 hover:border-border-strong",
          "aria-expanded:bg-foreground/5 aria-expanded:border-border-strong",
          "data-pressed:bg-foreground/10",
        ].join(" "),
        secondary: [
          "bg-foreground/8 text-foreground border-transparent",
          "hover:bg-foreground/12",
          "aria-expanded:bg-foreground/12",
        ].join(" "),
        ghost: [
          "bg-transparent text-foreground/85",
          "hover:bg-foreground/8 hover:text-foreground",
          "aria-expanded:bg-foreground/8 aria-expanded:text-foreground",
        ].join(" "),
        destructive: [
          "border-destructive/40 bg-transparent text-destructive",
          "hover:bg-destructive/10 hover:border-destructive/60",
          "focus-visible:outline-destructive",
        ].join(" "),
        link: [
          "text-(--signal) underline-offset-4",
          "hover:underline decoration-1",
          "focus-visible:underline",
        ].join(" "),
      },
      size: {
        // Sizes are tight by default — admin density. Padding scales
        // separately from height so icon-only buttons stay square.
        default: "h-7 px-2.5 gap-1.5",
        xs: "h-5 px-1.5 text-xs gap-1 [&_svg:not([class*='size-'])]:size-3",
        sm: "h-6 px-2 text-xs gap-1 [&_svg:not([class*='size-'])]:size-3",
        lg: "h-8 px-3 text-sm gap-1.5",
        icon: "size-7",
        "icon-xs": "size-5 [&_svg:not([class*='size-'])]:size-3",
        "icon-sm": "size-6 [&_svg:not([class*='size-'])]:size-3.5",
        "icon-lg": "size-8",
      },
    },
    defaultVariants: {
      // ``default`` (filled) is the default because the bulk of the
      // current codebase relies on it semantically — an unspecified
      // ``<Button>`` is the screen's primary action. New code should
      // prefer ``outline`` or ``ghost`` for non-primary actions
      // (see the doc comment above).
      variant: "default",
      size: "default",
    },
  }
)

function Button({
  className,
  variant,
  size,
  ...props
}: ButtonPrimitive.Props & VariantProps<typeof buttonVariants>) {
  return (
    <ButtonPrimitive
      data-slot="button"
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  )
}

export { Button, buttonVariants }
