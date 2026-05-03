/**
 * AuthorPromptDialog — "Author with AI" modal for AI-prompt config fields.
 *
 * Flow: user types a free-form change request, picks an AI profile, and
 * presses Generate. The backend rewrites the prompt; the dialog then
 * shows a side-by-side diff of the current vs. proposed text. Apply
 * writes the new text back into the parent's local state — the user
 * still presses the section's Save button to persist.
 */

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { SparklesIcon, Loader2Icon } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { DiffView } from "@/components/ui/DiffView";
import { useWsApi } from "@/hooks/useWsApi";

interface AuthorPromptDialogProps {
  open: boolean;
  onClose: () => void;
  namespace: string;
  paramKey: string;
  paramLabel: string;
  currentText: string;
  onApply: (newText: string) => void;
}

export function AuthorPromptDialog({
  open,
  onClose,
  namespace,
  paramKey,
  paramLabel,
  currentText,
  onApply,
}: AuthorPromptDialogProps) {
  const api = useWsApi();
  const [instruction, setInstruction] = useState("");
  const [profile, setProfile] = useState("");
  const [proposed, setProposed] = useState<string | null>(null);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const profilesQuery = useQuery({
    queryKey: ["ai-profiles"],
    queryFn: () => api.listAiProfiles(),
    enabled: open,
    staleTime: 60_000,
  });

  // Reset state each time the dialog opens.
  useEffect(() => {
    if (open) {
      setInstruction("");
      setProposed(null);
      setError(null);
      setGenerating(false);
    }
  }, [open]);

  // Default the profile dropdown to "standard" when available.
  useEffect(() => {
    if (!profile && profilesQuery.data && profilesQuery.data.length > 0) {
      const std = profilesQuery.data.find((p) => p.name === "standard");
      setProfile(std?.name ?? profilesQuery.data[0].name);
    }
  }, [profile, profilesQuery.data]);

  async function handleGenerate() {
    const trimmed = instruction.trim();
    if (!trimmed) return;
    setGenerating(true);
    setError(null);
    try {
      const result = await api.authorPrompt({
        namespace,
        key: paramKey,
        currentText,
        instruction: trimmed,
        aiProfile: profile,
      });
      setProposed(result.new_text);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setGenerating(false);
    }
  }

  function handleApply() {
    if (proposed != null) {
      onApply(proposed);
      onClose();
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <SparklesIcon className="size-4 text-primary" />
            Author "{paramLabel}" with AI
          </DialogTitle>
        </DialogHeader>

        {proposed === null ? (
          <div className="space-y-3">
            <div className="space-y-1.5">
              <Label htmlFor="author-instruction" className="text-sm font-medium">
                What change do you want?
              </Label>
              <Textarea
                id="author-instruction"
                value={instruction}
                onChange={(e) => setInstruction(e.target.value)}
                placeholder="e.g. Make the tone more concise. Drop the section about pricing objections. Add a rule that we never discuss restoration costs."
                className="min-h-[120px] text-sm"
                disabled={generating}
                autoFocus
              />
              <p className="text-xs text-muted-foreground">
                The AI sees the full current prompt and applies your change.
                You'll review the result before anything is saved.
              </p>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="author-profile" className="text-sm font-medium">
                AI profile
              </Label>
              <Select
                value={profile}
                onValueChange={(v) => setProfile(v ?? "")}
                disabled={generating}
              >
                <SelectTrigger id="author-profile" className="w-full">
                  <SelectValue placeholder="Select profile..." />
                </SelectTrigger>
                <SelectContent>
                  {(profilesQuery.data ?? []).map((p) => (
                    <SelectItem key={p.name} value={p.name}>
                      {p.name}
                      {p.description ? ` — ${p.description}` : ""}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {error && (
              <div className="rounded border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-600">
                {error}
              </div>
            )}

            <DialogFooter>
              <Button variant="outline" onClick={onClose} disabled={generating}>
                Cancel
              </Button>
              <Button onClick={handleGenerate} disabled={!instruction.trim() || generating}>
                {generating ? (
                  <>
                    <Loader2Icon className="size-3.5 animate-spin" />
                    Generating...
                  </>
                ) : (
                  <>
                    <SparklesIcon className="size-3.5" />
                    Generate
                  </>
                )}
              </Button>
            </DialogFooter>
          </div>
        ) : (
          <div className="space-y-3">
            <div className="text-xs text-muted-foreground">
              Review the proposed prompt below. <span className="font-medium">Apply</span> writes
              it into the field — you still need to press <span className="font-medium">Save</span>{" "}
              on the settings section to persist.
            </div>

            <DiffView oldText={currentText} newText={proposed} />

            <DialogFooter>
              <Button variant="outline" onClick={() => setProposed(null)}>
                Back
              </Button>
              <Button variant="outline" onClick={onClose}>
                Cancel
              </Button>
              <Button onClick={handleApply}>Apply</Button>
            </DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
