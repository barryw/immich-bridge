import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Database,
  FolderTree,
  Gauge,
  KeyRound,
  Lock,
  LogOut,
  Plus,
  Radio,
  Save,
  ShieldCheck,
  Trash2
} from "lucide-react";
import {
  api,
  Diagnostics,
  emptyFilters,
  MountSettings,
  SavedView,
  SavedViewPayload,
  ViewFilters,
  WritePolicy
} from "../api";

type Section = "overview" | "views" | "mount" | "writes" | "diagnostics";

const navItems: Array<{ id: Section; label: string; icon: typeof FolderTree }> = [
  { id: "overview", label: "Overview", icon: Gauge },
  { id: "views", label: "Views", icon: FolderTree },
  { id: "mount", label: "Mount", icon: Database },
  { id: "writes", label: "Writes", icon: ShieldCheck },
  { id: "diagnostics", label: "Diagnostics", icon: Activity }
];

const defaultView: SavedViewPayload = {
  name: "Favorites This Year",
  description: "",
  enabled: true,
  layout: "date_buckets",
  filters: {
    ...emptyFilters,
    is_favorite: true
  }
};

export function App() {
  const [section, setSection] = useState<Section>("overview");
  const queryClient = useQueryClient();
  const session = useQuery({
    queryKey: ["session"],
    queryFn: api.session,
    retry: false
  });

  if (session.isLoading) {
    return <ShellFrame status="checking" />;
  }

  if (session.isError || !session.data?.authenticated) {
    return (
      <ShellFrame status="locked">
        <LoginPanel onLogin={() => queryClient.invalidateQueries({ queryKey: ["session"] })} />
      </ShellFrame>
    );
  }

  return (
    <ShellFrame
      status="online"
      userLabel={session.data.user?.email ?? session.data.user?.name ?? "Immich admin"}
      active={section}
      onNavigate={setSection}
      onLogout={() => {
        api.logout().finally(() => queryClient.invalidateQueries({ queryKey: ["session"] }));
      }}
    >
      <AdminWorkspace section={section} />
    </ShellFrame>
  );
}

function ShellFrame({
  children,
  status,
  userLabel,
  active = "overview",
  onNavigate,
  onLogout
}: {
  children?: React.ReactNode;
  status: "checking" | "locked" | "online";
  userLabel?: string;
  active?: Section;
  onNavigate?: (section: Section) => void;
  onLogout?: () => void;
}) {
  return (
    <div className="console">
      <aside className="rail">
        <div className="brand-lockup">
          <div className="brand-mark">ib</div>
          <div>
            <strong>immich-bridge</strong>
            <span>{status}</span>
          </div>
        </div>

        <nav className="nav-list">
          {navItems.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.id}
                className={active === item.id ? "nav-item active" : "nav-item"}
                disabled={!onNavigate}
                onClick={() => onNavigate?.(item.id)}
                title={item.label}
              >
                <Icon size={18} />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>

        <div className="rail-footer">
          {userLabel && <span className="user-label">{userLabel}</span>}
          {onLogout && (
            <button className="icon-button" onClick={onLogout} title="Sign out">
              <LogOut size={18} />
            </button>
          )}
        </div>
      </aside>

      <main className="workbench">{children ?? <div className="loading-strip">Loading</div>}</main>
    </div>
  );
}

function LoginPanel({ onLogin }: { onLogin: () => void }) {
  const [username, setUsername] = useState("");
  const [apiKey, setApiKey] = useState("");
  const login = useMutation({
    mutationFn: () => api.login(username, apiKey),
    onSuccess: onLogin
  });

  return (
    <section className="login-panel">
      <div className="login-header">
        <KeyRound size={22} />
        <h1>Admin access</h1>
      </div>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          login.mutate();
        }}
      >
        <label>
          Immich identity
          <input
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
          />
        </label>
        <label>
          Immich API key
          <input
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
            type="password"
            autoComplete="current-password"
          />
        </label>
        {login.isError && <div className="form-error">{login.error.message}</div>}
        <button className="primary-button" type="submit" disabled={login.isPending}>
          <Lock size={17} />
          <span>{login.isPending ? "Checking" : "Sign in"}</span>
        </button>
      </form>
    </section>
  );
}

function AdminWorkspace({ section }: { section: Section }) {
  const diagnostics = useQuery({ queryKey: ["diagnostics"], queryFn: api.diagnostics });
  const events = useRealtimeEvents();

  return (
    <>
      <header className="page-header">
        <div>
          <span className="eyebrow">Admin API</span>
          <h1>{titleFor(section)}</h1>
        </div>
        <div className={events.connected ? "live-pill online" : "live-pill"}>
          <Radio size={16} />
          <span>{events.connected ? "live" : "offline"}</span>
        </div>
      </header>

      {section === "overview" && <Overview diagnostics={diagnostics.data} />}
      {section === "views" && <ViewsPanel />}
      {section === "mount" && <MountPanel />}
      {section === "writes" && <WritePolicyPanel />}
      {section === "diagnostics" && <DiagnosticsPanel diagnostics={diagnostics.data} />}
    </>
  );
}

function Overview({ diagnostics }: { diagnostics?: Diagnostics }) {
  const nodes = useMemo(() => {
    const mount = diagnostics?.mount;
    return [
      ["Albums", mount?.albums_enabled],
      ["Timeline", mount?.timeline_enabled],
      ["Favorites", mount?.favorites_enabled],
      ["Views", mount?.views_enabled],
      [".well-known", true]
    ] as const;
  }, [diagnostics]);

  return (
    <section className="overview-grid">
      <div className="mount-map">
        <div className="map-root">/</div>
        <div className="map-branches">
          {nodes.map(([label, enabled]) => (
            <div className={enabled ? "map-node" : "map-node muted"} key={label}>
              <FolderTree size={17} />
              <span>{label}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="status-ledger">
        <LedgerRow label="Immich" value={diagnostics?.immich_url ?? "loading"} />
        <LedgerRow label="Views" value={`${diagnostics?.view_count ?? 0}`} />
        <LedgerRow label="Redis" value={diagnostics?.redis_enabled ? "enabled" : "disabled"} />
        <LedgerRow label="SQLite" value={diagnostics?.database_path ?? "loading"} />
      </div>
    </section>
  );
}

function ViewsPanel() {
  const queryClient = useQueryClient();
  const views = useQuery({ queryKey: ["views"], queryFn: api.views });
  const createView = useMutation({
    mutationFn: api.createView,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["views"] })
  });
  const updateView = useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: SavedViewPayload }) =>
      api.updateView(id, payload),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["views"] })
  });
  const deleteView = useMutation({
    mutationFn: api.deleteView,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["views"] })
  });

  return (
    <section className="views-layout">
      <ViewEditor
        title="New view"
        initial={defaultView}
        pending={createView.isPending}
        onSave={(payload) => createView.mutate(payload)}
      />

      <div className="view-list">
        {(views.data?.views ?? []).map((view) => (
          <ViewCard
            key={view.id}
            view={view}
            onSave={(payload) => updateView.mutate({ id: view.id, payload })}
            onDelete={() => deleteView.mutate(view.id)}
            pending={updateView.isPending || deleteView.isPending}
          />
        ))}
      </div>
    </section>
  );
}

function ViewCard({
  view,
  onSave,
  onDelete,
  pending
}: {
  view: SavedView;
  onSave: (payload: SavedViewPayload) => void;
  onDelete: () => void;
  pending: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <article className="view-card">
      <button className="view-summary" onClick={() => setOpen(!open)}>
        <span>{view.name}</span>
        <code>/Views/{view.name}</code>
      </button>
      {open && (
        <ViewEditor title="Edit view" initial={view} pending={pending} onSave={onSave}>
          <button className="danger-button" onClick={onDelete} disabled={pending} title="Delete">
            <Trash2 size={16} />
            <span>Delete</span>
          </button>
        </ViewEditor>
      )}
    </article>
  );
}

function ViewEditor({
  title,
  initial,
  pending,
  children,
  onSave
}: {
  title: string;
  initial: SavedViewPayload;
  pending: boolean;
  children?: React.ReactNode;
  onSave: (payload: SavedViewPayload) => void;
}) {
  const [draft, setDraft] = useState<SavedViewPayload>(initial);
  useEffect(() => setDraft(initial), [initial]);

  const filters = draft.filters;
  const updateFilter = <K extends keyof ViewFilters>(key: K, value: ViewFilters[K]) => {
    setDraft({ ...draft, filters: { ...filters, [key]: value } });
  };

  return (
    <form
      className="editor-panel"
      onSubmit={(event) => {
        event.preventDefault();
        onSave(draft);
      }}
    >
      <div className="panel-title">
        <h2>{title}</h2>
        <label className="switch-row">
          <input
            type="checkbox"
            checked={draft.enabled}
            onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })}
          />
          enabled
        </label>
      </div>

      <div className="form-grid">
        <label>
          Name
          <input
            value={draft.name}
            onChange={(event) => setDraft({ ...draft, name: event.target.value })}
          />
        </label>
        <label>
          Layout
          <select
            value={draft.layout}
            onChange={(event) =>
              setDraft({ ...draft, layout: event.target.value as SavedViewPayload["layout"] })
            }
          >
            <option value="date_buckets">Date buckets</option>
            <option value="flat">Flat</option>
          </select>
        </label>
        <label>
          Media
          <select
            value={filters.media_type ?? ""}
            onChange={(event) =>
              updateFilter("media_type", (event.target.value || null) as ViewFilters["media_type"])
            }
          >
            <option value="">Any</option>
            <option value="IMAGE">Images</option>
            <option value="VIDEO">Videos</option>
          </select>
        </label>
        <label>
          Rating
          <input
            type="number"
            min={1}
            max={5}
            value={filters.rating ?? ""}
            onChange={(event) =>
              updateFilter("rating", event.target.value ? Number(event.target.value) : null)
            }
          />
        </label>
        <label>
          Tag IDs
          <input
            value={filters.tag_ids.join(", ")}
            onChange={(event) => updateFilter("tag_ids", splitIds(event.target.value))}
          />
        </label>
        <label>
          Person IDs
          <input
            value={filters.person_ids.join(", ")}
            onChange={(event) => updateFilter("person_ids", splitIds(event.target.value))}
          />
        </label>
        <label>
          OCR
          <input
            value={filters.ocr ?? ""}
            onChange={(event) => updateFilter("ocr", event.target.value || null)}
          />
        </label>
        <label>
          Filename
          <input
            value={filters.original_file_name ?? ""}
            onChange={(event) => updateFilter("original_file_name", event.target.value || null)}
          />
        </label>
      </div>

      <div className="panel-actions">
        {children}
        <button className="primary-button" type="submit" disabled={pending}>
          <Save size={16} />
          <span>{pending ? "Saving" : "Save"}</span>
        </button>
      </div>
    </form>
  );
}

function MountPanel() {
  const queryClient = useQueryClient();
  const mount = useQuery({ queryKey: ["mount"], queryFn: api.mount });
  const mutation = useMutation({
    mutationFn: api.updateMount,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["mount"] })
  });
  const [draft, setDraft] = useState<MountSettings | null>(null);
  useEffect(() => {
    if (mount.data) setDraft(mount.data);
  }, [mount.data]);
  if (!draft) return <div className="loading-strip">Loading</div>;

  return (
    <SettingsForm onSubmit={() => mutation.mutate(draft)} pending={mutation.isPending}>
      <ToggleGrid
        values={[
          ["albums_enabled", "Albums"],
          ["timeline_enabled", "Timeline"],
          ["favorites_enabled", "Favorites"],
          ["views_enabled", "Views"],
          ["tags_enabled", "Tags"],
          ["people_enabled", "People"]
        ]}
        draft={draft}
        onChange={(key, value) => setDraft({ ...draft, [key]: value })}
      />
      <div className="form-grid">
        <label>
          Album split
          <input
            type="number"
            value={draft.album_folder_split_threshold}
            onChange={(event) =>
              setDraft({ ...draft, album_folder_split_threshold: Number(event.target.value) })
            }
          />
        </label>
        <label>
          Day split
          <input
            type="number"
            value={draft.day_folder_split_threshold}
            onChange={(event) =>
              setDraft({ ...draft, day_folder_split_threshold: Number(event.target.value) })
            }
          />
        </label>
        <label>
          Filenames
          <select
            value={draft.filename_mode}
            onChange={(event) =>
              setDraft({ ...draft, filename_mode: event.target.value as MountSettings["filename_mode"] })
            }
          >
            <option value="date-original-id">Date original ID</option>
            <option value="original">Original</option>
            <option value="stable">Stable</option>
          </select>
        </label>
      </div>
    </SettingsForm>
  );
}

function WritePolicyPanel() {
  const queryClient = useQueryClient();
  const policy = useQuery({ queryKey: ["write-policy"], queryFn: api.writePolicy });
  const mutation = useMutation({
    mutationFn: api.updateWritePolicy,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["write-policy"] })
  });
  const [draft, setDraft] = useState<WritePolicy | null>(null);
  useEffect(() => {
    if (policy.data) setDraft(policy.data);
  }, [policy.data]);
  if (!draft) return <div className="loading-strip">Loading</div>;

  return (
    <SettingsForm onSubmit={() => mutation.mutate(draft)} pending={mutation.isPending}>
      <ToggleGrid
        values={[
          ["root_uploads", "Root uploads"],
          ["album_uploads", "Album uploads"],
          ["album_create", "Album create"],
          ["album_membership_delete", "Album delete"],
          ["permanent_delete", "Permanent delete"],
          ["move_copy", "Move copy"],
          ["overwrite", "Overwrite"]
        ]}
        draft={draft}
        onChange={(key, value) => setDraft({ ...draft, [key]: value })}
      />
    </SettingsForm>
  );
}

function SettingsForm({
  children,
  pending,
  onSubmit
}: {
  children: React.ReactNode;
  pending: boolean;
  onSubmit: () => void;
}) {
  return (
    <form
      className="settings-panel"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      {children}
      <div className="panel-actions">
        <button className="primary-button" disabled={pending}>
          <Save size={16} />
          <span>{pending ? "Saving" : "Save"}</span>
        </button>
      </div>
    </form>
  );
}

function ToggleGrid<T extends Record<string, unknown>>({
  values,
  draft,
  onChange
}: {
  values: Array<[keyof T, string]>;
  draft: T;
  onChange: (key: keyof T, value: boolean) => void;
}) {
  return (
    <div className="toggle-grid">
      {values.map(([key, label]) => (
        <label className="toggle-tile" key={String(key)}>
          <input
            type="checkbox"
            checked={Boolean(draft[key])}
            onChange={(event) => onChange(key, event.target.checked)}
          />
          <span>{label}</span>
        </label>
      ))}
    </div>
  );
}

function DiagnosticsPanel({ diagnostics }: { diagnostics?: Diagnostics }) {
  return (
    <section className="diagnostics-grid">
      <LedgerRow label="Admin port" value={`${diagnostics?.admin_port ?? ""}`} />
      <LedgerRow label="DAV port" value={`${diagnostics?.webdav_port ?? ""}`} />
      <LedgerRow label="Metrics" value={diagnostics?.metrics_enabled ? "on" : "off"} />
      <LedgerRow label="Redis" value={diagnostics?.redis_enabled ? "on" : "off"} />
      <LedgerRow label="Database" value={diagnostics?.database_path ?? "loading"} />
      <LedgerRow label="Immich" value={diagnostics?.immich_url ?? "loading"} />
    </section>
  );
}

function LedgerRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="ledger-row">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function useRealtimeEvents() {
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    const events = new EventSource("/api/admin/events", { withCredentials: true });
    events.addEventListener("ready", () => setConnected(true));
    events.addEventListener("heartbeat", () => setConnected(true));
    events.onerror = () => setConnected(false);
    return () => events.close();
  }, []);

  return { connected };
}

function titleFor(section: Section) {
  return {
    overview: "Mount console",
    views: "Saved views",
    mount: "Mount layout",
    writes: "Write policy",
    diagnostics: "Diagnostics"
  }[section];
}

function splitIds(value: string) {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}
