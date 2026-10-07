export type PromptRole = "system" | "user" | "assistant";
export type PromptPlacement = "before" | "middle" | "after";
export interface RolePlacement {
  role: PromptRole;
  placement: PromptPlacement;
  depth: number | null;
  order: number;
  enabled: boolean;
}
