/**
 * ConfigField — renders the appropriate form control for a ConfigParam.
 *
 * Type → Control mapping:
 *   string + choices   → Select dropdown
 *   string + sensitive → password input with reveal toggle
 *   string + multiline → textarea
 *   string             → text input
 *   integer / number   → number input
 *   boolean            → toggle switch
 *   array              → tag-style comma input
 *   object             → key-value pair editor
 */

import { useState } from "react";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { EyeIcon, EyeOffIcon, RotateCcwIcon, PlusIcon, XIcon } from "lucide-react";
import type { ConfigParamMeta } from "@/types/config";

interface ConfigFieldProps {
  param: ConfigParamMeta;
  value: unknown;
  onChange: (key: string, value: unknown) => void;
}

function humanize(key: string): string {
  // Strip settings. prefix for display
  const bare = key.replace(/^settings\./, "");
  return bare
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function ConfigField({ param, value, onChange }: ConfigFieldProps) {
  const [showPassword, setShowPassword] = useState(false);

  const handleReset = () => onChange(param.key, param.default);

  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2">
        <Label htmlFor={param.key} className="text-sm font-medium">
          {humanize(param.key)}
        </Label>
        {param.restart_required && (
          <Badge variant="outline" className="text-[10px] px-1.5 py-0 text-amber-500 border-amber-500/40">
            restart
          </Badge>
        )}
        <Button
          variant="ghost"
          size="sm"
          className="h-5 w-5 p-0 ml-auto opacity-40 hover:opacity-100"
          onClick={handleReset}
          title="Reset to default"
        >
          <RotateCcwIcon className="size-3" />
        </Button>
      </div>

      <FieldControl
        param={param}
        value={value}
        onChange={onChange}
        showPassword={showPassword}
        setShowPassword={setShowPassword}
      />

      <p className="text-xs text-muted-foreground">{param.description}</p>
    </div>
  );
}

function FieldControl({
  param,
  value,
  onChange,
  showPassword,
  setShowPassword,
}: {
  param: ConfigParamMeta;
  value: unknown;
  onChange: (key: string, value: unknown) => void;
  showPassword: boolean;
  setShowPassword: (v: boolean) => void;
}) {
  // Boolean → toggle switch
  if (param.type === "boolean") {
    const checked = value === true || value === "true";
    return (
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${checked ? "bg-primary" : "bg-muted"}`}
        onClick={() => onChange(param.key, !checked)}
      >
        <span
          className={`pointer-events-none block h-5 w-5 rounded-full bg-background shadow-lg ring-0 transition-transform ${checked ? "translate-x-5" : "translate-x-0"}`}
        />
      </button>
    );
  }

  // String + choices → dropdown
  if (param.type === "string" && param.choices && param.choices.length > 0) {
    return (
      <Select
        value={String(value ?? "")}
        onValueChange={(v) => onChange(param.key, v)}
      >
        <SelectTrigger className="w-full">
          <SelectValue placeholder="Select..." />
        </SelectTrigger>
        <SelectContent>
          {param.choices.map((choice) => (
            <SelectItem key={choice} value={choice}>{choice}</SelectItem>
          ))}
        </SelectContent>
      </Select>
    );
  }

  // String + multiline → textarea (even if sensitive)
  if (param.type === "string" && param.multiline) {
    return (
      <Textarea
        id={param.key}
        value={String(value ?? "")}
        onChange={(e) => onChange(param.key, e.target.value)}
        className="min-h-[80px] font-mono text-xs"
      />
    );
  }

  // String + sensitive → password with reveal toggle
  if (param.type === "string" && param.sensitive) {
    return (
      <div className="relative">
        <Input
          id={param.key}
          type={showPassword ? "text" : "password"}
          value={String(value ?? "")}
          onChange={(e) => onChange(param.key, e.target.value)}
          className="pr-10 font-mono text-xs"
        />
        <Button
          variant="ghost"
          size="sm"
          className="absolute right-1 top-1/2 -translate-y-1/2 h-7 w-7 p-0"
          onClick={() => setShowPassword(!showPassword)}
        >
          {showPassword ? <EyeOffIcon className="size-3.5" /> : <EyeIcon className="size-3.5" />}
        </Button>
      </div>
    );
  }

  // Plain string
  if (param.type === "string") {
    return (
      <Input
        id={param.key}
        type="text"
        value={String(value ?? "")}
        onChange={(e) => onChange(param.key, e.target.value)}
      />
    );
  }

  // Number / integer
  if (param.type === "integer" || param.type === "number") {
    return (
      <Input
        id={param.key}
        type="number"
        step={param.type === "number" ? "any" : "1"}
        value={value != null ? String(value) : ""}
        onChange={(e) => {
          const v = e.target.value;
          if (v === "") {
            onChange(param.key, param.default);
          } else {
            onChange(param.key, param.type === "integer" ? parseInt(v, 10) : parseFloat(v));
          }
        }}
      />
    );
  }

  // Array + choices → checkbox multi-select
  if (param.type === "array" && param.choices && param.choices.length > 0) {
    return <CheckboxMultiSelect param={param} value={value} onChange={onChange} />;
  }

  // Array → tag input
  if (param.type === "array") {
    return <ArrayField param={param} value={value} onChange={onChange} />;
  }

  // Object → key-value pair editor
  if (param.type === "object") {
    return <KeyValueField param={param} value={value} onChange={onChange} />;
  }

  // Fallback
  return (
    <Input
      id={param.key}
      type="text"
      value={String(value ?? "")}
      onChange={(e) => onChange(param.key, e.target.value)}
    />
  );
}

/** Tag-style array editor with add/remove chips. */
function ArrayField({
  param,
  value,
  onChange,
}: {
  param: ConfigParamMeta;
  value: unknown;
  onChange: (key: string, value: unknown) => void;
}) {
  const [input, setInput] = useState("");
  const arr = Array.isArray(value) ? value : [];

  const add = () => {
    const trimmed = input.trim();
    if (trimmed && !arr.includes(trimmed)) {
      onChange(param.key, [...arr, trimmed]);
      setInput("");
    }
  };

  const remove = (idx: number) => {
    onChange(param.key, arr.filter((_, i) => i !== idx));
  };

  return (
    <div className="space-y-2">
      {arr.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {arr.map((item, idx) => (
            <span
              key={idx}
              className="inline-flex items-center gap-1 rounded-md bg-muted px-2 py-0.5 text-xs"
            >
              {typeof item === "string" ? item : JSON.stringify(item)}
              <button type="button" onClick={() => remove(idx)} className="text-muted-foreground hover:text-foreground">
                <XIcon className="size-3" />
              </button>
            </span>
          ))}
        </div>
      )}
      <div className="flex gap-1.5">
        <Input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); add(); } }}
          placeholder="Add item..."
          className="text-sm"
        />
        <Button variant="outline" size="sm" onClick={add} disabled={!input.trim()}>
          <PlusIcon className="size-3.5" />
        </Button>
      </div>
    </div>
  );
}

/** Key-value pair editor for object types. */
function KeyValueField({
  param,
  value,
  onChange,
}: {
  param: ConfigParamMeta;
  value: unknown;
  onChange: (key: string, value: unknown) => void;
}) {
  const [newKey, setNewKey] = useState("");
  const [newVal, setNewVal] = useState("");
  const obj = (typeof value === "object" && value !== null && !Array.isArray(value))
    ? value as Record<string, unknown>
    : {};

  const entries = Object.entries(obj);

  const add = () => {
    const k = newKey.trim();
    if (k) {
      // Try to parse as JSON for nested objects, otherwise use string
      let v: unknown = newVal;
      try { v = JSON.parse(newVal); } catch { /* keep as string */ }
      onChange(param.key, { ...obj, [k]: v });
      setNewKey("");
      setNewVal("");
    }
  };

  const remove = (k: string) => {
    const next = { ...obj };
    delete next[k];
    onChange(param.key, next);
  };

  const renderValue = (v: unknown): string => {
    if (typeof v === "string") return v;
    return JSON.stringify(v);
  };

  return (
    <div className="space-y-2">
      {entries.length > 0 && (
        <div className="space-y-1">
          {entries.map(([k, v]) => (
            <div key={k} className="flex items-center gap-2 rounded bg-muted/50 px-2 py-1 text-xs">
              <span className="font-medium min-w-[80px]">{k}</span>
              <span className="text-muted-foreground truncate flex-1">{renderValue(v)}</span>
              <button type="button" onClick={() => remove(k)} className="text-muted-foreground hover:text-foreground shrink-0">
                <XIcon className="size-3" />
              </button>
            </div>
          ))}
        </div>
      )}
      <div className="flex gap-1.5">
        <Input
          type="text"
          value={newKey}
          onChange={(e) => setNewKey(e.target.value)}
          placeholder="Key"
          className="text-sm w-1/3"
        />
        <Input
          type="text"
          value={newVal}
          onChange={(e) => setNewVal(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); add(); } }}
          placeholder="Value"
          className="text-sm flex-1"
        />
        <Button variant="outline" size="sm" onClick={add} disabled={!newKey.trim()}>
          <PlusIcon className="size-3.5" />
        </Button>
      </div>
    </div>
  );
}

/** Checkbox multi-select for arrays with known choices. */
function CheckboxMultiSelect({
  param,
  value,
  onChange,
}: {
  param: ConfigParamMeta;
  value: unknown;
  onChange: (key: string, value: unknown) => void;
}) {
  const selected = new Set(Array.isArray(value) ? (value as string[]) : []);
  const choices = param.choices ?? [];

  const toggle = (item: string) => {
    const next = new Set(selected);
    if (next.has(item)) {
      next.delete(item);
    } else {
      next.add(item);
    }
    onChange(param.key, [...next]);
  };

  if (choices.length === 0) {
    return <p className="text-xs text-muted-foreground italic">No options available</p>;
  }

  return (
    <div className="space-y-1.5">
      {choices.map((item) => (
        <label key={item} className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={selected.has(item)}
            onChange={() => toggle(item)}
            className="rounded border-input h-4 w-4 accent-primary"
          />
          <span className="text-sm">{item}</span>
        </label>
      ))}
    </div>
  );
}
