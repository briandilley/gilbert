export interface CollectionGroup {
  namespace: string;
  collections: CollectionInfo[];
}

export interface CollectionInfo {
  name: string;
  short_name: string;
  count: number;
}

export interface CollectionData {
  collection: string;
  entities: Record<string, unknown>[];
  total: number;
  page: number;
  total_pages: number;
  sortable_fields: string[];
  fk_map: Record<string, string>;
  display_columns: string[];
}

export interface EntityData {
  collection: string;
  entity_id: string;
  entity: Record<string, unknown>;
  fk_map: Record<string, string>;
}
