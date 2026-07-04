import Foundation
import Security

enum KeychainStore {
    enum Account: String {
        case adminApiKey = "admin-api-key"
        case sessionToken = "admin-session-token"
    }

    enum KeychainError: LocalizedError {
        case unhandled(OSStatus)

        var errorDescription: String? {
            switch self {
            case let .unhandled(status):
                "Keychain operation failed with status \(status)."
            }
        }
    }

    private static let service = "io.immichbridge.ImmichFS"

    static func save(_ value: String, account: Account) throws {
        try save(value, accountName: account.rawValue)
    }

    static func save(_ value: String, accountName: String) throws {
        let data = Data(value.utf8)
        var query = baseQuery(accountName: accountName)
        SecItemDelete(query as CFDictionary)

        query[kSecValueData as String] = data
        query[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly

        let status = SecItemAdd(query as CFDictionary, nil)
        guard status == errSecSuccess else {
            throw KeychainError.unhandled(status)
        }
    }

    static func read(account: Account) throws -> String? {
        try read(accountName: account.rawValue)
    }

    static func read(accountName: String) throws -> String? {
        var query = baseQuery(accountName: accountName)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne

        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound {
            return nil
        }
        guard status == errSecSuccess else {
            throw KeychainError.unhandled(status)
        }
        guard let data = result as? Data else {
            return nil
        }
        return String(data: data, encoding: .utf8)
    }

    static func delete(account: Account) throws {
        try delete(accountName: account.rawValue)
    }

    static func delete(accountName: String) throws {
        let status = SecItemDelete(baseQuery(accountName: accountName) as CFDictionary)
        if status == errSecItemNotFound {
            return
        }
        guard status == errSecSuccess else {
            throw KeychainError.unhandled(status)
        }
    }

    private static func baseQuery(accountName: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: accountName
        ]
    }
}
