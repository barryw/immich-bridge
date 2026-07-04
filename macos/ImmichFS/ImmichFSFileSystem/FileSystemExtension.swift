import ExtensionFoundation
import Foundation
import FSKit

@main
struct ImmichFSFileSystemExtension: UnaryFileSystemExtension {
    var fileSystem: FSUnaryFileSystem & FSUnaryFileSystemOperations {
        ImmichFSFileSystem()
    }
}

