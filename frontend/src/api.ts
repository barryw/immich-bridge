export type AdminUser = {
  id: string;
  email?: string | null;
  name?: string | null;
  api_key_name?: string | null;
};

export type AdminSession = {
  authenticated: boolean;
  user?: AdminUser | null;
  expires_at?: string | null;
  session_token?: string | null;
};

export type ViewFilters = {
  album_ids: string[];
  person_ids: string[];
  tag_ids: string[];
  is_favorite: boolean | null;
  media_type: "IMAGE" | "VIDEO" | null;
  taken_after: string | null;
  taken_before: string | null;
  rating: number | null;
  original_file_name: string | null;
  ocr: string | null;
  city: string | null;
  country: string | null;
};

export type SavedView = {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  layout: "date_buckets" | "flat";
  filters: ViewFilters;
  created_at: string;
  updated_at: string;
  match_count?: number | null;
};

export type SavedViewPayload = Omit<SavedView, "id" | "created_at" | "updated_at" | "match_count">;

export type OptionItem = {
  id: string;
  name: string;
  color?: string | null;
  asset_count?: number | null;
  hidden?: boolean | null;
};

export type OptionsResponse = {
  items: OptionItem[];
};

export type MountSettings = {
  albums_enabled: boolean;
  timeline_enabled: boolean;
  favorites_enabled: boolean;
  views_enabled: boolean;
  tags_enabled: boolean;
  people_enabled: boolean;
  album_folder_split_threshold: number;
  day_folder_split_threshold: number;
  filename_mode: "date-original-id" | "original" | "stable";
};

export type WritePolicy = {
  root_uploads: boolean;
  album_uploads: boolean;
  album_create: boolean;
  album_membership_delete: boolean;
  permanent_delete: boolean;
  move_copy: boolean;
  overwrite: boolean;
};

export type Diagnostics = {
  immich_url: string;
  database_path: string;
  redis_enabled: boolean;
  metrics_enabled: boolean;
  webdav_port: number;
  admin_port: number;
  view_count: number;
  mount: MountSettings;
  write_policy: WritePolicy;
};

export const emptyFilters: ViewFilters = {
  album_ids: [],
  person_ids: [],
  tag_ids: [],
  is_favorite: null,
  media_type: null,
  taken_after: null,
  taken_before: null,
  rating: null,
  original_file_name: null,
  ocr: null,
  city: null,
  country: null
};

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(init.headers ?? {})
    },
    ...init
  });

  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload.detail) {
        message = payload.detail;
      }
    } catch {
      // Keep HTTP status text.
    }
    throw new Error(message);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export const api = {
  login: (username: string, apiKey: string) =>
    request<AdminSession>("/api/admin/session", {
      method: "POST",
      body: JSON.stringify({ username, api_key: apiKey })
    }),
  session: () => request<AdminSession>("/api/admin/session"),
  logout: () => request<void>("/api/admin/session", { method: "DELETE" }),
  views: () => request<{ views: SavedView[] }>("/api/admin/views"),
  createView: (view: SavedViewPayload) =>
    request<SavedView>("/api/admin/views", {
      method: "POST",
      body: JSON.stringify(view)
    }),
  updateView: (id: string, view: SavedViewPayload) =>
    request<SavedView>(`/api/admin/views/${id}`, {
      method: "PUT",
      body: JSON.stringify(view)
    }),
  deleteView: (id: string) => request<void>(`/api/admin/views/${id}`, { method: "DELETE" }),
  matchCount: (filters: ViewFilters) =>
    request<{ count: number | null }>("/api/admin/views/match-count", {
      method: "POST",
      body: JSON.stringify({ filters })
    }),
  tagOptions: () => request<OptionsResponse>("/api/admin/options/tags"),
  peopleOptions: () => request<OptionsResponse>("/api/admin/options/people"),
  mount: () => request<MountSettings>("/api/admin/mount"),
  updateMount: (mount: MountSettings) =>
    request<MountSettings>("/api/admin/mount", {
      method: "PUT",
      body: JSON.stringify(mount)
    }),
  writePolicy: () => request<WritePolicy>("/api/admin/write-policy"),
  updateWritePolicy: (policy: WritePolicy) =>
    request<WritePolicy>("/api/admin/write-policy", {
      method: "PUT",
      body: JSON.stringify(policy)
    }),
  diagnostics: () => request<Diagnostics>("/api/admin/diagnostics")
};
