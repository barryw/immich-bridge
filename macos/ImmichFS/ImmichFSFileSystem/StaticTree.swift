import Foundation
import FSKit

struct StaticNode {
    var id: UInt64
    var parentID: UInt64
    var name: String
    var itemType: FSItem.ItemType
    var contents: Data?
    var children: [StaticNode]

    var isDirectory: Bool {
        itemType == .directory
    }
}

struct StaticTree {
    static let rootID: UInt64 = 2

    private let root: StaticNode
    private let nodesByID: [UInt64: StaticNode]

    var nodeCount: Int {
        nodesByID.count
    }

    init() {
        let albumsReadme = StaticNode(
            id: 7,
            parentID: 4,
            name: "README.txt",
            itemType: .file,
            contents: Self.data("""
            Albums will expose Immich albums as directories.

            The native filesystem scaffold is currently read-only and static.
            Next step: enumerate album nodes from /api/fs/v1.
            """),
            children: []
        )

        let timelineReadme = StaticNode(
            id: 8,
            parentID: 5,
            name: "README.txt",
            itemType: .file,
            contents: Self.data("""
            Timeline will expose date-bucketed Immich assets.

            The bridge API owns pagination, ordering, thumbnails, and write policy.
            """),
            children: []
        )

        let viewsReadme = StaticNode(
            id: 9,
            parentID: 6,
            name: "README.txt",
            itemType: .file,
            contents: Self.data("""
            Views will expose configured searches, tags, people, and locations.

            Saved views are managed by the Immich Bridge admin API.
            """),
            children: []
        )

        let albums = StaticNode(id: 4, parentID: Self.rootID, name: "Albums", itemType: .directory, contents: nil, children: [albumsReadme])
        let timeline = StaticNode(id: 5, parentID: Self.rootID, name: "Timeline", itemType: .directory, contents: nil, children: [timelineReadme])
        let views = StaticNode(id: 6, parentID: Self.rootID, name: "Views", itemType: .directory, contents: nil, children: [viewsReadme])
        let rootReadme = StaticNode(
            id: 3,
            parentID: Self.rootID,
            name: "README.txt",
            itemType: .file,
            contents: Self.data("""
            ImmichFS is the native filesystem client for Immich Bridge.

            This first mount is a static, read-only FSKit scaffold. It proves the
            macOS app, extension packaging, volume metadata, directory enumeration,
            and file reads before the tree is backed by /api/fs/v1.
            """),
            children: []
        )

        root = StaticNode(id: Self.rootID, parentID: Self.rootID, name: "", itemType: .directory, contents: nil, children: [
            rootReadme,
            albums,
            timeline,
            views
        ])
        nodesByID = Self.flatten(root)
    }

    func node(id: UInt64) -> StaticNode? {
        nodesByID[id]
    }

    func children(of node: StaticNode) -> [StaticNode] {
        nodesByID[node.id]?.children ?? []
    }

    func child(named name: String, in parent: StaticNode) -> StaticNode? {
        children(of: parent).first { $0.name.compare(name, options: [.caseInsensitive, .diacriticInsensitive]) == .orderedSame }
    }

    private static func flatten(_ node: StaticNode) -> [UInt64: StaticNode] {
        var values = [node.id: node]

        for child in node.children {
            values.merge(flatten(child)) { current, _ in current }
        }

        return values
    }

    private static func data(_ string: String) -> Data {
        Data((string + "\n").utf8)
    }
}

