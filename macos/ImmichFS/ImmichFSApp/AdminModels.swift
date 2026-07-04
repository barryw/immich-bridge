import Foundation

struct AdminLoginRequest: Encodable {
    var username: String
    var apiKey: String
}

struct ShareLoginRequest: Encodable {
    var shareUrl: String
}

struct AdminUser: Codable, Equatable {
    var id: String
    var email: String?
    var name: String?
    var apiKeyName: String?
}

struct Principal: Codable, Equatable {
    var id: String
    var kind: String
    var displayName: String?

    var roleLabel: String {
        switch kind {
        case "superadmin":
            "Superadmin"
        case "immich_admin":
            "Library admin"
        case "share_guest":
            "Viewer"
        default:
            kind
        }
    }
}

struct Grant: Codable, Equatable {
    var scope: String
    var libraryId: String?
    var shareId: String?
    var shareName: String?
    var shareKeyHash: String?
    var allowDownload: Bool?
    var allowUpload: Bool?
    var assetCount: Int?
    var expiresAt: String?
    var capabilities: [String]

    enum CodingKeys: String, CodingKey {
        case scope
        case libraryId
        case shareId
        case shareName
        case shareKeyHash
        case allowDownload
        case allowUpload
        case assetCount
        case expiresAt
        case capabilities
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        scope = try container.decodeIfPresent(String.self, forKey: .scope) ?? ""
        libraryId = try container.decodeIfPresent(String.self, forKey: .libraryId)
        shareId = try container.decodeIfPresent(String.self, forKey: .shareId)
        shareName = try container.decodeIfPresent(String.self, forKey: .shareName)
        shareKeyHash = try container.decodeIfPresent(String.self, forKey: .shareKeyHash)
        allowDownload = try container.decodeIfPresent(Bool.self, forKey: .allowDownload)
        allowUpload = try container.decodeIfPresent(Bool.self, forKey: .allowUpload)
        assetCount = try container.decodeIfPresent(Int.self, forKey: .assetCount)
        expiresAt = try container.decodeIfPresent(String.self, forKey: .expiresAt)
        capabilities = try container.decodeIfPresent([String].self, forKey: .capabilities) ?? []
    }

    func hasCapability(_ capability: String) -> Bool {
        capabilities.contains(capability)
    }
}

struct AdminSession: Codable, Equatable {
    var authenticated: Bool
    var user: AdminUser?
    var principal: Principal?
    var grants: [Grant]
    var grantToken: String?
    var expiresAt: String?
    var sessionToken: String?

    enum CodingKeys: String, CodingKey {
        case authenticated
        case user
        case principal
        case grants
        case grantToken
        case expiresAt
        case sessionToken
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        authenticated = try container.decodeIfPresent(Bool.self, forKey: .authenticated) ?? false
        user = try container.decodeIfPresent(AdminUser.self, forKey: .user)
        principal = try container.decodeIfPresent(Principal.self, forKey: .principal)
        grants = try container.decodeIfPresent([Grant].self, forKey: .grants) ?? []
        grantToken = try container.decodeIfPresent(String.self, forKey: .grantToken)
        expiresAt = try container.decodeIfPresent(String.self, forKey: .expiresAt)
        sessionToken = try container.decodeIfPresent(String.self, forKey: .sessionToken)
    }

    var displayName: String {
        principal?.displayName
            ?? user?.name
            ?? user?.email
            ?? "Immich Bridge user"
    }

    var isAdminCapable: Bool {
        if principal?.kind == "superadmin" || principal?.kind == "immich_admin" {
            return true
        }
        if principal == nil, user != nil, grants.isEmpty {
            return true
        }
        return grants.contains { grant in
            grant.hasCapability("manage_instance")
                || grant.hasCapability("manage_library")
                || grant.hasCapability("manage_policy")
                || grant.hasCapability("manage_views")
        }
    }
}

enum BridgeAuthKind: String, Codable, CaseIterable, Identifiable {
    case admin
    case share

    var id: String { rawValue }

    var label: String {
        switch self {
        case .admin: "Admin"
        case .share: "Share Link"
        }
    }

    var systemImage: String {
        switch self {
        case .admin: "person.badge.key"
        case .share: "link"
        }
    }
}

struct BridgeMount: Codable, Identifiable, Equatable {
    var id: String
    var kind: String
    var libraryId: String?
    var libraryName: String?
    var displayName: String
    var scope: String
    var capabilities: [String]
    var shareId: String?
    var assetCount: Int?
    var expiresAt: String?

    init(
        id: String,
        kind: String,
        libraryId: String? = nil,
        libraryName: String? = nil,
        displayName: String,
        scope: String,
        capabilities: [String],
        shareId: String? = nil,
        assetCount: Int? = nil,
        expiresAt: String? = nil
    ) {
        self.id = id
        self.kind = kind
        self.libraryId = libraryId
        self.libraryName = libraryName
        self.displayName = displayName
        self.scope = scope
        self.capabilities = capabilities
        self.shareId = shareId
        self.assetCount = assetCount
        self.expiresAt = expiresAt
    }

    enum CodingKeys: String, CodingKey {
        case id
        case kind
        case libraryId
        case libraryName
        case displayName
        case scope
        case capabilities
        case shareId
        case assetCount
        case expiresAt
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        kind = try container.decodeIfPresent(String.self, forKey: .kind) ?? "library"
        libraryId = try container.decodeIfPresent(String.self, forKey: .libraryId)
        libraryName = try container.decodeIfPresent(String.self, forKey: .libraryName)
        displayName = try container.decodeIfPresent(String.self, forKey: .displayName)
            ?? libraryName
            ?? id
        scope = try container.decodeIfPresent(String.self, forKey: .scope) ?? kind
        capabilities = try container.decodeIfPresent([String].self, forKey: .capabilities) ?? []
        shareId = try container.decodeIfPresent(String.self, forKey: .shareId)
        assetCount = try container.decodeIfPresent(Int.self, forKey: .assetCount)
        expiresAt = try container.decodeIfPresent(String.self, forKey: .expiresAt)
    }

    var kindLabel: String {
        kind == "share" ? "Shared album" : "Library"
    }

    var canUpload: Bool {
        capabilities.contains("upload")
    }

    var assetCountLabel: String? {
        guard let assetCount else {
            return nil
        }
        return "\(assetCount) \(assetCount == 1 ? "asset" : "assets")"
    }
}

struct MountsResponse: Codable {
    var mounts: [BridgeMount]

    enum CodingKeys: String, CodingKey {
        case mounts
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        mounts = try container.decodeIfPresent([BridgeMount].self, forKey: .mounts) ?? []
    }
}

struct BridgeLibrary: Codable, Identifiable, Equatable {
    var id: String
    var name: String
    var immichUrl: String
    var publicUrl: String?
    var shareHosts: [String]
    var isDefault: Bool
    var createdAt: String
    var updatedAt: String
}

struct LibrariesResponse: Codable {
    var libraries: [BridgeLibrary]
}

struct BridgeProfile: Codable, Identifiable, Equatable {
    var id: UUID
    var bridgeURL: String
    var displayName: String
    var authKind: BridgeAuthKind
    var username: String?
    var principalKind: String?
    var principalName: String?
    var mounts: [BridgeMount]
    var sessionExpiresAt: String?
    var lastConnectedAt: Date?

    init(
        id: UUID = UUID(),
        bridgeURL: String,
        displayName: String,
        authKind: BridgeAuthKind,
        username: String? = nil,
        principalKind: String? = nil,
        principalName: String? = nil,
        mounts: [BridgeMount] = [],
        sessionExpiresAt: String? = nil,
        lastConnectedAt: Date? = nil
    ) {
        self.id = id
        self.bridgeURL = bridgeURL
        self.displayName = displayName
        self.authKind = authKind
        self.username = username
        self.principalKind = principalKind
        self.principalName = principalName
        self.mounts = mounts
        self.sessionExpiresAt = sessionExpiresAt
        self.lastConnectedAt = lastConnectedAt
    }

    var hostLabel: String {
        URL(string: bridgeURL)?.host ?? bridgeURL
    }

    var roleLabel: String {
        switch principalKind {
        case "superadmin":
            "Superadmin"
        case "immich_admin":
            "Library admin"
        case "share_guest":
            "Viewer"
        default:
            authKind.label
        }
    }
}

struct ViewFilters: Codable, Equatable {
    var albumIds: [String] = []
    var personIds: [String] = []
    var tagIds: [String] = []
    var isFavorite: Bool?
    var mediaType: MediaType?
    var takenAfter: String?
    var takenBefore: String?
    var rating: Int?
    var query: String?
    var originalFileName: String?
    var ocr: String?
    var city: String?
    var state: String?
    var country: String?

    static let empty = ViewFilters()
}

enum MediaType: String, Codable, CaseIterable, Identifiable {
    case image = "IMAGE"
    case video = "VIDEO"

    var id: String { rawValue }

    var label: String {
        switch self {
        case .image: "Images"
        case .video: "Videos"
        }
    }
}

enum ViewLayout: String, Codable, CaseIterable, Identifiable {
    case dateBuckets = "date_buckets"
    case flat

    var id: String { rawValue }

    var label: String {
        switch self {
        case .dateBuckets: "Date buckets"
        case .flat: "Flat"
        }
    }
}

struct SavedViewPayload: Codable, Equatable {
    var name: String = ""
    var description: String = ""
    var enabled: Bool = true
    var layout: ViewLayout = .dateBuckets
    var filters: ViewFilters = .empty
}

struct SavedView: Codable, Identifiable, Equatable {
    var id: String
    var name: String
    var description: String
    var enabled: Bool
    var layout: ViewLayout
    var filters: ViewFilters
    var createdAt: String
    var updatedAt: String
    var matchCount: Int?

    var payload: SavedViewPayload {
        SavedViewPayload(
            name: name,
            description: description,
            enabled: enabled,
            layout: layout,
            filters: filters
        )
    }
}

struct ViewsResponse: Codable {
    var views: [SavedView]
}

struct MatchCountRequest: Encodable {
    var filters: ViewFilters
}

struct MatchCountResponse: Decodable {
    var count: Int?
}

struct OptionItem: Codable, Identifiable, Equatable {
    var id: String
    var name: String
    var color: String?
    var assetCount: Int?
    var hidden: Bool?
}

struct OptionsResponse: Codable {
    var items: [OptionItem]
}

enum FilenameMode: String, Codable, CaseIterable, Identifiable {
    case dateOriginalId = "date-original-id"
    case original
    case stable

    var id: String { rawValue }

    var label: String {
        switch self {
        case .dateOriginalId: "Date, original name, ID"
        case .original: "Original filename"
        case .stable: "Stable ID"
        }
    }
}

struct MountSettings: Codable, Equatable {
    var albumsEnabled: Bool = true
    var timelineEnabled: Bool = true
    var favoritesEnabled: Bool = true
    var viewsEnabled: Bool = true
    var tagsEnabled: Bool = false
    var peopleEnabled: Bool = false
    var albumFolderSplitThreshold: Int = 200
    var dayFolderSplitThreshold: Int = 1000
    var filenameMode: FilenameMode = .dateOriginalId
}

struct WritePolicy: Codable, Equatable {
    var rootUploads: Bool = true
    var albumUploads: Bool = true
    var albumCreate: Bool = true
    var albumMembershipDelete: Bool = true
    var permanentDelete: Bool = false
    var moveCopy: Bool = false
    var overwrite: Bool = false
}

struct Diagnostics: Codable, Equatable {
    var immichUrl: String
    var databasePath: String
    var redisEnabled: Bool
    var metricsEnabled: Bool
    var webdavPort: Int
    var adminPort: Int
    var viewCount: Int
    var mount: MountSettings
    var writePolicy: WritePolicy
}

struct EmptyResponse: Decodable {}
