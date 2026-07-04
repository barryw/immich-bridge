import Darwin
import Foundation
import FSKit

@objc
final class ImmichFSFileSystem: FSUnaryFileSystem, FSUnaryFileSystemOperations {
    private static let stableUUID = UUID(uuidString: "B18842E8-158C-43C5-B3D4-F05D7A4A1A8C")!

    func probeResource(resource: FSResource, replyHandler: @escaping (FSProbeResult?, (any Error)?) -> Void) {
        let containerID = FSContainerIdentifier(uuid: Self.stableUUID)
        replyHandler(.usable(name: "ImmichFS", containerID: containerID), nil)
    }

    func loadResource(resource: FSResource, options: FSTaskOptions, replyHandler: @escaping (FSVolume?, (any Error)?) -> Void) {
        let volumeID = FSVolume.Identifier(uuid: Self.stableUUID)
        let volumeName = FSFileName(string: "ImmichFS")
        replyHandler(ImmichStaticVolume(volumeID: volumeID, volumeName: volumeName), nil)
    }

    func unloadResource(resource: FSResource, options: FSTaskOptions) async throws {
    }
}

@objc
final class ImmichStaticVolume: FSVolume, FSVolume.Operations, FSVolume.ReadWriteOperations {
    private let tree = StaticTree()
    private let createdAt = timespec(tv_sec: 1_783_123_200, tv_nsec: 0)

    override init(volumeID: FSVolume.Identifier, volumeName: FSFileName) {
        super.init(volumeID: volumeID, volumeName: volumeName)
    }

    var supportedVolumeCapabilities: FSVolume.SupportedCapabilities {
        let capabilities = FSVolume.SupportedCapabilities()
        capabilities.supportsPersistentObjectIDs = true
        capabilities.supportsFastStatFS = true
        capabilities.supports2TBFiles = true
        capabilities.supports64BitObjectIDs = true
        capabilities.supportsHiddenFiles = false
        capabilities.doesNotSupportImmutableFiles = true
        capabilities.doesNotSupportSettingFilePermissions = true
        capabilities.doesNotSupportVolumeSizes = true
        capabilities.caseFormat = .insensitiveCasePreserving
        return capabilities
    }

    var volumeStatistics: FSStatFSResult {
        let stats = FSStatFSResult(fileSystemTypeName: "immichfs")
        stats.blockSize = 4096
        stats.ioSize = 1024 * 1024
        stats.totalFiles = UInt64(tree.nodeCount)
        stats.freeFiles = 0
        return stats
    }

    var maximumLinkCount: Int {
        1
    }

    var maximumNameLength: Int {
        255
    }

    var restrictsOwnershipChanges: Bool {
        true
    }

    var truncatesLongNames: Bool {
        false
    }

    var maximumFileSize: UInt64 {
        UInt64(Int64.max)
    }

    func mount(options: FSTaskOptions, replyHandler: @escaping ((any Error)?) -> Void) {
        replyHandler(nil)
    }

    func unmount(replyHandler: @escaping () -> Void) {
        replyHandler()
    }

    func synchronize(flags: FSSyncFlags, replyHandler: @escaping ((any Error)?) -> Void) {
        replyHandler(nil)
    }

    func activate(options: FSTaskOptions, replyHandler: @escaping (FSItem?, (any Error)?) -> Void) {
        replyHandler(ImmichStaticItem(nodeID: StaticTree.rootID), nil)
    }

    func deactivate(options: FSDeactivateOptions, replyHandler: @escaping ((any Error)?) -> Void) {
        replyHandler(nil)
    }

    func getAttributes(_ desiredAttributes: FSItem.GetAttributesRequest, of item: FSItem, replyHandler: @escaping (FSItem.Attributes?, (any Error)?) -> Void) {
        guard let node = node(for: item) else {
            replyHandler(nil, posixError(ENOENT))
            return
        }

        replyHandler(attributes(for: node), nil)
    }

    func setAttributes(_ newAttributes: FSItem.SetAttributesRequest, on item: FSItem, replyHandler: @escaping (FSItem.Attributes?, (any Error)?) -> Void) {
        replyHandler(nil, posixError(EROFS))
    }

    func lookupItem(named name: FSFileName, inDirectory directory: FSItem, replyHandler: @escaping (FSItem?, FSFileName?, (any Error)?) -> Void) {
        guard let parent = node(for: directory), parent.isDirectory else {
            replyHandler(nil, nil, posixError(ENOTDIR))
            return
        }

        guard let requestedName = name.string, !requestedName.isEmpty else {
            replyHandler(nil, nil, posixError(EINVAL))
            return
        }

        if requestedName == "." {
            replyHandler(ImmichStaticItem(nodeID: parent.id), FSFileName(string: parent.name), nil)
            return
        }

        if requestedName == "..", let parentNode = tree.node(id: parent.parentID) {
            replyHandler(ImmichStaticItem(nodeID: parentNode.id), FSFileName(string: parentNode.name), nil)
            return
        }

        guard let child = tree.child(named: requestedName, in: parent) else {
            replyHandler(nil, nil, posixError(ENOENT))
            return
        }

        replyHandler(ImmichStaticItem(nodeID: child.id), FSFileName(string: child.name), nil)
    }

    func reclaimItem(_ item: FSItem, replyHandler: @escaping ((any Error)?) -> Void) {
        replyHandler(nil)
    }

    func readSymbolicLink(_ item: FSItem, replyHandler: @escaping (FSFileName?, (any Error)?) -> Void) {
        replyHandler(nil, posixError(EINVAL))
    }

    func createItem(named name: FSFileName, type: FSItem.ItemType, inDirectory directory: FSItem, attributes newAttributes: FSItem.SetAttributesRequest, replyHandler: @escaping (FSItem?, FSFileName?, (any Error)?) -> Void) {
        replyHandler(nil, nil, posixError(EROFS))
    }

    func createSymbolicLink(named name: FSFileName, inDirectory directory: FSItem, attributes newAttributes: FSItem.SetAttributesRequest, linkContents contents: FSFileName, replyHandler: @escaping (FSItem?, FSFileName?, (any Error)?) -> Void) {
        replyHandler(nil, nil, posixError(EROFS))
    }

    @objc(createLinkToItem:named:inDirectory:replyHandler:)
    func createLink(to item: FSItem, named name: FSFileName, inDirectory directory: FSItem, replyHandler: @escaping (FSFileName?, (any Error)?) -> Void) {
        replyHandler(nil, posixError(EROFS))
    }

    func removeItem(_ item: FSItem, named name: FSFileName, fromDirectory directory: FSItem, replyHandler: @escaping ((any Error)?) -> Void) {
        replyHandler(posixError(EROFS))
    }

    func renameItem(_ item: FSItem, inDirectory sourceDirectory: FSItem, named sourceName: FSFileName, to destinationName: FSFileName, inDirectory destinationDirectory: FSItem, overItem: FSItem?, replyHandler: @escaping (FSFileName?, (any Error)?) -> Void) {
        replyHandler(nil, posixError(EROFS))
    }

    func enumerateDirectory(_ directory: FSItem, startingAt cookie: FSDirectoryCookie, verifier: FSDirectoryVerifier, attributes requestedAttributes: FSItem.GetAttributesRequest?, packer: FSDirectoryEntryPacker, replyHandler: @escaping (FSDirectoryVerifier, (any Error)?) -> Void) {
        guard let node = node(for: directory), node.isDirectory else {
            replyHandler(FSDirectoryVerifier(rawValue: 0), posixError(ENOTDIR))
            return
        }

        let entries = directoryEntries(for: node, includeDotEntries: requestedAttributes == nil)
        let startIndex = Int(cookie.rawValue)
        guard startIndex <= entries.count else {
            replyHandler(FSDirectoryVerifier(rawValue: 0), posixError(EINVAL))
            return
        }

        for index in startIndex..<entries.count {
            let entry = entries[index]
            let nextCookie = FSDirectoryCookie(rawValue: UInt64(index + 1))
            let entryAttributes = requestedAttributes == nil ? nil : attributes(for: entry.node)
            let packed = packer.packEntry(
                name: FSFileName(string: entry.name),
                itemType: entry.node.itemType,
                itemID: itemID(entry.node.id),
                nextCookie: nextCookie,
                attributes: entryAttributes
            )

            if !packed {
                break
            }
        }

        replyHandler(FSDirectoryVerifier(rawValue: 1), nil)
    }

    func read(from item: FSItem, at offset: off_t, length: Int, into buffer: FSMutableFileDataBuffer, replyHandler: @escaping (Int, (any Error)?) -> Void) {
        guard let node = node(for: item), let contents = node.contents else {
            replyHandler(0, posixError(ENOENT))
            return
        }

        guard offset >= 0 else {
            replyHandler(0, posixError(EINVAL))
            return
        }

        let start = Int(offset)
        guard start < contents.count else {
            replyHandler(0, nil)
            return
        }

        let count = min(length, buffer.length, contents.count - start)
        let end = start + count

        let bytesCopied = buffer.withUnsafeMutableBytes { destination in
            contents.copyBytes(to: destination, from: start..<end)
        }
        replyHandler(bytesCopied, nil)
    }

    func write(contents: Data, to item: FSItem, at offset: off_t, replyHandler: @escaping (Int, (any Error)?) -> Void) {
        replyHandler(0, posixError(EROFS))
    }

    private func node(for item: FSItem) -> StaticNode? {
        guard let staticItem = item as? ImmichStaticItem else {
            return nil
        }

        return tree.node(id: staticItem.nodeID)
    }

    private func attributes(for node: StaticNode) -> FSItem.Attributes {
        let attributes = FSItem.Attributes()
        attributes.type = node.itemType
        attributes.mode = node.isDirectory ? 0o40555 : 0o100444
        attributes.linkCount = node.isDirectory ? 2 : 1
        attributes.uid = getuid()
        attributes.gid = getgid()
        attributes.flags = 0
        attributes.size = UInt64(node.contents?.count ?? 0)
        attributes.allocSize = allocatedSize(for: attributes.size)
        attributes.fileID = itemID(node.id)
        attributes.parentID = itemID(node.parentID)
        attributes.supportsLimitedXAttrs = false
        attributes.inhibitKernelOffloadedIO = true
        attributes.accessTime = createdAt
        attributes.modifyTime = createdAt
        attributes.changeTime = createdAt
        attributes.birthTime = createdAt
        attributes.addedTime = createdAt
        attributes.backupTime = createdAt
        return attributes
    }

    private func allocatedSize(for size: UInt64) -> UInt64 {
        guard size > 0 else {
            return 0
        }

        return ((size + 4095) / 4096) * 4096
    }

    private func itemID(_ rawValue: UInt64) -> FSItem.Identifier {
        FSItem.Identifier(rawValue: rawValue)!
    }

    private func directoryEntries(for node: StaticNode, includeDotEntries: Bool) -> [DirectoryEntry] {
        var entries: [DirectoryEntry] = []

        if includeDotEntries {
            entries.append(DirectoryEntry(name: ".", node: node))
            entries.append(DirectoryEntry(name: "..", node: tree.node(id: node.parentID) ?? node))
        }

        entries.append(contentsOf: tree.children(of: node).map { DirectoryEntry(name: $0.name, node: $0) })
        return entries
    }

    private func posixError(_ code: Int32) -> NSError {
        NSError(domain: NSPOSIXErrorDomain, code: Int(code))
    }
}

@objc
final class ImmichStaticItem: FSItem {
    let nodeID: UInt64

    init(nodeID: UInt64) {
        self.nodeID = nodeID
        super.init()
    }
}

private struct DirectoryEntry {
    var name: String
    var node: StaticNode
}
