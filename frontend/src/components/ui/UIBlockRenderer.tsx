import { useState } from "react";
import type { UIBlock, UIElement } from "@/types/ui";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

interface UIBlockRendererProps {
  block: UIBlock;
  onSubmit: (blockId: string, values: Record<string, unknown>) => void;
}

export function UIBlockRenderer({ block, onSubmit }: UIBlockRendererProps) {
  const [values, setValues] = useState<Record<string, unknown>>(() => {
    if (block.submitted && block.submission) return block.submission;
    const defaults: Record<string, unknown> = {};
    for (const el of block.elements) {
      if (el.name && el.default !== undefined) {
        defaults[el.name] = el.default;
      } else if (el.type === "checkbox" && el.name) {
        // Checkbox with options: default to selected options
        if (el.options?.length) {
          defaults[el.name] = el.options
            .filter((o) => o.selected)
            .map((o) => o.value);
        } else {
          defaults[el.name] = false;
        }
      } else if (el.type === "range" && el.name) {
        defaults[el.name] = el.min ?? 0;
      }
    }
    return defaults;
  });

  const disabled = !!block.submitted;

  function setValue(name: string, value: unknown) {
    setValues((prev) => ({ ...prev, [name]: value }));
  }

  function handleSubmit(buttonValue?: string) {
    const submitValues = { ...values };
    if (buttonValue !== undefined) {
      // Find the buttons element and use its name
      const btnEl = block.elements.find((e) => e.type === "buttons");
      if (btnEl?.name) submitValues[btnEl.name] = buttonValue;
    }
    onSubmit(block.block_id, submitValues);
  }

  return (
    <Card className="my-2">
      {block.title && (
        <CardHeader className="pb-3">
          <CardTitle className="text-sm">{block.title}</CardTitle>
        </CardHeader>
      )}
      <CardContent className="space-y-3">
        {block.elements.map((el, i) => (
          <UIElementRenderer
            key={el.name || `el-${i}`}
            element={el}
            value={values[el.name || ""]}
            onChange={(v) => el.name && setValue(el.name, v)}
            disabled={disabled}
            onButtonClick={(v) => handleSubmit(v)}
          />
        ))}

        {/* Show submit button only if there are no "buttons" type elements */}
        {!block.elements.some((e) => e.type === "buttons") && (
          <Button
            onClick={() => handleSubmit()}
            disabled={disabled}
            size="sm"
            className="mt-2"
          >
            {disabled ? "Submitted" : (block.submit_label || "Submit")}
          </Button>
        )}
      </CardContent>
    </Card>
  );
}

interface UIElementRendererProps {
  element: UIElement;
  value: unknown;
  onChange: (value: unknown) => void;
  disabled: boolean;
  onButtonClick: (value: string) => void;
}

function UIElementRenderer({
  element,
  value,
  onChange,
  disabled,
  onButtonClick,
}: UIElementRendererProps) {
  switch (element.type) {
    case "label":
      return (
        <p className="text-sm text-muted-foreground whitespace-pre-wrap">
          {element.label}
        </p>
      );

    case "image": {
      // Thumbnail-scale image for form previews (posters, etc.).
      // Width defaults to 96 px; callers can override via max_width.
      const width = element.max_width && element.max_width > 0
        ? element.max_width
        : 96;
      if (!element.url) return null;
      return (
        <img
          src={element.url}
          alt={element.label || ""}
          style={{ maxWidth: `${width}px`, width: "100%", height: "auto" }}
          className="rounded-sm shadow-sm"
          loading="lazy"
        />
      );
    }

    case "separator":
      return <Separator />;

    case "text":
      return (
        <div className="space-y-1.5">
          {element.label && (
            <Label className="text-xs">{element.label}</Label>
          )}
          <Input
            value={(value as string) || ""}
            onChange={(e) => onChange(e.target.value)}
            placeholder={element.placeholder}
            disabled={disabled}
            required={element.required}
          />
        </div>
      );

    case "textarea":
      return (
        <div className="space-y-1.5">
          {element.label && (
            <Label className="text-xs">{element.label}</Label>
          )}
          <Textarea
            value={(value as string) || ""}
            onChange={(e) => onChange(e.target.value)}
            placeholder={element.placeholder}
            disabled={disabled}
            rows={element.rows || 4}
          />
        </div>
      );

    case "select":
      return (
        <div className="space-y-1.5">
          {element.label && (
            <Label className="text-xs">{element.label}</Label>
          )}
          <Select
            value={(value as string) || ""}
            onValueChange={(v) => onChange(v ?? "")}
            disabled={disabled}
          >
            <SelectTrigger>
              <SelectValue placeholder={element.placeholder || "Select..."} />
            </SelectTrigger>
            <SelectContent>
              {element.options?.map((opt) => (
                <SelectItem key={opt.value} value={opt.value}>
                  {opt.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      );

    case "radio":
      return (
        <div className="space-y-1.5">
          {element.label && (
            <Label className="text-xs">{element.label}</Label>
          )}
          <div className="space-y-1">
            {element.options?.map((opt) => (
              <label
                key={opt.value}
                className="flex items-center gap-2 text-sm cursor-pointer"
              >
                <input
                  type="radio"
                  name={element.name}
                  value={opt.value}
                  checked={value === opt.value}
                  onChange={() => onChange(opt.value)}
                  disabled={disabled}
                  className="accent-primary"
                />
                {opt.label}
              </label>
            ))}
          </div>
        </div>
      );

    case "checkbox":
      if (element.options?.length) {
        const selected = (value as string[]) || [];
        return (
          <div className="space-y-1.5">
            {element.label && (
              <Label className="text-xs">{element.label}</Label>
            )}
            <div className="space-y-1">
              {element.options.map((opt) => (
                <label
                  key={opt.value}
                  className="flex items-center gap-2 text-sm cursor-pointer"
                >
                  <input
                    type="checkbox"
                    checked={selected.includes(opt.value)}
                    onChange={(e) => {
                      if (e.target.checked) {
                        onChange([...selected, opt.value]);
                      } else {
                        onChange(selected.filter((v) => v !== opt.value));
                      }
                    }}
                    disabled={disabled}
                    className="accent-primary"
                  />
                  {opt.label}
                </label>
              ))}
            </div>
          </div>
        );
      }
      return (
        <label className="flex items-center gap-2 text-sm cursor-pointer">
          <input
            type="checkbox"
            checked={!!value}
            onChange={(e) => onChange(e.target.checked)}
            disabled={disabled}
            className="accent-primary"
          />
          {element.label}
        </label>
      );

    case "range":
      return (
        <div className="space-y-1.5">
          {element.label && (
            <Label className="text-xs">
              {element.label}: {String(value)}
            </Label>
          )}
          <input
            type="range"
            min={element.min ?? 0}
            max={element.max ?? 100}
            step={element.step ?? 1}
            value={Number(value) || 0}
            onChange={(e) => onChange(Number(e.target.value))}
            disabled={disabled}
            className="w-full accent-primary"
          />
        </div>
      );

    case "buttons":
      return (
        <div className="flex flex-wrap gap-2">
          {element.options?.map((opt) => (
            <Button
              key={opt.value}
              variant="secondary"
              size="sm"
              disabled={disabled}
              onClick={() => onButtonClick(opt.value)}
            >
              {opt.label}
            </Button>
          ))}
        </div>
      );

    default:
      return null;
  }
}
