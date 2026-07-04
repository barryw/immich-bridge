import AppKit
import SwiftUI

@main
struct ImmichFSApp: App {
    @StateObject private var state = AppState()

    var body: some Scene {
        MenuBarExtra("ImmichFS", systemImage: state.menuBarSymbol) {
            ImmichMenu(state: state)
        }
        .menuBarExtraStyle(.window)

        Settings {
            SettingsView(state: state)
                .frame(minWidth: 920, minHeight: 640)
        }
    }
}

private struct ImmichMenu: View {
    @ObservedObject var state: AppState

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            if state.shouldShowConnectForm {
                ConnectPopover(state: state)
            } else {
                ConnectedPopover(state: state)
            }
        }
        .padding(16)
        .frame(width: 380)
        .task {
            await state.bootstrap()
        }
    }
}

private struct ConnectPopover: View {
    @ObservedObject var state: AppState

    var body: some View {
        let isAddingShare = state.isAddingBridge && state.isViewer && state.connectionMode == .share
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 10) {
                Image(systemName: "externaldrive.badge.plus")
                    .font(.title2)
                    .foregroundStyle(.tint)
                VStack(alignment: .leading, spacing: 2) {
                    Text(isAddingShare ? "Add Share" : (state.isAddingBridge ? "Add Bridge" : "Connect Immich Bridge"))
                        .font(.headline)
                    Text(isAddingShare ? "Paste another Immich share link." : "Use admin credentials or an Immich share link.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }

            Picker("Access", selection: $state.connectionMode) {
                ForEach(BridgeAuthKind.allCases) { mode in
                    Label(mode.label, systemImage: mode.systemImage).tag(mode)
                }
            }
            .pickerStyle(.segmented)

            VStack(alignment: .leading, spacing: 8) {
                TextField("Bridge URL", text: $state.bridgeURLText)
                    .textContentType(.URL)
                    .textFieldStyle(.roundedBorder)

                if state.connectionMode == .admin {
                    TextField("Immich username or email", text: $state.username)
                        .textContentType(.username)
                        .textFieldStyle(.roundedBorder)
                    SecureField("API key or superadmin password", text: $state.apiKeyInput)
                        .textContentType(.password)
                        .textFieldStyle(.roundedBorder)
                } else {
                    TextField("Immich share URL", text: $state.shareURLInput)
                        .textContentType(.URL)
                        .textFieldStyle(.roundedBorder)
                }
            }

            if let errorMessage = state.errorMessage {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                    Text(errorMessage)
                        .font(.caption)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer()
                }
                .padding(10)
                .background(.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
            }

            HStack {
                Button {
                    Task {
                        await state.connect()
                    }
                } label: {
                    Label(
                        state.isLoading ? "Connecting" : (isAddingShare ? "Add Share" : "Connect"),
                        systemImage: "bolt.horizontal"
                    )
                }
                .buttonStyle(.borderedProminent)
                .disabled(state.isLoading || !state.canConnect)

                if state.isLoading {
                    ProgressView()
                        .controlSize(.small)
                }

                Spacer()

                if state.isAddingBridge {
                    Button("Cancel") {
                        state.cancelAddBridge()
                    }
                    .disabled(state.isLoading)
                }

                Button {
                    NSApp.terminate(nil)
                } label: {
                    Label("Quit", systemImage: "power")
                }
            }
        }
    }
}

private struct ConnectedPopover: View {
    @ObservedObject var state: AppState

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 10) {
                Image(systemName: state.canManageBridge ? "person.badge.key" : "link")
                    .font(.title2)
                    .foregroundStyle(.tint)
                VStack(alignment: .leading, spacing: 2) {
                    Text(state.activeProfile?.displayName ?? "Immich Bridge")
                        .font(.headline)
                        .lineLimit(1)
                    HStack(spacing: 6) {
                        RoleBadge(role: state.roleLabel)
                        Text(state.statusText)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                    }
                }
                Spacer()
            }

            if state.profiles.count > 1 {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Bridges")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                    ForEach(state.profiles) { profile in
                        Button {
                            Task {
                                await state.activateProfile(profile)
                            }
                        } label: {
                            HStack {
                                Image(systemName: profile.id == state.selectedProfileID ? "checkmark.circle.fill" : "circle")
                                    .foregroundColor(profile.id == state.selectedProfileID ? .accentColor : .secondary)
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(profile.displayName)
                                        .lineLimit(1)
                                    Text("\(profile.hostLabel) - \(profile.roleLabel)")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                                Spacer()
                            }
                        }
                        .buttonStyle(.plain)
                    }
                }
            }

            VStack(alignment: .leading, spacing: 8) {
                Text("Volumes")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                if state.availableMounts.isEmpty {
                    Text("No mounts available for this login.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(state.availableMounts) { mount in
                        MenuMountRow(mount: mount)
                    }
                }
            }

            if let errorMessage = state.errorMessage {
                Text(errorMessage)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Divider()

            HStack(spacing: 10) {
                SettingsLink {
                    Label("Settings", systemImage: "gearshape")
                }

                Button {
                    if state.isViewer {
                        state.beginAddShare()
                    } else {
                        state.beginAddBridge()
                    }
                } label: {
                    Label(state.isViewer ? "Add Share" : "Add Bridge", systemImage: "plus")
                }

                Button {
                    Task {
                        await state.signOut()
                    }
                } label: {
                    Label("Sign Out", systemImage: "rectangle.portrait.and.arrow.right")
                }
                .disabled(state.isLoading)

                Spacer()

                Button {
                    Task {
                        await state.refreshAll()
                    }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .help("Refresh")
                .disabled(state.isLoading)

                Button {
                    NSApp.terminate(nil)
                } label: {
                    Image(systemName: "power")
                }
                .help("Quit")
            }
        }
    }
}

private struct MenuMountRow: View {
    var mount: BridgeMount

    var body: some View {
        HStack(spacing: 9) {
            Image(systemName: mount.kind == "share" ? "link" : "externaldrive")
                .frame(width: 18)
            VStack(alignment: .leading, spacing: 1) {
                Text(mount.displayName)
                    .lineLimit(1)
                HStack(spacing: 6) {
                    Text(mount.kindLabel)
                    if let assetCountLabel = mount.assetCountLabel {
                        Text(assetCountLabel)
                    }
                    Text(mount.canUpload ? "upload" : "read-only")
                }
                .font(.caption2)
                .foregroundStyle(.secondary)
            }
            Spacer()
            Text("pending")
                .font(.caption2.weight(.medium))
                .foregroundStyle(.secondary)
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(.quaternary, in: Capsule())
        }
    }
}
