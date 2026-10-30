export interface ExpertSelectionRecord {
  definition_id: string;
  enabled: boolean;
  valid: boolean;
  definition?: {
    name?: string;
    description?: string;
  } | null;
}

export interface SelectableExpert {
  id: string;
  name: string;
  description: string;
}

export function resolveExpertLabel(
  expert: Pick<SelectableExpert, "id" | "name">,
): string {
  return expert.name.trim() || expert.id;
}

export function normalizeSelectableExperts(
  records: ExpertSelectionRecord[],
): SelectableExpert[] {
  return records
    .filter((record) => record.enabled && record.valid && record.definition)
    .map((record) => ({
      id: record.definition_id,
      name: resolveExpertLabel({
        id: record.definition_id,
        name: record.definition?.name || "",
      }),
      description: record.definition?.description || "",
    }))
    .sort((left, right) => left.name.localeCompare(right.name, "zh-CN"));
}
