/**
 * EditProfileLink — "Edit profile" link for any AI profile picker.
 *
 * Opens ``AIProfileEditorDialog`` for the selected profile, so a profile
 * can be changed in place (model, tools) without leaving the page that
 * uses it. Renders nothing when no profile is selected.
 */

import { useState } from "react";
import { PencilIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { AIProfileEditorDialog } from "./AIProfileEditorDialog";

interface Props {
  profileName: string;
  /** Called after a save, e.g. to refetch data that shows the profile. */
  onSaved?: (name: string) => void;
  className?: string;
}

export function EditProfileLink({ profileName, onSaved, className }: Props) {
  const [open, setOpen] = useState(false);
  if (!profileName) return null;
  return (
    <>
      <Button
        type="button"
        variant="link"
        size="xs"
        className={className ?? "h-auto px-0 text-xs"}
        title={`Edit the "${profileName}" AI profile`}
        onClick={() => setOpen(true)}
      >
        <PencilIcon />
        Edit profile
      </Button>
      <AIProfileEditorDialog
        open={open}
        profileName={profileName}
        onClose={() => setOpen(false)}
        onSaved={onSaved}
      />
    </>
  );
}
