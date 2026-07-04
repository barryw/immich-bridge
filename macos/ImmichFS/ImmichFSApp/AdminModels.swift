import Foundation

struct AdminLoginRequest: Encodable {
    var username: String
    var apiKey: String
}

struct AdminUser: Codable, Equatable {
    var id: String
    var email: String?
    var name: String?
    var apiKeyName: String?
}

struct AdminSession: Codable, Equatable {
    var authenticated: Bool
    var user: AdminUser?
    var expiresAt: String?
    var sessionToken: String?
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
