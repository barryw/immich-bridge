import SwiftUI

@MainActor
final class AppState: ObservableObject {
    @Published var bridgeURLText: String
    @Published var username: String
    @Published var apiKeyInput = ""
    @Published var shareURLInput = ""
    @Published var connectionMode: BridgeAuthKind = .admin
    @Published var isAddingBridge = false
    @Published var selectedSection: AdminSection = .overview
    @Published var mountSettings: MountSettings?
    @Published var writePolicy: WritePolicy?
    @Published private(set) var profiles: [BridgeProfile]
    @Published private(set) var selectedProfileID: UUID?
    @Published private(set) var session: AdminSession?
    @Published private(set) var diagnostics: Diagnostics?
    @Published private(set) var views: [SavedView] = []
    @Published private(set) var tags: [OptionItem] = []
    @Published private(set) var people: [OptionItem] = []
    @Published private(set) var availableMounts: [BridgeMount] = []
    @Published private(set) var isLoading = false
    @Published private(set) var isSaving = false
    @Published private(set) var lastRefresh: Date?
    @Published var errorMessage: String?

    private var api: AdminAPI?
    private var storedApiKey: String?
    private var didBootstrap = false
    private let defaults = UserDefaults.standard
    private let bridgeURLKey = "ImmichBridgeAdminURL"
    private let usernameKey = "ImmichBridgeAdminUsername"
    private let profilesKey = "ImmichBridgeProfilesV1"
    private let selectedProfileKey = "ImmichBridgeSelectedProfileID"

    init() {
        let loadedProfiles = Self.loadProfiles(from: defaults, profilesKey: profilesKey)
        let legacyBridgeURL = defaults.string(forKey: bridgeURLKey)
        let legacyUsername = defaults.string(forKey: usernameKey)
        let initialProfiles: [BridgeProfile]
        if loadedProfiles.isEmpty, let legacyBridgeURL, !legacyBridgeURL.isEmpty {
            initialProfiles = [
                BridgeProfile(
                    bridgeURL: legacyBridgeURL,
                    displayName: URL(string: legacyBridgeURL)?.host ?? "Immich Bridge",
                    authKind: .admin,
                    username: legacyUsername
                )
            ]
        } else {
            initialProfiles = loadedProfiles
        }

        let initialSelectedProfileID: UUID?
        if let selectedIDText = defaults.string(forKey: selectedProfileKey),
           let selectedID = UUID(uuidString: selectedIDText),
           initialProfiles.contains(where: { $0.id == selectedID }) {
            initialSelectedProfileID = selectedID
        } else {
            initialSelectedProfileID = initialProfiles.first?.id
        }

        let selectedProfile = initialProfiles.first { $0.id == initialSelectedProfileID }
        profiles = initialProfiles
        selectedProfileID = initialSelectedProfileID
        bridgeURLText = selectedProfile?.bridgeURL ?? legacyBridgeURL ?? ""
        username = selectedProfile?.username ?? legacyUsername ?? ""
        connectionMode = selectedProfile?.authKind ?? .admin
    }

    var activeProfile: BridgeProfile? {
        profiles.first { $0.id == selectedProfileID }
    }

    var isAuthenticated: Bool {
        session?.authenticated == true
    }

    var canManageBridge: Bool {
        session?.isAdminCapable == true
    }

    var shouldShowConnectForm: Bool {
        !isAuthenticated || isAddingBridge
    }

    var canConnect: Bool {
        let hasBridge = !bridgeURLText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        switch connectionMode {
        case .admin:
            return hasBridge && !apiKeyInput.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        case .share:
            return hasBridge && !shareURLInput.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
    }

    var displayUser: String {
        session?.displayName ?? activeProfile?.principalName ?? activeProfile?.displayName ?? username
    }

    var statusText: String {
        if isAuthenticated {
            let host = activeProfile?.hostLabel ?? URL(string: bridgeURLText)?.host ?? "Immich Bridge"
            return "Connected to \(host) as \(displayUser)"
        }
        return "Immich Bridge is not connected"
    }

    var menuBarSymbol: String {
        isAuthenticated ? "externaldrive.badge.checkmark" : "externaldrive.badge.questionmark"
    }

    func bootstrap() async {
        guard !didBootstrap else {
            return
        }
        didBootstrap = true
        await restoreSession()
    }

    func connect() async {
        switch connectionMode {
        case .admin:
            await connectAdmin()
        case .share:
            await connectShare()
        }
    }

    func signIn() async {
        await connect()
    }

    func beginAddBridge() {
        isAddingBridge = true
        errorMessage = nil
        bridgeURLText = ""
        username = ""
        apiKeyInput = ""
        shareURLInput = ""
        connectionMode = .admin
    }

    func cancelAddBridge() {
        isAddingBridge = false
        errorMessage = nil
        applyActiveProfileToInputs()
    }

    func activateProfile(_ profile: BridgeProfile) async {
        guard selectedProfileID != profile.id else {
            return
        }
        selectedProfileID = profile.id
        defaults.set(profile.id.uuidString, forKey: selectedProfileKey)
        clearRuntimeState(keepSession: false)
        applyActiveProfileToInputs()
        await restoreSession()
    }

    private func connectAdmin() async {
        errorMessage = nil
        isLoading = true
        defer { isLoading = false }

        do {
            let bridgeURL = try normalizedBridgeURL(from: bridgeURLText)
            let client = AdminAPI(baseURL: bridgeURL)
            let currentSession = try await client.login(username: username, apiKey: apiKeyInput)
            guard let token = currentSession.sessionToken else {
                throw AdminAPIError.emptyResponse
            }

            let apiKey = apiKeyInput
            try await finishConnection(
                client: client,
                currentSession: currentSession,
                bridgeURL: bridgeURL,
                authKind: .admin,
                username: username,
                sessionToken: token,
                secret: apiKey
            )
            storedApiKey = apiKey
            apiKeyInput = ""
        } catch {
            errorMessage = message(from: error)
        }
    }

    private func connectShare() async {
        errorMessage = nil
        isLoading = true
        defer { isLoading = false }

        do {
            let bridgeURL = try normalizedBridgeURL(from: bridgeURLText)
            let client = AdminAPI(baseURL: bridgeURL)
            let currentSession = try await client.shareLogin(shareURL: shareURLInput)
            guard let token = currentSession.sessionToken else {
                throw AdminAPIError.emptyResponse
            }

            let shareURL = shareURLInput
            try await finishConnection(
                client: client,
                currentSession: currentSession,
                bridgeURL: bridgeURL,
                authKind: .share,
                username: nil,
                sessionToken: token,
                secret: shareURL
            )
            shareURLInput = ""
        } catch {
            errorMessage = message(from: error)
        }
    }

    func signOut() async {
        errorMessage = nil
        if let api {
            try? await api.logoutCurrentSession()
        }
        if let profile = activeProfile {
            try? KeychainStore.delete(accountName: sessionTokenAccount(for: profile.id))
            try? KeychainStore.delete(accountName: adminApiKeyAccount(for: profile.id))
            try? KeychainStore.delete(accountName: shareURLAccount(for: profile.id))
        }
        clearRuntimeState(keepSession: false)
        applyActiveProfileToInputs()
    }

    func refreshAll(setLoading: Bool = true, retried: Bool = false) async {
        guard let api else {
            return
        }
        errorMessage = nil
        if setLoading {
            isLoading = true
        }
        defer {
            if setLoading {
                isLoading = false
            }
        }

        do {
            let currentSession = try await api.currentPrincipal()
            let mounts = try await loadMounts(for: currentSession, using: api)
            session = currentSession
            availableMounts = mounts
            updateActiveProfile(session: currentSession, mounts: mounts)

            guard currentSession.isAdminCapable else {
                diagnostics = nil
                mountSettings = nil
                writePolicy = nil
                views = []
                tags = []
                people = []
                lastRefresh = Date()
                return
            }

            async let diagnosticsTask = api.diagnostics()
            async let mountTask = api.mountSettings()
            async let policyTask = api.writePolicy()
            async let viewsTask = api.views(includeCounts: true)
            async let tagsTask = api.tagOptions()
            async let peopleTask = api.peopleOptions()

            let loaded = try await (
                diagnosticsTask,
                mountTask,
                policyTask,
                viewsTask,
                tagsTask,
                peopleTask
            )
            diagnostics = loaded.0
            mountSettings = loaded.1
            writePolicy = loaded.2
            views = loaded.3
            tags = loaded.4.sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
            people = loaded.5.sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
            lastRefresh = Date()
        } catch {
            if !retried, isUnauthorized(error), await reauthenticate() {
                await refreshAll(setLoading: false, retried: true)
                return
            }
            errorMessage = message(from: error)
        }
    }

    func savePolicy(mount: MountSettings, policy: WritePolicy, retried: Bool = false) async -> Bool {
        guard let api else {
            return false
        }
        errorMessage = nil
        isSaving = true
        defer { isSaving = false }

        do {
            var cleanPolicy = policy
            cleanPolicy.permanentDelete = false
            let savedMount = try await api.updateMountSettings(mount)
            let savedPolicy = try await api.updateWritePolicy(cleanPolicy)
            mountSettings = savedMount
            writePolicy = savedPolicy
            diagnostics = try? await api.diagnostics()
            lastRefresh = Date()
            return true
        } catch {
            if !retried, isUnauthorized(error), await reauthenticate() {
                return await savePolicy(mount: mount, policy: policy, retried: true)
            }
            errorMessage = message(from: error)
            return false
        }
    }

    func createView(_ payload: SavedViewPayload, retried: Bool = false) async -> Bool {
        guard let api else {
            return false
        }
        errorMessage = nil
        isSaving = true
        defer { isSaving = false }

        do {
            let view = try await api.createView(payload.cleaned())
            views.append(view)
            views.sort { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
            diagnostics = try? await api.diagnostics()
            return true
        } catch {
            if !retried, isUnauthorized(error), await reauthenticate() {
                return await createView(payload, retried: true)
            }
            errorMessage = message(from: error)
            return false
        }
    }

    func updateView(id: String, payload: SavedViewPayload, retried: Bool = false) async -> Bool {
        guard let api else {
            return false
        }
        errorMessage = nil
        isSaving = true
        defer { isSaving = false }

        do {
            let saved = try await api.updateView(id: id, payload: payload.cleaned())
            if let index = views.firstIndex(where: { $0.id == id }) {
                views[index] = saved
            }
            views.sort { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
            return true
        } catch {
            if !retried, isUnauthorized(error), await reauthenticate() {
                return await updateView(id: id, payload: payload, retried: true)
            }
            errorMessage = message(from: error)
            return false
        }
    }

    func deleteView(_ view: SavedView, retried: Bool = false) async {
        guard let api else {
            return
        }
        errorMessage = nil
        isSaving = true
        defer { isSaving = false }

        do {
            try await api.deleteView(id: view.id)
            views.removeAll { $0.id == view.id }
            diagnostics = try? await api.diagnostics()
        } catch {
            if !retried, isUnauthorized(error), await reauthenticate() {
                await deleteView(view, retried: true)
                return
            }
            errorMessage = message(from: error)
        }
    }

    func matchCount(filters: ViewFilters, retried: Bool = false) async throws -> Int? {
        guard let api else {
            throw AdminAPIError.invalidBridgeURL
        }
        do {
            return try await api.matchCount(filters: filters)
        } catch {
            if !retried, isUnauthorized(error), await reauthenticate() {
                return try await matchCount(filters: filters, retried: true)
            }
            throw error
        }
    }

    private func restoreSession() async {
        guard let profile = activeProfile else {
            applyActiveProfileToInputs()
            return
        }

        isLoading = true
        defer { isLoading = false }

        do {
            applyActiveProfileToInputs()
            if profile.authKind == .admin {
                storedApiKey = try KeychainStore.read(accountName: adminApiKeyAccount(for: profile.id))
                    ?? KeychainStore.read(account: .adminApiKey)
            }
            let token = try KeychainStore.read(accountName: sessionTokenAccount(for: profile.id))
                ?? KeychainStore.read(account: .sessionToken)
            let bridgeURL = try normalizedBridgeURL(from: profile.bridgeURL)
            let client = AdminAPI(baseURL: bridgeURL, sessionToken: token)
            api = client

            if let token, !token.isEmpty {
                do {
                    session = try await client.currentPrincipal()
                    await refreshAll(setLoading: false)
                } catch {
                    if isUnauthorized(error), await reauthenticate() {
                        await refreshAll(setLoading: false)
                    } else {
                        clearRuntimeState(keepSession: false)
                    }
                }
                return
            }

            if await reauthenticate() {
                await refreshAll(setLoading: false)
            }
        } catch {
            errorMessage = message(from: error)
        }
    }

    private func reauthenticate() async -> Bool {
        guard let profile = activeProfile else {
            return false
        }
        do {
            let bridgeURL = try normalizedBridgeURL(from: profile.bridgeURL)
            let client = AdminAPI(baseURL: bridgeURL)
            let currentSession: AdminSession
            let secret: String
            switch profile.authKind {
            case .admin:
                let profileApiKey = try KeychainStore.read(accountName: adminApiKeyAccount(for: profile.id))
                let legacyApiKey = try KeychainStore.read(account: .adminApiKey)
                let apiKey = storedApiKey ?? profileApiKey ?? legacyApiKey
                guard let apiKey, !apiKey.isEmpty else {
                    return false
                }
                currentSession = try await client.login(username: profile.username ?? username, apiKey: apiKey)
                secret = apiKey
                storedApiKey = apiKey
            case .share:
                guard let shareURL = try KeychainStore.read(accountName: shareURLAccount(for: profile.id)),
                      !shareURL.isEmpty else {
                    return false
                }
                currentSession = try await client.shareLogin(shareURL: shareURL)
                secret = shareURL
            }
            guard let token = currentSession.sessionToken else {
                throw AdminAPIError.emptyResponse
            }
            try await finishConnection(
                client: client,
                currentSession: currentSession,
                bridgeURL: bridgeURL,
                authKind: profile.authKind,
                username: profile.username,
                sessionToken: token,
                secret: secret,
                refreshAdminState: false
            )
            return true
        } catch {
            return false
        }
    }

    private func normalizedBridgeURL(from text: String) throws -> URL {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            throw AdminAPIError.invalidBridgeURL
        }
        var value = trimmed.contains("://") ? trimmed : "https://\(trimmed)"
        while value.hasSuffix("/") {
            value.removeLast()
        }
        guard let url = URL(string: value), url.scheme != nil, url.host != nil else {
            throw AdminAPIError.invalidBridgeURL
        }
        return url
    }

    private func isUnauthorized(_ error: Error) -> Bool {
        (error as? AdminAPIError)?.isUnauthorized == true
    }

    private func message(from error: Error) -> String {
        if let description = (error as? LocalizedError)?.errorDescription {
            return description
        }
        return error.localizedDescription
    }

    private func loadMounts(for currentSession: AdminSession, using client: AdminAPI) async throws -> [BridgeMount] {
        let mounts = try await client.availableMounts()
        if !mounts.isEmpty || !currentSession.isAdminCapable {
            return mounts
        }

        let libraries = (try? await client.libraries()) ?? []
        if !libraries.isEmpty {
            let capabilities = mountCapabilities(for: currentSession)
            return libraries.map { library in
                BridgeMount(
                    id: "library:\(library.id)",
                    kind: "library",
                    libraryId: library.id,
                    libraryName: library.name,
                    displayName: library.name,
                    scope: "library",
                    capabilities: capabilities
                )
            }
        }

        return fallbackMounts(for: currentSession)
    }

    private func fallbackMounts(for currentSession: AdminSession) -> [BridgeMount] {
        guard currentSession.isAdminCapable else {
            return []
        }
        let libraryID = currentSession.grants.first { $0.scope == "library" }?.libraryId ?? "default"
        let displayName = libraryID == "default" ? "Immich Library" : libraryID
        return [
            BridgeMount(
                id: "library:\(libraryID)",
                kind: "library",
                libraryId: libraryID,
                libraryName: displayName,
                displayName: displayName,
                scope: "library",
                capabilities: mountCapabilities(for: currentSession)
            )
        ]
    }

    private func mountCapabilities(for currentSession: AdminSession) -> [String] {
        let capabilities = Set(currentSession.grants.flatMap(\.capabilities))
        if !capabilities.isEmpty {
            return Array(capabilities).sorted()
        }
        return [
            "browse",
            "download",
            "thumbnail",
            "upload",
            "manage_views",
            "manage_policy",
            "manage_library",
            "diagnostics",
        ]
    }

    private func finishConnection(
        client: AdminAPI,
        currentSession: AdminSession,
        bridgeURL: URL,
        authKind: BridgeAuthKind,
        username: String?,
        sessionToken: String,
        secret: String,
        refreshAdminState: Bool = true
    ) async throws {
        api = client
        session = currentSession
        availableMounts = try await loadMounts(for: currentSession, using: client)

        let profile = upsertProfile(
            bridgeURL: bridgeURL,
            authKind: authKind,
            username: username,
            session: currentSession,
            mounts: availableMounts
        )
        selectedProfileID = profile.id
        defaults.set(profile.id.uuidString, forKey: selectedProfileKey)
        defaults.set(bridgeURL.absoluteString, forKey: bridgeURLKey)
        if let username {
            defaults.set(username, forKey: usernameKey)
        }
        try KeychainStore.save(sessionToken, accountName: sessionTokenAccount(for: profile.id))
        switch authKind {
        case .admin:
            try KeychainStore.save(secret, accountName: adminApiKeyAccount(for: profile.id))
        case .share:
            try KeychainStore.save(secret, accountName: shareURLAccount(for: profile.id))
        }
        isAddingBridge = false
        lastRefresh = Date()

        if refreshAdminState, currentSession.isAdminCapable {
            await refreshAll(setLoading: false)
        } else if !currentSession.isAdminCapable {
            diagnostics = nil
            mountSettings = nil
            writePolicy = nil
            views = []
            tags = []
            people = []
        }
    }

    private func upsertProfile(
        bridgeURL: URL,
        authKind: BridgeAuthKind,
        username: String?,
        session: AdminSession,
        mounts: [BridgeMount]
    ) -> BridgeProfile {
        let principalName = session.displayName
        let displayName = mounts.first?.libraryName
            ?? session.principal?.displayName
            ?? bridgeURL.host
            ?? "Immich Bridge"
        let principalKind = session.principal?.kind
        let targetIndex = profiles.firstIndex { profile in
            if !isAddingBridge, profile.id == selectedProfileID {
                return true
            }
            return profile.bridgeURL == bridgeURL.absoluteString
                && profile.authKind == authKind
                && profile.username == username
                && profile.principalKind == principalKind
                && profile.principalName == principalName
        }

        if let targetIndex {
            profiles[targetIndex].bridgeURL = bridgeURL.absoluteString
            profiles[targetIndex].displayName = displayName
            profiles[targetIndex].authKind = authKind
            profiles[targetIndex].username = username
            profiles[targetIndex].principalKind = principalKind
            profiles[targetIndex].principalName = principalName
            profiles[targetIndex].mounts = mounts
            profiles[targetIndex].sessionExpiresAt = session.expiresAt
            profiles[targetIndex].lastConnectedAt = Date()
            saveProfiles()
            return profiles[targetIndex]
        }

        let profile = BridgeProfile(
            bridgeURL: bridgeURL.absoluteString,
            displayName: displayName,
            authKind: authKind,
            username: username,
            principalKind: principalKind,
            principalName: principalName,
            mounts: mounts,
            sessionExpiresAt: session.expiresAt,
            lastConnectedAt: Date()
        )
        profiles.append(profile)
        saveProfiles()
        return profile
    }

    private func updateActiveProfile(session: AdminSession, mounts: [BridgeMount]) {
        guard let index = profiles.firstIndex(where: { $0.id == selectedProfileID }) else {
            return
        }
        profiles[index].principalKind = session.principal?.kind
        profiles[index].principalName = session.displayName
        profiles[index].mounts = mounts
        profiles[index].sessionExpiresAt = session.expiresAt
        profiles[index].lastConnectedAt = Date()
        saveProfiles()
    }

    private func clearRuntimeState(keepSession: Bool) {
        api = nil
        storedApiKey = nil
        if !keepSession {
            session = nil
        }
        diagnostics = nil
        mountSettings = nil
        writePolicy = nil
        views = []
        tags = []
        people = []
        availableMounts = []
        lastRefresh = nil
    }

    private func applyActiveProfileToInputs() {
        guard let profile = activeProfile else {
            bridgeURLText = ""
            username = ""
            connectionMode = .admin
            apiKeyInput = ""
            shareURLInput = ""
            return
        }
        bridgeURLText = profile.bridgeURL
        username = profile.username ?? ""
        connectionMode = profile.authKind
        apiKeyInput = ""
        shareURLInput = ""
    }

    private func saveProfiles() {
        guard let data = try? JSONEncoder().encode(profiles) else {
            return
        }
        defaults.set(data, forKey: profilesKey)
    }

    private static func loadProfiles(from defaults: UserDefaults, profilesKey: String) -> [BridgeProfile] {
        guard let data = defaults.data(forKey: profilesKey) else {
            return []
        }
        return (try? JSONDecoder().decode([BridgeProfile].self, from: data)) ?? []
    }

    private func sessionTokenAccount(for profileID: UUID) -> String {
        "profile.\(profileID.uuidString).session-token"
    }

    private func adminApiKeyAccount(for profileID: UUID) -> String {
        "profile.\(profileID.uuidString).admin-api-key"
    }

    private func shareURLAccount(for profileID: UUID) -> String {
        "profile.\(profileID.uuidString).share-url"
    }
}

enum AdminSection: String, CaseIterable, Identifiable, Hashable {
    case overview
    case views
    case policy
    case diagnostics

    var id: String { rawValue }

    var title: String {
        switch self {
        case .overview: "Overview"
        case .views: "Views"
        case .policy: "Filesystem"
        case .diagnostics: "Diagnostics"
        }
    }

    var systemImage: String {
        switch self {
        case .overview: "gauge.with.dots.needle.67percent"
        case .views: "rectangle.grid.2x2"
        case .policy: "folder.badge.gearshape"
        case .diagnostics: "waveform.path.ecg"
        }
    }
}

struct SettingsView: View {
    @ObservedObject var state: AppState

    var body: some View {
        Group {
            if state.isAuthenticated, state.canManageBridge {
                AdminConsoleView(state: state)
            } else if state.isAuthenticated {
                ViewerSettingsView(state: state)
            } else {
                LoginView(state: state)
            }
        }
        .task {
            await state.bootstrap()
        }
    }
}

private struct LoginView: View {
    @ObservedObject var state: AppState

    var body: some View {
        VStack(alignment: .leading, spacing: 24) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Immich Bridge")
                    .font(.largeTitle.weight(.semibold))
                Text("Connect with an Immich admin API key or an Immich shared link.")
                    .foregroundStyle(.secondary)
            }

            Form {
                Picker("Access", selection: $state.connectionMode) {
                    ForEach(BridgeAuthKind.allCases) { mode in
                        Label(mode.label, systemImage: mode.systemImage).tag(mode)
                    }
                }
                .pickerStyle(.segmented)

                TextField("Bridge URL", text: $state.bridgeURLText)
                    .textContentType(.URL)

                if state.connectionMode == .admin {
                    TextField("Immich username or email", text: $state.username)
                        .textContentType(.username)
                    SecureField("Immich API key or superadmin password", text: $state.apiKeyInput)
                        .textContentType(.password)
                } else {
                    TextField("Immich share URL", text: $state.shareURLInput)
                        .textContentType(.URL)
                }
            }
            .formStyle(.grouped)

            if let errorMessage = state.errorMessage {
                ErrorBanner(message: errorMessage)
            }

            HStack {
                Button {
                    Task {
                        await state.signIn()
                    }
                } label: {
                    Label(state.isLoading ? "Connecting" : "Connect", systemImage: "key")
                }
                .buttonStyle(.borderedProminent)
                .disabled(state.isLoading || !state.canConnect)

                if state.isLoading {
                    ProgressView()
                        .controlSize(.small)
                }

                Spacer()
            }
        }
        .padding(32)
        .frame(minWidth: 620, minHeight: 420)
    }
}

private struct ViewerSettingsView: View {
    @ObservedObject var state: AppState

    var body: some View {
        VStack(spacing: 0) {
            HeaderView(state: state)
            Divider()

            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if let errorMessage = state.errorMessage {
                        ErrorBanner(message: errorMessage)
                    }

                    GroupBox("Access") {
                        Grid(alignment: .leading, horizontalSpacing: 28, verticalSpacing: 10) {
                            InfoRow("Bridge", state.activeProfile?.hostLabel ?? state.bridgeURLText)
                            InfoRow("Role", state.session?.principal?.roleLabel ?? "Viewer")
                            InfoRow("User", state.displayUser)
                            InfoRow("Expires", state.session?.expiresAt ?? "-")
                        }
                        .padding(6)
                    }

                    MountsGroup(mounts: state.availableMounts)

                    GroupBox("Settings") {
                        HStack(alignment: .top, spacing: 10) {
                            Image(systemName: "lock")
                                .foregroundStyle(.secondary)
                            Text("This login can browse the mounts listed above. Bridge configuration is available only to superadmins and Immich library admins.")
                                .foregroundStyle(.secondary)
                            Spacer()
                        }
                        .padding(6)
                    }
                }
                .padding(22)
            }
        }
        .frame(minWidth: 720, minHeight: 520)
    }
}

private struct AdminConsoleView: View {
    @ObservedObject var state: AppState

    var body: some View {
        HStack(spacing: 0) {
            List(AdminSection.allCases, selection: $state.selectedSection) { section in
                Label(section.title, systemImage: section.systemImage)
                    .tag(section)
            }
            .listStyle(.sidebar)
            .frame(width: 190)

            Divider()

            VStack(spacing: 0) {
                HeaderView(state: state)
                Divider()

                if let errorMessage = state.errorMessage {
                    ErrorBanner(message: errorMessage)
                        .padding([.horizontal, .top], 18)
                }

                Group {
                    switch state.selectedSection {
                    case .overview:
                        OverviewPanel(state: state)
                    case .views:
                        ViewsPanel(state: state)
                    case .policy:
                        PolicyPanel(state: state)
                    case .diagnostics:
                        DiagnosticsPanel(state: state)
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            }
        }
        .frame(minWidth: 920, minHeight: 640)
    }
}

private struct HeaderView: View {
    @ObservedObject var state: AppState

    var body: some View {
        HStack(spacing: 14) {
            VStack(alignment: .leading, spacing: 4) {
                Text(state.selectedSection.title)
                    .font(.title2.weight(.semibold))
                Text(state.statusText)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }

            Spacer()

            if state.isLoading || state.isSaving {
                ProgressView()
                    .controlSize(.small)
            }

            Button {
                Task {
                    await state.refreshAll()
                }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
            .disabled(state.isLoading || state.isSaving)

            Button {
                Task {
                    await state.signOut()
                }
            } label: {
                Label("Sign Out", systemImage: "rectangle.portrait.and.arrow.right")
            }
        }
        .padding(18)
    }
}

private struct OverviewPanel: View {
    @ObservedObject var state: AppState

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                if let diagnostics = state.diagnostics {
                    GroupBox("Bridge") {
                        Grid(alignment: .leading, horizontalSpacing: 28, verticalSpacing: 10) {
                            InfoRow("Immich", diagnostics.immichUrl)
                            InfoRow("Admin API", state.bridgeURLText)
                            InfoRow("WebDAV port", "\(diagnostics.webdavPort)")
                            InfoRow("Admin port", "\(diagnostics.adminPort)")
                            InfoRow("Redis", diagnostics.redisEnabled ? "Enabled" : "Disabled")
                            InfoRow("Metrics", diagnostics.metricsEnabled ? "Enabled" : "Disabled")
                        }
                        .padding(6)
                    }

                    GroupBox("Mount map") {
                        VStack(alignment: .leading, spacing: 10) {
                            MountLine(name: "Albums", enabled: diagnostics.mount.albumsEnabled)
                            MountLine(name: "Timeline", enabled: diagnostics.mount.timelineEnabled)
                            MountLine(name: "Favorites", enabled: diagnostics.mount.favoritesEnabled)
                            MountLine(name: "Views", enabled: diagnostics.mount.viewsEnabled)
                            MountLine(name: "Tags", enabled: diagnostics.mount.tagsEnabled)
                            MountLine(name: "People", enabled: diagnostics.mount.peopleEnabled)
                        }
                        .padding(6)
                    }

                    GroupBox("Saved views") {
                        HStack {
                            Label("\(diagnostics.viewCount)", systemImage: "rectangle.grid.2x2")
                                .font(.title3.weight(.semibold))
                            Text(diagnostics.viewCount == 1 ? "view configured" : "views configured")
                                .foregroundStyle(.secondary)
                            Spacer()
                            Button("Manage Views") {
                                state.selectedSection = .views
                            }
                        }
                        .padding(6)
                    }
                } else {
                    LoadingStateView(message: "Loading bridge status")
                }
            }
            .padding(22)
        }
    }
}

private struct ViewsPanel: View {
    @ObservedObject var state: AppState
    @State private var editor: ViewEditorState?
    @State private var deleteTarget: SavedView?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("\(state.views.count) saved \(state.views.count == 1 ? "view" : "views")")
                    .font(.headline)
                Spacer()
                Button {
                    editor = ViewEditorState(payload: SavedViewPayload(name: "New View"))
                } label: {
                    Label("New View", systemImage: "plus")
                }
                .buttonStyle(.borderedProminent)
            }

            if state.views.isEmpty {
                ContentUnavailableView(
                    "No saved views",
                    systemImage: "rectangle.grid.2x2",
                    description: Text("Create a view to expose filtered Immich media under /Views.")
                )
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollView {
                    LazyVStack(spacing: 10) {
                        ForEach(state.views) { view in
                            ViewRow(
                                view: view,
                                tags: state.tags,
                                people: state.people,
                                onEdit: {
                                    editor = ViewEditorState(view: view)
                                },
                                onDelete: {
                                    deleteTarget = view
                                }
                            )
                        }
                    }
                    .padding(.bottom, 14)
                }
            }
        }
        .padding(22)
        .sheet(item: $editor) { editor in
            ViewEditorSheet(
                title: editor.viewID == nil ? "New View" : "Edit View",
                initialPayload: editor.payload,
                tags: state.tags,
                people: state.people,
                isSaving: state.isSaving,
                countProvider: { filters in
                    try await state.matchCount(filters: filters)
                },
                onCancel: {
                    self.editor = nil
                },
                onSave: { payload in
                    if let viewID = editor.viewID {
                        return await state.updateView(id: viewID, payload: payload)
                    }
                    return await state.createView(payload)
                }
            )
        }
        .confirmationDialog(
            "Delete this view?",
            isPresented: Binding(
                get: { deleteTarget != nil },
                set: { if !$0 { deleteTarget = nil } }
            ),
            presenting: deleteTarget
        ) { view in
            Button("Delete \(view.name)", role: .destructive) {
                Task {
                    await state.deleteView(view)
                    deleteTarget = nil
                }
            }
        } message: { view in
            Text("/Views/\(view.name) will disappear from WebDAV.")
        }
    }
}

private struct ViewRow: View {
    var view: SavedView
    var tags: [OptionItem]
    var people: [OptionItem]
    var onEdit: () -> Void
    var onDelete: () -> Void

    var body: some View {
        HStack(alignment: .center, spacing: 14) {
            Button(action: onEdit) {
                VStack(alignment: .leading, spacing: 7) {
                    HStack(spacing: 8) {
                        Text(view.name)
                            .font(.headline)
                        StatusPill(text: view.enabled ? "enabled" : "disabled", isActive: view.enabled)
                        Text(view.layout.label)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Text("/Views/\(view.name)")
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                    FilterChips(filters: view.filters, tags: tags, people: people)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            CountPill(count: view.matchCount)

            Button(action: onEdit) {
                Image(systemName: "pencil")
            }
            .buttonStyle(.borderless)
            .help("Edit view")

            Button(role: .destructive, action: onDelete) {
                Image(systemName: "trash")
            }
            .buttonStyle(.borderless)
            .help("Delete view")
        }
        .padding(14)
        .background(.background, in: RoundedRectangle(cornerRadius: 8))
        .overlay {
            RoundedRectangle(cornerRadius: 8)
                .stroke(.quaternary)
        }
    }
}

private struct PolicyPanel: View {
    @ObservedObject var state: AppState
    @State private var draftMount: MountSettings?
    @State private var draftPolicy: WritePolicy?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Filesystem policy")
                            .font(.headline)
                        Text("Control visible DAV folders and allowed write operations.")
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button {
                        Task {
                            await save()
                        }
                    } label: {
                        Label(state.isSaving ? "Saving" : "Save", systemImage: "square.and.arrow.down")
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(draftMount == nil || draftPolicy == nil || state.isSaving)
                }

                if draftMount == nil || draftPolicy == nil {
                    LoadingStateView(message: "Loading filesystem policy")
                } else {
                    GroupBox("Root") {
                        VStack(alignment: .leading, spacing: 12) {
                            Toggle("Allow raw media uploads at /", isOn: policyBinding(\.rootUploads, default: true))
                            Toggle("Allow DAV move and copy", isOn: policyBinding(\.moveCopy, default: false))
                            Toggle("Allow overwrites", isOn: policyBinding(\.overwrite, default: false))
                            Toggle("Permanent deletion", isOn: .constant(false))
                                .disabled(true)
                            Text("Permanent deletion is intentionally disabled by the bridge backend.")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        .padding(6)
                    }

                    GroupBox("Top-level folders") {
                        VStack(alignment: .leading, spacing: 12) {
                            Toggle("Albums", isOn: mountBinding(\.albumsEnabled, default: true))
                            Toggle("Timeline", isOn: mountBinding(\.timelineEnabled, default: true))
                            Toggle("Favorites", isOn: mountBinding(\.favoritesEnabled, default: true))
                            Toggle("Views", isOn: mountBinding(\.viewsEnabled, default: true))
                            Toggle("Tags", isOn: mountBinding(\.tagsEnabled, default: false))
                            Toggle("People", isOn: mountBinding(\.peopleEnabled, default: false))
                        }
                        .padding(6)
                    }

                    GroupBox("Albums") {
                        VStack(alignment: .leading, spacing: 12) {
                            Toggle("Allow uploads into albums", isOn: policyBinding(\.albumUploads, default: true))
                            Toggle("Allow album creation", isOn: policyBinding(\.albumCreate, default: true))
                            Toggle(
                                "Allow removing assets from albums",
                                isOn: policyBinding(\.albumMembershipDelete, default: true)
                            )
                            Stepper(
                                "Split album folders above \(draftMount?.albumFolderSplitThreshold ?? 0) assets",
                                value: mountBinding(\.albumFolderSplitThreshold, default: 200),
                                in: 0...100_000,
                                step: 50
                            )
                        }
                        .padding(6)
                    }

                    GroupBox("Timeline and filenames") {
                        VStack(alignment: .leading, spacing: 12) {
                            Stepper(
                                "Split day folders above \(draftMount?.dayFolderSplitThreshold ?? 0) assets",
                                value: mountBinding(\.dayFolderSplitThreshold, default: 1000),
                                in: 0...100_000,
                                step: 100
                            )
                            Picker("Filename mode", selection: mountBinding(\.filenameMode, default: .dateOriginalId)) {
                                ForEach(FilenameMode.allCases) { mode in
                                    Text(mode.label).tag(mode)
                                }
                            }
                            .pickerStyle(.menu)
                        }
                        .padding(6)
                    }
                }
            }
            .padding(22)
        }
        .onAppear(perform: syncDrafts)
        .onChange(of: state.mountSettings) { _, _ in
            if draftMount == nil {
                syncDrafts()
            }
        }
        .onChange(of: state.writePolicy) { _, _ in
            if draftPolicy == nil {
                syncDrafts()
            }
        }
    }

    private func syncDrafts() {
        draftMount = state.mountSettings
        draftPolicy = state.writePolicy
    }

    private func save() async {
        guard let draftMount, let draftPolicy else {
            return
        }
        if await state.savePolicy(mount: draftMount, policy: draftPolicy) {
            syncDrafts()
        }
    }

    private func mountBinding<Value>(
        _ keyPath: WritableKeyPath<MountSettings, Value>,
        default defaultValue: Value
    ) -> Binding<Value> {
        Binding {
            draftMount?[keyPath: keyPath] ?? defaultValue
        } set: { value in
            guard var draft = draftMount else {
                return
            }
            draft[keyPath: keyPath] = value
            draftMount = draft
        }
    }

    private func policyBinding<Value>(
        _ keyPath: WritableKeyPath<WritePolicy, Value>,
        default defaultValue: Value
    ) -> Binding<Value> {
        Binding {
            draftPolicy?[keyPath: keyPath] ?? defaultValue
        } set: { value in
            guard var draft = draftPolicy else {
                return
            }
            draft[keyPath: keyPath] = value
            draftPolicy = draft
        }
    }
}

private struct DiagnosticsPanel: View {
    @ObservedObject var state: AppState

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                if let diagnostics = state.diagnostics {
                    GroupBox("Runtime") {
                        Grid(alignment: .leading, horizontalSpacing: 28, verticalSpacing: 10) {
                            InfoRow("Immich URL", diagnostics.immichUrl)
                            InfoRow("Database", diagnostics.databasePath)
                            InfoRow("Redis", diagnostics.redisEnabled ? "enabled" : "disabled")
                            InfoRow("Metrics", diagnostics.metricsEnabled ? "enabled" : "disabled")
                            InfoRow("WebDAV port", "\(diagnostics.webdavPort)")
                            InfoRow("Admin port", "\(diagnostics.adminPort)")
                            InfoRow("Last refresh", state.lastRefresh?.formatted(date: .omitted, time: .standard) ?? "Never")
                        }
                        .padding(6)
                    }

                    GroupBox("Session") {
                        Grid(alignment: .leading, horizontalSpacing: 28, verticalSpacing: 10) {
                            InfoRow("User", state.displayUser)
                            InfoRow("Email", state.session?.user?.email ?? "-")
                            InfoRow("API key", state.session?.user?.apiKeyName ?? "-")
                            InfoRow("Expires", state.session?.expiresAt ?? "-")
                        }
                        .padding(6)
                    }
                } else {
                    LoadingStateView(message: "Loading diagnostics")
                }
            }
            .padding(22)
        }
    }
}

private struct ViewEditorState: Identifiable {
    let id = UUID()
    var viewID: String?
    var payload: SavedViewPayload

    init(payload: SavedViewPayload) {
        self.payload = payload
    }

    init(view: SavedView) {
        viewID = view.id
        payload = view.payload
    }
}

private struct ViewEditorSheet: View {
    var title: String
    var initialPayload: SavedViewPayload
    var tags: [OptionItem]
    var people: [OptionItem]
    var isSaving: Bool
    var countProvider: (ViewFilters) async throws -> Int?
    var onCancel: () -> Void
    var onSave: (SavedViewPayload) async -> Bool

    @State private var draft: SavedViewPayload
    @State private var previewCount: Int?
    @State private var previewError: String?
    @State private var isCounting = false

    init(
        title: String,
        initialPayload: SavedViewPayload,
        tags: [OptionItem],
        people: [OptionItem],
        isSaving: Bool,
        countProvider: @escaping (ViewFilters) async throws -> Int?,
        onCancel: @escaping () -> Void,
        onSave: @escaping (SavedViewPayload) async -> Bool
    ) {
        self.title = title
        self.initialPayload = initialPayload
        self.tags = tags
        self.people = people
        self.isSaving = isSaving
        self.countProvider = countProvider
        self.onCancel = onCancel
        self.onSave = onSave
        _draft = State(initialValue: initialPayload)
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text(title)
                        .font(.title2.weight(.semibold))
                    Text(draft.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "/Views/<name>" : "/Views/\(draft.name)")
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button(action: onCancel) {
                    Image(systemName: "xmark")
                }
                .buttonStyle(.borderless)
            }
            .padding(20)

            Divider()

            Form {
                Section("General") {
                    TextField("Name", text: $draft.name)
                    TextField("Description", text: $draft.description)
                    Toggle("Enabled", isOn: $draft.enabled)
                    Picker("Layout", selection: $draft.layout) {
                        ForEach(ViewLayout.allCases) { layout in
                            Text(layout.label).tag(layout)
                        }
                    }
                    .pickerStyle(.segmented)
                }

                Section("Search") {
                    TextField("Search string", text: optionalStringBinding(\.query))
                    TextField("Original filename", text: optionalStringBinding(\.originalFileName))
                    TextField("OCR text", text: optionalStringBinding(\.ocr))
                    Toggle(
                        "Favorites only",
                        isOn: Binding(
                            get: { draft.filters.isFavorite == true },
                            set: { draft.filters.isFavorite = $0 ? true : nil }
                        )
                    )
                    Picker("Media", selection: optionalMediaBinding()) {
                        Text("Any").tag(MediaType?.none)
                        ForEach(MediaType.allCases) { mediaType in
                            Text(mediaType.label).tag(Optional(mediaType))
                        }
                    }
                    Picker("Rating", selection: optionalIntBinding(\.rating)) {
                        Text("Any").tag(Int?.none)
                        ForEach(1...5, id: \.self) { rating in
                            Text("\(rating)").tag(Optional(rating))
                        }
                    }
                }

                Section("Date and location") {
                    TextField("Taken after", text: optionalStringBinding(\.takenAfter))
                    TextField("Taken before", text: optionalStringBinding(\.takenBefore))
                    TextField("City", text: optionalStringBinding(\.city))
                    TextField("State", text: optionalStringBinding(\.state))
                    TextField("Country", text: optionalStringBinding(\.country))
                }

                OptionPickerSection(title: "Tags", options: tags, selectedIDs: $draft.filters.tagIds)
                OptionPickerSection(title: "People", options: people, selectedIDs: $draft.filters.personIds)
            }
            .formStyle(.grouped)

            Divider()

            HStack {
                Button {
                    Task {
                        await preview()
                    }
                } label: {
                    Label(isCounting ? "Counting" : "Preview Count", systemImage: "number")
                }
                .disabled(isCounting)

                if isCounting {
                    ProgressView()
                        .controlSize(.small)
                } else if let previewCount {
                    Text("\(previewCount) \(previewCount == 1 ? "asset" : "assets")")
                        .foregroundStyle(.secondary)
                } else if let previewError {
                    Text(previewError)
                        .foregroundStyle(.red)
                        .lineLimit(1)
                }

                Spacer()

                Button("Cancel", action: onCancel)
                Button {
                    Task {
                        if await onSave(draft.cleaned()) {
                            onCancel()
                        }
                    }
                } label: {
                    Label(isSaving ? "Saving" : "Save", systemImage: "square.and.arrow.down")
                }
                .buttonStyle(.borderedProminent)
                .disabled(isSaving || draft.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            .padding(16)
        }
        .frame(width: 760, height: 700)
    }

    private func preview() async {
        isCounting = true
        previewError = nil
        defer { isCounting = false }

        do {
            previewCount = try await countProvider(draft.cleaned().filters)
        } catch {
            previewCount = nil
            previewError = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }

    private func optionalStringBinding(_ keyPath: WritableKeyPath<ViewFilters, String?>) -> Binding<String> {
        Binding {
            draft.filters[keyPath: keyPath] ?? ""
        } set: { value in
            let clean = value.trimmingCharacters(in: .whitespacesAndNewlines)
            draft.filters[keyPath: keyPath] = clean.isEmpty ? nil : clean
        }
    }

    private func optionalIntBinding(_ keyPath: WritableKeyPath<ViewFilters, Int?>) -> Binding<Int?> {
        Binding {
            draft.filters[keyPath: keyPath]
        } set: { value in
            draft.filters[keyPath: keyPath] = value
        }
    }

    private func optionalMediaBinding() -> Binding<MediaType?> {
        Binding {
            draft.filters.mediaType
        } set: { value in
            draft.filters.mediaType = value
        }
    }
}

private struct OptionPickerSection: View {
    var title: String
    var options: [OptionItem]
    @Binding var selectedIDs: [String]
    @State private var search = ""

    private var visibleOptions: [OptionItem] {
        let query = search.trimmingCharacters(in: .whitespacesAndNewlines)
        let filtered = query.isEmpty
            ? options
            : options.filter { $0.name.localizedCaseInsensitiveContains(query) }
        return Array(filtered.prefix(100))
    }

    var body: some View {
        Section(title) {
            TextField("Search \(title.lowercased())", text: $search)
            if options.isEmpty {
                Text("No \(title.lowercased()) returned by Immich.")
                    .foregroundStyle(.secondary)
            } else {
                ForEach(visibleOptions) { option in
                    Toggle(isOn: selectionBinding(for: option.id)) {
                        HStack {
                            Text(option.name)
                            if option.hidden == true {
                                Text("hidden")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            if let assetCount = option.assetCount {
                                Text("\(assetCount)")
                                    .font(.caption.monospacedDigit())
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                }
                if options.count > visibleOptions.count {
                    Text("\(options.count - visibleOptions.count) more; refine search")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private func selectionBinding(for id: String) -> Binding<Bool> {
        Binding {
            selectedIDs.contains(id)
        } set: { isSelected in
            if isSelected {
                if !selectedIDs.contains(id) {
                    selectedIDs.append(id)
                }
            } else {
                selectedIDs.removeAll { $0 == id }
            }
        }
    }
}

private struct FilterChips: View {
    var filters: ViewFilters
    var tags: [OptionItem]
    var people: [OptionItem]

    var body: some View {
        let chips = filterChips(filters: filters, tags: tags, people: people)
        HStack(spacing: 6) {
            ForEach(chips, id: \.self) { chip in
                Text(chip)
                    .font(.caption)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(.quaternary, in: Capsule())
            }
        }
    }
}

private struct CountPill: View {
    var count: Int?

    var body: some View {
        VStack(spacing: 2) {
            Text(count.map(String.init) ?? "--")
                .font(.headline.monospacedDigit())
            Text(count == 1 ? "asset" : "assets")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(width: 74)
    }
}

private struct StatusPill: View {
    var text: String
    var isActive: Bool

    var body: some View {
        Text(text)
            .font(.caption)
            .padding(.horizontal, 7)
            .padding(.vertical, 2)
            .background(isActive ? .green.opacity(0.18) : .secondary.opacity(0.16), in: Capsule())
            .foregroundStyle(isActive ? .green : .secondary)
    }
}

private struct MountLine: View {
    var name: String
    var enabled: Bool

    var body: some View {
        HStack {
            Label(name, systemImage: enabled ? "folder" : "folder.badge.minus")
            Spacer()
            StatusPill(text: enabled ? "visible" : "hidden", isActive: enabled)
        }
    }
}

private struct MountsGroup: View {
    var mounts: [BridgeMount]

    var body: some View {
        GroupBox("Available mounts") {
            if mounts.isEmpty {
                HStack(spacing: 10) {
                    Image(systemName: "externaldrive.badge.questionmark")
                        .foregroundStyle(.secondary)
                    Text("No mounts are available for this login.")
                        .foregroundStyle(.secondary)
                    Spacer()
                }
                .padding(6)
            } else {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(mounts) { mount in
                        MountAccessLine(mount: mount)
                    }
                }
                .padding(6)
            }
        }
    }
}

private struct MountAccessLine: View {
    var mount: BridgeMount

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: mount.kind == "share" ? "link" : "externaldrive")
                .frame(width: 20)
            VStack(alignment: .leading, spacing: 2) {
                Text(mount.displayName)
                    .font(.body.weight(.medium))
                HStack(spacing: 8) {
                    Text(mount.kindLabel)
                    if let assetCountLabel = mount.assetCountLabel {
                        Text(assetCountLabel)
                    }
                    Text(mount.canUpload ? "uploads allowed" : "read-only")
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            Spacer()
            StatusPill(text: "not mounted", isActive: false)
        }
    }
}

private struct InfoRow: View {
    var label: String
    var value: String

    init(_ label: String, _ value: String) {
        self.label = label
        self.value = value
    }

    var body: some View {
        GridRow {
            Text(label)
                .foregroundStyle(.secondary)
            Text(value)
                .textSelection(.enabled)
        }
    }
}

private struct ErrorBanner: View {
    var message: String

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
            Text(message)
                .fixedSize(horizontal: false, vertical: true)
            Spacer()
        }
        .padding(12)
        .background(.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
    }
}

private struct LoadingStateView: View {
    var message: String

    var body: some View {
        HStack(spacing: 10) {
            ProgressView()
                .controlSize(.small)
            Text(message)
                .foregroundStyle(.secondary)
        }
        .padding(18)
    }
}

private func filterChips(filters: ViewFilters, tags: [OptionItem], people: [OptionItem]) -> [String] {
    let tagLookup = Dictionary(uniqueKeysWithValues: tags.map { ($0.id, $0.name) })
    let peopleLookup = Dictionary(uniqueKeysWithValues: people.map { ($0.id, $0.name) })
    var chips: [String] = []

    if filters.isFavorite == true {
        chips.append("favorites")
    }
    if let mediaType = filters.mediaType {
        chips.append(mediaType.label)
    }
    if let rating = filters.rating {
        chips.append("\(rating)-star")
    }
    if let query = filters.query, !query.isEmpty {
        chips.append("search: \(query)")
    }
    if let city = filters.city, !city.isEmpty {
        chips.append(city)
    }
    if let state = filters.state, !state.isEmpty {
        chips.append(state)
    }
    if let country = filters.country, !country.isEmpty {
        chips.append(country)
    }
    chips.append(contentsOf: filters.tagIds.prefix(3).map { tagLookup[$0] ?? $0 })
    chips.append(contentsOf: filters.personIds.prefix(3).map { peopleLookup[$0] ?? $0 })

    if chips.isEmpty {
        return ["all media"]
    }
    return Array(chips.prefix(8))
}

private extension SavedViewPayload {
    func cleaned() -> SavedViewPayload {
        var copy = self
        copy.name = copy.name.trimmingCharacters(in: .whitespacesAndNewlines)
        copy.description = copy.description.trimmingCharacters(in: .whitespacesAndNewlines)
        copy.filters.query = clean(copy.filters.query)
        copy.filters.originalFileName = clean(copy.filters.originalFileName)
        copy.filters.ocr = clean(copy.filters.ocr)
        copy.filters.city = clean(copy.filters.city)
        copy.filters.state = clean(copy.filters.state)
        copy.filters.country = clean(copy.filters.country)
        copy.filters.takenAfter = clean(copy.filters.takenAfter)
        copy.filters.takenBefore = clean(copy.filters.takenBefore)
        return copy
    }

    private func clean(_ value: String?) -> String? {
        let clean = value?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return clean.isEmpty ? nil : clean
    }
}
