import * as React from "react"

import { cn } from "@/lib/utils"

/**
 * PageHeader — the canonical top-of-page layout block.
 *
 * Encodes the spec's page-header pattern so every route doesn't
 * reinvent it. Five slots (all optional except the title):
 *
 *   <PageHeader
 *     eyebrow="SETTINGS"
 *     title="Mailbox"
 *     description="Inbound mail addressed to this user goes here."
 *     actions={<><Button variant="outline" size="sm">Cancel</Button>
 *               <Button size="sm">Save</Button></>}
 *   >
 *     <SearchInput />        — optional tools row below the title
 *   </PageHeader>
 *
 * Layout: 24px top padding, 16px between title and the hairline.
 * Actions are right-aligned and vertically centered against the
 * title row. ``children`` renders below the title/actions row, above
 * the hairline — useful for a search/filter strip that belongs with
 * the header rather than the content.
 */

interface PageHeaderProps extends Omit<React.ComponentProps<"div">, "title"> {
  /** Small uppercase mono label above the title. Encodes context
   *  ("MCP", "INBOX", "SECURITY") for pages that benefit from a
   *  category label. Often omitted. */
  eyebrow?: React.ReactNode
  /** The page title (sentence case, 19px). Required. */
  title: React.ReactNode
  /** Short description below the title (12px muted). Two lines max
   *  in practice — if you need more, link to docs instead. */
  description?: React.ReactNode
  /** Right-aligned action cluster. Pass a fragment of buttons. */
  actions?: React.ReactNode
}

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
  className,
  children,
  ...props
}: PageHeaderProps) {
  return (
    <div
      data-slot="page-header"
      className={cn(
        "border-b border-border px-6 pt-6 pb-4",
        className
      )}
      {...props}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          {eyebrow ? <PageHeaderEyebrow>{eyebrow}</PageHeaderEyebrow> : null}
          <h1 className="text-xl font-semibold leading-tight tracking-[-0.015em] truncate">
            {title}
          </h1>
          {description ? (
            <p className="mt-1 text-xs text-muted-foreground leading-relaxed max-w-prose">
              {description}
            </p>
          ) : null}
        </div>
        {actions ? (
          <div
            data-slot="page-header-actions"
            className="flex shrink-0 items-center gap-1.5"
          >
            {actions}
          </div>
        ) : null}
      </div>
      {children ? (
        <div data-slot="page-header-extra" className="mt-3">
          {children}
        </div>
      ) : null}
    </div>
  )
}

/**
 * Eyebrow — the small uppercase mono label above a page title.
 * Exported standalone so it can be reused inside cards and section
 * headers without re-deriving the styling each time.
 */
export function PageHeaderEyebrow({
  className,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="page-header-eyebrow"
      className={cn(
        "font-mono text-[11px] uppercase tracking-[0.08em] font-medium text-muted-foreground leading-none mb-2",
        className
      )}
      {...props}
    />
  )
}
