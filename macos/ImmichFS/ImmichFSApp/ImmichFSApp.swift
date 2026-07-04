import AppKit
import SwiftUI

@main
struct ImmichFSApp: App {
    @StateObject private var state = AppState()

    var body: some Scene {
        MenuBarExtra("ImmichFS", systemImage: state.menuBarSymbol) {
            ImmichMenu(state: state)
        }

        Settings {
            SettingsView(state: state)
                .frame(minWidth: 920, minHeight: 640)
        }
    }
}

private struct ImmichMenu: View {
    @ObservedObject var state: AppState

    var body: some View {
        Text(state.statusText)

        Divider()

        Button("Refresh Admin State") {
            Task {
                await state.refreshAll()
            }
        }
        .disabled(!state.isAuthenticated)

        Divider()

        SettingsLink {
            Text("Immich Bridge Settings")
        }

        Divider()

        Button("Quit ImmichFS") {
            NSApp.terminate(nil)
        }
    }
}
