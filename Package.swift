// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "process-guard",
    platforms: [.macOS(.v12)],
    products: [.executable(name: "process-guard", targets: ["ProcessGuard"])],
    targets: [.executableTarget(name: "ProcessGuard")]
)
