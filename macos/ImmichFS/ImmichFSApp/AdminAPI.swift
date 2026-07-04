import Foundation

enum AdminAPIError: LocalizedError {
    case invalidBridgeURL
    case emptyResponse
    case unauthorized(String)
    case httpStatus(Int, String)

    var errorDescription: String? {
        switch self {
        case .invalidBridgeURL:
            "Enter a valid Immich Bridge URL."
        case .emptyResponse:
            "The bridge returned an empty response."
        case let .unauthorized(message):
            message
        case let .httpStatus(_, message):
            message
        }
    }

    var isUnauthorized: Bool {
        if case .unauthorized = self {
            return true
        }
        return false
    }
}

final class AdminAPI {
    var sessionToken: String?

    private let baseURL: URL
    private let session: URLSession
    private let decoder: JSONDecoder
    private let encoder: JSONEncoder

    init(baseURL: URL, sessionToken: String? = nil, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.sessionToken = sessionToken
        self.session = session
        decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
    }

    func login(username: String, apiKey: String) async throws -> AdminSession {
        let response: AdminSession = try await send(
            "/api/admin/session",
            method: "POST",
            body: AdminLoginRequest(username: username, apiKey: apiKey)
        )
        sessionToken = response.sessionToken
        return response
    }

    func shareLogin(shareURL: String) async throws -> AdminSession {
        let response: AdminSession = try await send(
            "/api/auth/share-link",
            method: "POST",
            body: ShareLoginRequest(shareUrl: shareURL)
        )
        sessionToken = response.sessionToken
        return response
    }

    func addShareLink(shareURL: String) async throws -> AdminSession {
        try await send(
            "/api/auth/session/share-link",
            method: "POST",
            body: ShareLoginRequest(shareUrl: shareURL)
        )
    }

    func sessionStatus() async throws -> AdminSession {
        try await send("/api/admin/session")
    }

    func currentPrincipal() async throws -> AdminSession {
        try await send("/api/me")
    }

    func availableMounts() async throws -> [BridgeMount] {
        let response: MountsResponse = try await send("/api/me/mounts")
        return response.mounts
    }

    func libraries() async throws -> [BridgeLibrary] {
        let response: LibrariesResponse = try await send("/api/admin/libraries")
        return response.libraries
    }

    func logout() async throws {
        let _: EmptyResponse = try await send("/api/admin/session", method: "DELETE")
        sessionToken = nil
    }

    func logoutCurrentSession() async throws {
        let _: EmptyResponse = try await send("/api/auth/session", method: "DELETE")
        sessionToken = nil
    }

    func diagnostics() async throws -> Diagnostics {
        try await send("/api/admin/diagnostics")
    }

    func mountSettings() async throws -> MountSettings {
        try await send("/api/admin/mount")
    }

    func updateMountSettings(_ settings: MountSettings) async throws -> MountSettings {
        try await send("/api/admin/mount", method: "PUT", body: settings)
    }

    func writePolicy() async throws -> WritePolicy {
        try await send("/api/admin/write-policy")
    }

    func updateWritePolicy(_ policy: WritePolicy) async throws -> WritePolicy {
        try await send("/api/admin/write-policy", method: "PUT", body: policy)
    }

    func views(includeCounts: Bool = true) async throws -> [SavedView] {
        let response: ViewsResponse = try await send(
            "/api/admin/views?include_counts=\(includeCounts ? "true" : "false")"
        )
        return response.views
    }

    func createView(_ payload: SavedViewPayload) async throws -> SavedView {
        try await send("/api/admin/views", method: "POST", body: payload)
    }

    func updateView(id: String, payload: SavedViewPayload) async throws -> SavedView {
        try await send("/api/admin/views/\(id)", method: "PUT", body: payload)
    }

    func deleteView(id: String) async throws {
        let _: EmptyResponse = try await send("/api/admin/views/\(id)", method: "DELETE")
    }

    func matchCount(filters: ViewFilters) async throws -> Int? {
        let response: MatchCountResponse = try await send(
            "/api/admin/views/match-count",
            method: "POST",
            body: MatchCountRequest(filters: filters)
        )
        return response.count
    }

    func tagOptions() async throws -> [OptionItem] {
        let response: OptionsResponse = try await send("/api/admin/options/tags")
        return response.items
    }

    func peopleOptions() async throws -> [OptionItem] {
        let response: OptionsResponse = try await send("/api/admin/options/people")
        return response.items
    }

    private func send<Response: Decodable>(_ path: String, method: String = "GET") async throws -> Response {
        try await send(path, method: method, bodyData: nil)
    }

    private func send<Response: Decodable, Body: Encodable>(
        _ path: String,
        method: String,
        body: Body
    ) async throws -> Response {
        try await send(path, method: method, bodyData: encoder.encode(body))
    }

    private func send<Response: Decodable>(
        _ path: String,
        method: String,
        bodyData: Data?
    ) async throws -> Response {
        var request = URLRequest(url: try endpoint(path))
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let bodyData {
            request.httpBody = bodyData
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        if let sessionToken {
            request.setValue("Bearer \(sessionToken)", forHTTPHeaderField: "Authorization")
        }

        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw AdminAPIError.httpStatus(0, "The bridge did not return an HTTP response.")
        }

        if !(200..<300).contains(http.statusCode) {
            let message = decodeErrorMessage(from: data) ?? "\(http.statusCode) \(HTTPURLResponse.localizedString(forStatusCode: http.statusCode))"
            if http.statusCode == 401 {
                throw AdminAPIError.unauthorized(message)
            }
            throw AdminAPIError.httpStatus(http.statusCode, message)
        }

        if data.isEmpty {
            if Response.self == EmptyResponse.self {
                return EmptyResponse() as! Response
            }
            throw AdminAPIError.emptyResponse
        }

        return try decoder.decode(Response.self, from: data)
    }

    private func endpoint(_ path: String) throws -> URL {
        var base = baseURL.absoluteString
        while base.hasSuffix("/") {
            base.removeLast()
        }
        guard let url = URL(string: base + path) else {
            throw AdminAPIError.invalidBridgeURL
        }
        return url
    }

    private func decodeErrorMessage(from data: Data) -> String? {
        guard !data.isEmpty else {
            return nil
        }
        return try? decoder.decode(ErrorResponse.self, from: data).detail
    }
}

private struct ErrorResponse: Decodable {
    var detail: String?
}
