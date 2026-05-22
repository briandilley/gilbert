/**
 * PromptEditorDialog — full-size editor for AI-prompt config fields.
 *
 * AI-prompt fields can run to many hundreds of lines; inline-textarea
 * editing eats vertical space in dense forms and makes it easy to
 * miss the rest of the section's controls. This dialog gives the
 * prompt the whole modal width + height instead, with the existing
 * AuthorPromptDialog "rewrite with AI" flow reachable inside it.
 *
 * Apply pushes the new text back to the parent via ``onApply`` — the
 * user still presses the section's Save (or page-level Save all) to
 * persist.
 */

import { useEffect, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { SparklesIcon } from "lucide-react";
import { AuthorPromptDialog } from "./AuthorPromptDialog";

interface PromptEditorDialogProps {
  open: boolean;
  onClose: () => void;
  namespace: string;
  paramKey: string;
  paramLabel: string;
  description?: string;
  currentText: string;
  onApply: (newText: string) => void;
}

export function PromptEditorDialog({
  open,
  onClose,
  namespace,
  paramKey,
  paramLabel,
  description,
  currentText,
  onApply,
}: PromptEditorDialogProps) {
  const [draft, setDraft] = useState(currentText);
  const [authorOpen, setAuthorOpen] = useState(false);

  // Reset draft when the dialog opens or the underlying text changes
  // (e.g. the parent reset the field while the dialog was closed).
  useEffect(() => {
    if (open) setDraft(currentText);
  }, [open, currentText]);

  const dirty = draft !== currentText;
  const applyAndClose = () => {
    if (dirty) onApply(draft);
    onClose();
  };

  return (
    <>
      <Dialog
        open={open}
        // Don't auto-dismiss on outside click — long-form editing
        // shouldn't lose work on a misclick. Escape and the close X
        // / Cancel button still work.
        disablePointerDismissal
        onOpenChange={(o) => {
          if (!o) onClose();
        }}
      >
        <DialogContent className="sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle>{paramLabel}</DialogTitle>
            {description ? (
              <DialogDescription>{description}</DialogDescription>
            ) : null}
          </DialogHeader>

          <Textarea
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            className="min-h-[55vh] max-h-[55vh] font-mono text-xs leading-relaxed"
            spellCheck={false}
          />

          <DialogFooter>
            <Button
              variant="outline"
              size="sm"
              className="sm:mr-auto"
              onClick={() => setAuthorOpen(true)}
              title="Rewrite this prompt with AI assistance"
            >
              <SparklesIcon />
              Author with AI
            </Button>
            <Button variant="outline" size="sm" onClick={onClose}>
              Cancel
            </Button>
            <Button size="sm" disabled={!dirty} onClick={applyAndClose}>
              Apply
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AuthorPromptDialog
        open={authorOpen}
        onClose={() => setAuthorOpen(false)}
        namespace={namespace}
        paramKey={paramKey}
        paramLabel={paramLabel}
        currentText={draft}
        onApply={(newText) => {
          setDraft(newText);
          setAuthorOpen(false);
        }}
      />
    </>
  );
}
