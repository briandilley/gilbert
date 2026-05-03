/**
 * DiffView — side-by-side line-level diff between two strings.
 *
 * Uses an LCS table to align unchanged lines; insertions appear only on
 * the right column, deletions only on the left. Lightweight enough to
 * render in a modal without an external diff dependency. For prompt-
 * sized inputs (a few hundred lines) the O(n*m) LCS is fine.
 */

import { useMemo } from "react";
import { cn } from "@/lib/utils";

interface DiffViewProps {
  oldText: string;
  newText: string;
  className?: string;
}

interface DiffRow {
  kind: "equal" | "added" | "removed";
  left: string | null;
  right: string | null;
  /** Stable key for React. */
  key: string;
}

function computeRows(oldText: string, newText: string): DiffRow[] {
  const a = oldText.split("\n");
  const b = newText.split("\n");
  const n = a.length;
  const m = b.length;

  // LCS length table.
  const lcs: number[][] = Array.from({ length: n + 1 }, () =>
    new Array<number>(m + 1).fill(0),
  );
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      if (a[i] === b[j]) {
        lcs[i][j] = lcs[i + 1][j + 1] + 1;
      } else {
        lcs[i][j] = Math.max(lcs[i + 1][j], lcs[i][j + 1]);
      }
    }
  }

  // Walk forward producing edit ops.
  const rows: DiffRow[] = [];
  let i = 0;
  let j = 0;
  let counter = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      rows.push({ kind: "equal", left: a[i], right: b[j], key: `e${counter++}` });
      i++;
      j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      rows.push({ kind: "removed", left: a[i], right: null, key: `r${counter++}` });
      i++;
    } else {
      rows.push({ kind: "added", left: null, right: b[j], key: `a${counter++}` });
      j++;
    }
  }
  while (i < n) {
    rows.push({ kind: "removed", left: a[i++], right: null, key: `r${counter++}` });
  }
  while (j < m) {
    rows.push({ kind: "added", left: null, right: b[j++], key: `a${counter++}` });
  }
  return rows;
}

export function DiffView({ oldText, newText, className }: DiffViewProps) {
  const rows = useMemo(() => computeRows(oldText, newText), [oldText, newText]);

  return (
    <div
      className={cn(
        "rounded-md border bg-background font-mono text-[11px] leading-relaxed",
        className,
      )}
    >
      <div className="grid grid-cols-2 divide-x">
        <div className="px-2 py-1 text-[10px] uppercase tracking-wider text-muted-foreground border-b">
          Current
        </div>
        <div className="px-2 py-1 text-[10px] uppercase tracking-wider text-muted-foreground border-b">
          Proposed
        </div>
      </div>
      <div className="grid grid-cols-2 divide-x max-h-[60vh] overflow-auto">
        <DiffColumn rows={rows} side="left" />
        <DiffColumn rows={rows} side="right" />
      </div>
    </div>
  );
}

function DiffColumn({
  rows,
  side,
}: {
  rows: DiffRow[];
  side: "left" | "right";
}) {
  return (
    <div className="min-w-0">
      {rows.map((row) => {
        const text = side === "left" ? row.left : row.right;
        const isMissing = text === null;
        const highlight =
          side === "left" && row.kind === "removed"
            ? "bg-rose-500/15"
            : side === "right" && row.kind === "added"
              ? "bg-emerald-500/15"
              : "";
        const marker =
          isMissing
            ? ""
            : side === "left" && row.kind === "removed"
              ? "-"
              : side === "right" && row.kind === "added"
                ? "+"
                : " ";
        return (
          <div
            key={row.key}
            className={cn(
              "flex items-start gap-2 px-2 py-px whitespace-pre-wrap break-words min-h-[1.2em]",
              highlight,
              isMissing && "bg-muted/30",
            )}
          >
            <span className="select-none text-muted-foreground w-3 shrink-0">
              {marker}
            </span>
            <span className="min-w-0 flex-1">{text ?? ""}</span>
          </div>
        );
      })}
    </div>
  );
}
