export interface UIOption {
  value: string;
  label: string;
  selected?: boolean;
}

export interface UIElement {
  type:
    | "text"
    | "textarea"
    | "select"
    | "radio"
    | "checkbox"
    | "range"
    | "buttons"
    | "label"
    | "separator";
  name?: string;
  label?: string;
  placeholder?: string;
  default?: unknown;
  required?: boolean;
  options?: UIOption[];
  min?: number;
  max?: number;
  step?: number;
  rows?: number;
}

export interface UIBlock {
  block_type: "form";
  block_id: string;
  title?: string;
  elements: UIElement[];
  submit_label?: string;
  tool_name?: string;
  for_user?: string;
  exclude_user?: string;
  submitted?: boolean;
  submission?: Record<string, unknown>;
}
