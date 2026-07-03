import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Check,
  Database,
  FolderTree,
  Gauge,
  KeyRound,
  ListFilter,
  Lock,
  LogOut,
  Pencil,
  Plus,
  Radio,
  RefreshCw,
  Save,
  Search,
  ShieldCheck,
  Tags,
  Trash2,
  Users,
  X
} from "lucide-react";
import {
  api,
  Diagnostics,
  emptyFilters,
  MountSettings,
  OptionItem,
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
  name: "",
  description: "",
  enabled: true,
  layout: "date_buckets",
  filters: { ...emptyFilters }
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
  const [editor, setEditor] = useState<EditorState>(null);
  const views = useQuery({ queryKey: ["views"], queryFn: api.views });
  const tags = useQuery({ queryKey: ["view-options", "tags"], queryFn: api.tagOptions });
  const people = useQuery({ queryKey: ["view-options", "people"], queryFn: api.peopleOptions });
  const optionLookup = useMemo(
    () => createOptionLookup(tags.data?.items ?? [], people.data?.items ?? []),
    [tags.data?.items, people.data?.items]
  );
  const invalidateAdminData = () => {
    queryClient.invalidateQueries({ queryKey: ["views"] });
    queryClient.invalidateQueries({ queryKey: ["diagnostics"] });
  };
  const createView = useMutation({
    mutationFn: api.createView,
    onSuccess: () => {
      setEditor(null);
      invalidateAdminData();
    }
  });
  const updateView = useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: SavedViewPayload }) =>
      api.updateView(id, payload),
    onSuccess: () => {
      setEditor(null);
      invalidateAdminData();
    }
  });
  const deleteView = useMutation({
    mutationFn: api.deleteView,
    onSuccess: () => {
      setEditor(null);
      invalidateAdminData();
    }
  });
  const savedViews = views.data?.views ?? [];
  const editorInitial = editor?.mode === "edit" ? payloadFromView(editor.view) : copyPayload(defaultView);
  const editorKey = editor?.mode === "edit" ? editor.view.id : "create";

  return (
    <section className={editor ? "views-manager editing" : "views-manager"}>
      <div className="view-ledger">
        <div className="views-toolbar">
          <div>
            <h2>Published folders</h2>
            <span>{savedViews.length} configured under /Views</span>
          </div>
          <button
            className="secondary-button"
            onClick={() => {
              tags.refetch();
              people.refetch();
              views.refetch();
            }}
            type="button"
            title="Refresh views"
          >
            <RefreshCw size={16} />
            <span>Refresh</span>
          </button>
        </div>

        {views.isError && <div className="form-error">{messageFromError(views.error)}</div>}
        {deleteView.isError && <div className="form-error">{messageFromError(deleteView.error)}</div>}

        {views.isLoading ? (
          <div className="loading-strip">Loading views</div>
        ) : savedViews.length ? (
          <div className="view-list">
            {savedViews.map((view) => (
              <ViewRow
                key={view.id}
                view={view}
                optionLookup={optionLookup}
                active={editor?.mode === "edit" && editor.view.id === view.id}
                pending={deleteView.isPending}
                onEdit={() => setEditor({ mode: "edit", view })}
                onDelete={() => {
                  if (window.confirm(`Delete saved view "${view.name}"?`)) {
                    deleteView.mutate(view.id);
                  }
                }}
              />
            ))}
          </div>
        ) : (
          <div className="empty-views">
            <FolderTree size={28} />
            <strong>No saved views yet</strong>
            <span>Create a DAV folder backed by Immich search filters.</span>
          </div>
        )}

        <div className="view-list-footer">
          <button className="primary-button" onClick={() => setEditor({ mode: "create" })} type="button">
            <Plus size={17} />
            <span>New View</span>
          </button>
        </div>
      </div>

      {editor && (
        <ViewComposer
          key={editorKey}
          mode={editor.mode}
          initial={editorInitial}
          pending={createView.isPending || updateView.isPending}
          errorMessage={messageFromError(createView.error) ?? messageFromError(updateView.error)}
          tagOptions={tags.data?.items ?? []}
          peopleOptions={people.data?.items ?? []}
          tagsLoading={tags.isLoading || tags.isFetching}
          peopleLoading={people.isLoading || people.isFetching}
          tagsError={tags.isError ? messageFromError(tags.error) : null}
          peopleError={people.isError ? messageFromError(people.error) : null}
          onCancel={() => setEditor(null)}
          onSave={(payload) => {
            if (editor.mode === "edit") {
              updateView.mutate({ id: editor.view.id, payload });
              return;
            }
            createView.mutate(payload);
          }}
        />
      )}
    </section>
  );
}

type EditorState = { mode: "create" } | { mode: "edit"; view: SavedView } | null;

type OptionLookup = {
  tags: Map<string, OptionItem>;
  people: Map<string, OptionItem>;
};

function ViewRow({
  view,
  optionLookup,
  active,
  onDelete,
  onEdit,
  pending
}: {
  view: SavedView;
  optionLookup: OptionLookup;
  active: boolean;
  onEdit: () => void;
  onDelete: () => void;
  pending: boolean;
}) {
  const chips = filterChips(view.filters, optionLookup);

  return (
    <article className={active ? "view-row active" : "view-row"}>
      <button className="view-main" onClick={onEdit} type="button">
        <div className="view-name-line">
          <strong>{view.name}</strong>
          <span className={view.enabled ? "status-dot enabled" : "status-dot"} />
          <span>{view.enabled ? "enabled" : "disabled"}</span>
        </div>
        <code>/Views/{view.name}</code>
        <div className="filter-chips">
          {chips.map((chip) => (
            <span className="filter-chip" key={chip}>
              {chip}
            </span>
          ))}
        </div>
      </button>
      <CountPill count={view.match_count} />
      <div className="row-actions">
        <button className="icon-button" onClick={onEdit} type="button" title="Edit view">
          <Pencil size={16} />
        </button>
        <button
          className="icon-button danger-icon"
          onClick={onDelete}
          disabled={pending}
          type="button"
          title="Delete view"
        >
          <Trash2 size={16} />
        </button>
      </div>
    </article>
  );
}

function CountPill({ count }: { count?: number | null }) {
  return (
    <div className={count == null ? "count-pill muted" : "count-pill"}>
      <strong>{count == null ? "--" : formatCount(count)}</strong>
      <span>{count === 1 ? "asset" : "assets"}</span>
    </div>
  );
}

function ViewComposer({
  mode,
  initial,
  pending,
  errorMessage,
  tagOptions,
  peopleOptions,
  tagsLoading,
  peopleLoading,
  tagsError,
  peopleError,
  onCancel,
  onSave
}: {
  mode: "create" | "edit";
  initial: SavedViewPayload;
  pending: boolean;
  errorMessage: string | null;
  tagOptions: OptionItem[];
  peopleOptions: OptionItem[];
  tagsLoading: boolean;
  peopleLoading: boolean;
  tagsError: string | null;
  peopleError: string | null;
  onCancel: () => void;
  onSave: (payload: SavedViewPayload) => void;
}) {
  const [draft, setDraft] = useState<SavedViewPayload>(() => copyPayload(initial));
  useEffect(() => setDraft(copyPayload(initial)), [initial]);
  const filters = draft.filters;
  const debouncedFilters = useDebouncedValue(filters, 450);
  const countPreview = useQuery({
    queryKey: ["view-count-preview", debouncedFilters],
    queryFn: () => api.matchCount(debouncedFilters),
    enabled: draft.name.trim().length > 0,
    retry: false,
    staleTime: 5000
  });
  const updateFilter = <K extends keyof ViewFilters>(key: K, value: ViewFilters[K]) => {
    setDraft((current) => ({ ...current, filters: { ...current.filters, [key]: value } }));
  };

  return (
    <form
      className="composer-panel"
      onSubmit={(event) => {
        event.preventDefault();
        onSave(cleanPayload(draft));
      }}
    >
      <div className="composer-header">
        <div>
          <h2>{mode === "edit" ? "Edit view" : "Create view"}</h2>
          <code>{draft.name.trim() ? `/Views/${draft.name.trim()}` : "/Views/<name>"}</code>
        </div>
        <button className="icon-button" onClick={onCancel} type="button" title="Close editor">
          <X size={17} />
        </button>
      </div>

      <div className="composer-switches">
        <label className="switch-row">
          <input
            type="checkbox"
            checked={draft.enabled}
            onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })}
          />
          enabled
        </label>
        <label className="switch-row">
          <input
            type="checkbox"
            checked={filters.is_favorite === true}
            onChange={(event) => updateFilter("is_favorite", event.target.checked ? true : null)}
          />
          favorites only
        </label>
      </div>

      <div className="form-grid">
        <label>
          Name
          <input
            required
            value={draft.name}
            onChange={(event) => setDraft({ ...draft, name: event.target.value })}
          />
        </label>
        <label>
          Description
          <input
            value={draft.description}
            onChange={(event) => setDraft({ ...draft, description: event.target.value })}
          />
        </label>
      </div>

      <div className="control-pair">
        <div className="control-group">
          <span>Layout</span>
          <div className="segmented-control">
            <button
              className={draft.layout === "date_buckets" ? "active" : ""}
              onClick={() => setDraft({ ...draft, layout: "date_buckets" })}
              type="button"
            >
              Date buckets
            </button>
            <button
              className={draft.layout === "flat" ? "active" : ""}
              onClick={() => setDraft({ ...draft, layout: "flat" })}
              type="button"
            >
              Flat
            </button>
          </div>
        </div>
        <div className="control-group">
          <span>Media</span>
          <div className="segmented-control">
            <button
              className={filters.media_type == null ? "active" : ""}
              onClick={() => updateFilter("media_type", null)}
              type="button"
            >
              Any
            </button>
            <button
              className={filters.media_type === "IMAGE" ? "active" : ""}
              onClick={() => updateFilter("media_type", "IMAGE")}
              type="button"
            >
              Images
            </button>
            <button
              className={filters.media_type === "VIDEO" ? "active" : ""}
              onClick={() => updateFilter("media_type", "VIDEO")}
              type="button"
            >
              Videos
            </button>
          </div>
        </div>
      </div>

      <div className="option-picker-grid">
        <OptionPicker
          title="Tags"
          icon={Tags}
          options={tagOptions}
          selectedIds={filters.tag_ids}
          loading={tagsLoading}
          error={tagsError}
          emptyLabel="No tags returned by Immich"
          searchPlaceholder="Search tags"
          onChange={(ids) => updateFilter("tag_ids", ids)}
        />
        <OptionPicker
          title="People"
          icon={Users}
          options={peopleOptions}
          selectedIds={filters.person_ids}
          loading={peopleLoading}
          error={peopleError}
          emptyLabel="No people returned by Immich"
          searchPlaceholder="Search people"
          onChange={(ids) => updateFilter("person_ids", ids)}
        />
      </div>

      <div className="form-grid">
        <label>
          Taken after
          <input
            type="date"
            value={filters.taken_after ?? ""}
            onChange={(event) => updateFilter("taken_after", event.target.value || null)}
          />
        </label>
        <label>
          Taken before
          <input
            type="date"
            value={filters.taken_before ?? ""}
            onChange={(event) => updateFilter("taken_before", event.target.value || null)}
          />
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
          Filename
          <input
            value={filters.original_file_name ?? ""}
            onChange={(event) => updateFilter("original_file_name", event.target.value || null)}
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
          City
          <input
            value={filters.city ?? ""}
            onChange={(event) => updateFilter("city", event.target.value || null)}
          />
        </label>
        <label>
          Country
          <input
            value={filters.country ?? ""}
            onChange={(event) => updateFilter("country", event.target.value || null)}
          />
        </label>
      </div>

      <details className="manual-id-panel">
        <summary>Manual IDs</summary>
        <div className="form-grid compact">
          <label>
            Album IDs
            <input
              value={filters.album_ids.join(", ")}
              onChange={(event) => updateFilter("album_ids", splitIds(event.target.value))}
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
        </div>
      </details>

      <div className="preview-panel">
        <ListFilter size={18} />
        <div>
          <strong>
            {countPreview.isError
              ? "count unavailable"
              : countPreview.isFetching
                ? "counting"
                : `${formatCount(countPreview.data?.count ?? null)} matching assets`}
          </strong>
          <span>{draft.name.trim() ? `/Views/${draft.name.trim()}` : "Name the view to preview it"}</span>
        </div>
        <button
          className="icon-button"
          onClick={() => countPreview.refetch()}
          type="button"
          title="Refresh count"
        >
          <RefreshCw size={16} />
        </button>
      </div>

      {errorMessage && <div className="form-error">{errorMessage}</div>}

      <div className="panel-actions">
        <button className="secondary-button" onClick={onCancel} type="button">
          <X size={16} />
          <span>Cancel</span>
        </button>
        <button className="primary-button" type="submit" disabled={pending}>
          <Save size={16} />
          <span>{pending ? "Saving" : "Save"}</span>
        </button>
      </div>
    </form>
  );
}

function OptionPicker({
  title,
  icon: Icon,
  options,
  selectedIds,
  loading,
  error,
  emptyLabel,
  searchPlaceholder,
  onChange
}: {
  title: string;
  icon: typeof Tags;
  options: OptionItem[];
  selectedIds: string[];
  loading: boolean;
  error: string | null;
  emptyLabel: string;
  searchPlaceholder: string;
  onChange: (ids: string[]) => void;
}) {
  const [query, setQuery] = useState("");
  const selected = new Set(selectedIds);
  const byId = new Map(options.map((option) => [option.id, option]));
  const selectedOptions = selectedIds.map(
    (id) => byId.get(id) ?? ({ id, name: shortId(id) } satisfies OptionItem)
  );
  const filtered = options.filter((option) =>
    option.name.toLowerCase().includes(query.trim().toLowerCase())
  );
  const visible = filtered.slice(0, 80);
  const toggle = (id: string) => {
    onChange(selected.has(id) ? selectedIds.filter((item) => item !== id) : [...selectedIds, id]);
  };

  return (
    <section className="option-picker">
      <div className="option-header">
        <Icon size={17} />
        <strong>{title}</strong>
        <span>{selectedIds.length} selected</span>
      </div>
      <label className="search-field">
        <Search size={15} />
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={searchPlaceholder}
        />
      </label>
      {selectedOptions.length > 0 && (
        <div className="selected-strip">
          {selectedOptions.map((option) => (
            <button key={option.id} onClick={() => toggle(option.id)} type="button">
              <span>{option.name}</span>
              <X size={13} />
            </button>
          ))}
        </div>
      )}
      <div className="option-list">
        {error ? (
          <div className="option-state">{error}</div>
        ) : loading ? (
          <div className="option-state">Loading</div>
        ) : visible.length ? (
          visible.map((option) => (
            <button
              className={selected.has(option.id) ? "option-row selected" : "option-row"}
              key={option.id}
              onClick={() => toggle(option.id)}
              type="button"
            >
              <span
                className="option-color"
                style={option.color ? { background: option.color } : undefined}
              />
              <span className="option-name">
                {option.name}
                {option.hidden && <em>hidden</em>}
              </span>
              {option.asset_count != null && <span className="option-count">{formatCount(option.asset_count)}</span>}
              {selected.has(option.id) && <Check size={16} />}
            </button>
          ))
        ) : (
          <div className="option-state">{emptyLabel}</div>
        )}
        {filtered.length > visible.length && (
          <div className="option-state">{filtered.length - visible.length} more; refine search</div>
        )}
      </div>
    </section>
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

function createOptionLookup(tags: OptionItem[], people: OptionItem[]): OptionLookup {
  return {
    tags: new Map(tags.map((item) => [item.id, item])),
    people: new Map(people.map((item) => [item.id, item]))
  };
}

function filterChips(filters: ViewFilters, lookup: OptionLookup) {
  const chips: string[] = [];
  if (filters.is_favorite === true) chips.push("Favorites");
  if (filters.media_type === "IMAGE") chips.push("Images");
  if (filters.media_type === "VIDEO") chips.push("Videos");
  if (filters.rating != null) chips.push(`Rating ${filters.rating}+`);
  if (filters.tag_ids.length) chips.push(`Tags: ${labelsForIds(filters.tag_ids, lookup.tags)}`);
  if (filters.person_ids.length) chips.push(`People: ${labelsForIds(filters.person_ids, lookup.people)}`);
  if (filters.album_ids.length) chips.push(`${filters.album_ids.length} album IDs`);
  if (filters.taken_after || filters.taken_before) {
    chips.push([filters.taken_after ?? "start", filters.taken_before ?? "now"].join(" to "));
  }
  if (filters.original_file_name) chips.push(`Filename: ${filters.original_file_name}`);
  if (filters.ocr) chips.push(`OCR: ${filters.ocr}`);
  if (filters.city) chips.push(`City: ${filters.city}`);
  if (filters.country) chips.push(`Country: ${filters.country}`);
  return chips.length ? chips : ["Any asset"];
}

function labelsForIds(ids: string[], options: Map<string, OptionItem>) {
  const labels = ids.slice(0, 2).map((id) => options.get(id)?.name ?? shortId(id));
  const extra = ids.length > labels.length ? ` +${ids.length - labels.length}` : "";
  return `${labels.join(", ")}${extra}`;
}

function shortId(id: string) {
  return id.length > 10 ? `${id.slice(0, 8)}...` : id;
}

function formatCount(count?: number | null) {
  return count == null ? "--" : count.toLocaleString();
}

function messageFromError(error: unknown) {
  if (!error) return null;
  return error instanceof Error ? error.message : String(error);
}

function payloadFromView(view: SavedView): SavedViewPayload {
  return {
    name: view.name,
    description: view.description,
    enabled: view.enabled,
    layout: view.layout,
    filters: copyFilters(view.filters)
  };
}

function copyPayload(payload: SavedViewPayload): SavedViewPayload {
  return {
    ...payload,
    filters: copyFilters(payload.filters)
  };
}

function copyFilters(filters: ViewFilters): ViewFilters {
  return {
    ...filters,
    album_ids: [...filters.album_ids],
    person_ids: [...filters.person_ids],
    tag_ids: [...filters.tag_ids]
  };
}

function cleanPayload(payload: SavedViewPayload): SavedViewPayload {
  return {
    ...payload,
    name: payload.name.trim(),
    description: payload.description.trim(),
    filters: {
      ...payload.filters,
      album_ids: uniqueIds(payload.filters.album_ids),
      person_ids: uniqueIds(payload.filters.person_ids),
      tag_ids: uniqueIds(payload.filters.tag_ids),
      original_file_name: blankToNull(payload.filters.original_file_name),
      ocr: blankToNull(payload.filters.ocr),
      city: blankToNull(payload.filters.city),
      country: blankToNull(payload.filters.country)
    }
  };
}

function uniqueIds(ids: string[]) {
  return Array.from(new Set(ids.map((id) => id.trim()).filter(Boolean)));
}

function blankToNull(value: string | null) {
  const trimmed = value?.trim() ?? "";
  return trimmed ? trimmed : null;
}

function splitIds(value: string) {
  return uniqueIds(value.split(","));
}

function useDebouncedValue<T>(value: T, delayMs: number) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timeout = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(timeout);
  }, [value, delayMs]);
  return debounced;
}
